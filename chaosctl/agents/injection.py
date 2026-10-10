"""Agent 1: Injection agent (BUILD_PLAN 9.3).

score_candidates (Python) -> llm_select (Mistral) -> validate -> policy_guard (deterministic).
The LLM only chooses among catalog candidates and explains; the guard decides.
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from chaosctl import alerts, llm
from chaosctl.graph import Graph, node_id
from chaosctl.journal import Journal, NullJournal
from chaosctl.safety import Thresholds

WEIGHTS = {"criticality": 0.4, "untested": 0.3, "info_gain": 0.2, "tier_risk": 0.1}
START_WHEN = re.compile(r"^in_flight>=\d+$")
MAX_GUARD_LOOPS = 2


# ------------------------------------------------------------ schemas

class PlanItem(BaseModel):
    id: str = Field(description="exp-01, exp-02, ...")
    fault: str
    target: str = Field(description="node id, e.g. node:redis")
    rung: str
    start_when: str = Field(description="in_flight>=N")
    cooldown_s: int = Field(ge=30, le=600)
    reason: str = Field(description="one line citing node/edge ids")
    cites: list[str]
    hypothesis: str


class LLMPlan(BaseModel):
    experiments: list[PlanItem]


class GuardVerdict(BaseModel):
    exp_id: str
    verdict: str  # approve | downgrade | reject
    blast_radius: list[str]
    workflows_affected: dict[str, str]
    abort: dict[str, Any]
    rationale: str
    order_after: str | None = None


# ------------------------------------------------------------ scoring

def score_candidates(graph: Graph, catalog: dict, history: list[dict]) -> list[dict]:
    tested = {(h["node"], h["fault"]) for h in history if h.get("status") in ("completed", "aborted")}
    tested_nodes = {n for n, _ in tested}
    out = []
    for node, fault, rung in graph.candidates(catalog):
        entry = catalog["faults"][fault]
        rung_index = list(entry.get("ladder", {})).index(rung) if rung in entry.get("ladder", {}) else 0
        crit = graph.nodes[node]["criticality"]
        untested = 0.0 if (node, fault) in tested else 1.0
        info_gain = 1.0 if node not in tested_nodes else 0.5
        tier_risk = (entry["tier"] - 1) / 2
        score = (WEIGHTS["criticality"] * crit + WEIGHTS["untested"] * untested
                 + WEIGHTS["info_gain"] * info_gain - WEIGHTS["tier_risk"] * tier_risk)
        out.append({"target": node_id(node), "fault": fault, "rung": rung, "rung_index": rung_index,
                    "tier": entry["tier"], "kind": entry["chaos_kind"], "reversible": bool(entry.get("reversible")),
                    "score": round(score, 3), "criticality": crit, "untested": untested,
                    "info_gain": info_gain, "tier_risk": tier_risk})
    # ladders are listed gentlest first, so ties go to the gentler rung
    return sorted(out, key=lambda c: (-c["score"], c["target"], c["fault"], c["rung_index"]))


# ------------------------------------------------------------ fallback

HYPOTHESES = {
    "worker_pod_kill": "in-flight W1 requests fail or hang while W2 keeps returning 200 and the wait queue grows; "
                       "stalled jobs may be re-run after lock expiry, creating duplicates",
    "worker_graceful_kill": "the worker drains its active jobs within the 30 s grace period, so no in-flight "
                            "W1 request fails and no job is re-run",
    "worker_pod_failure": "the failed worker stays Ready (no readiness signal), its active jobs stall until "
                          "lock expiry and are redelivered to the other worker; W1 p95 rises",
    "worker_cpu_stress": "W1 p95 rises on the stressed worker but no request fails and the queue drains after",
    "worker_memory_stress": "the worker slows under memory pressure but is not OOM-killed (no limits set)",
    "worker_clock_skew": "a skewed worker clock makes Bull treat locks as expired, causing duplicate executions",
    "mockapi_http_500": "W1 requests in flight get errors (no retry on the HTTP node) while W2 still returns 200; "
                        "failed executions are recorded in postgres",
    "mockapi_http_abort": "W1 requests fail fast with a connection error; the worker stays healthy",
    "mockapi_latency": "W1 p95 rises by the injected delay; past the 30 s known timeout W1 requests fail",
    "redis_latency": "enqueue and dequeue slow down, so W1 and W2 latency rise and the wait queue grows; "
                     "no job is lost",
    "postgres_latency": "execution saves slow down, so W1 p95 rises; W2 responds as soon as the job is queued",
    "worker_redis_partition": "the other worker keeps taking new jobs; jobs active on the partitioned worker "
                              "finish late, or, if the partition reaches the 60 s lock duration, are redelivered "
                              "and run twice",
    "worker_container_kill": "the worker container restarts in place (restart count +1); its active jobs stall "
                             "until lock expiry and are redelivered; W1 p95 rises, the other worker keeps going",
    "mockapi_container_kill": "W1 requests in flight on the mock fail (connection reset/refused) until the container "
                              "is back a few seconds later; n8n records failed executions",
    "packet_loss": "TCP retransmits turn dropped packets into latency spikes on every call to the target; "
                   "W1 p95 rises, a few requests may time out, nothing is lost",
    "postgres_overload": "queries and execution saves slow down (CPU) or new connections are refused (held "
                         "connections): W1 p95 rises or W1 fails; W2 keeps returning 200 while work backs up",
    "traffic_spike": "the queue grows faster than the workers drain it; W1 p95 climbs toward the 30 s timeout and "
                     "the backlog drains only after the spike ends",
    "postgres_disk_full": "postgres cannot write WAL: execution saves fail, postgres may PANIC and restart, W1 "
                          "fails while W2 still says 200; it recovers once the ballast is removed",
    "redis_pod_kill": "queued and active jobs are lost because redis has no persistence; n8n may restart "
                      "if the outage exceeds the 10 s redis timeout",
    "main_pod_kill": "webhook calls fail until n8n-main is back; jobs already queued still finish on the workers",
    "postgres_pod_kill": "executions cannot be saved: in-flight W1 requests fail and workers log DB errors "
                         "until postgres is back; queued jobs resume after",
}


def templated_plan(graph: Graph, scored: list[dict], max_n: int | None = None) -> LLMPlan:
    """One experiment per (target, fault) at its gentlest rung, up to max_n. Nodes take turns
    (best fault of every node, then the second best, ...), so a short plan covers more nodes."""
    gentlest: dict[tuple[str, str], dict] = {}
    for c in scored:  # already sorted by score, then gentlest rung
        gentlest.setdefault((c["target"], c["fault"]), c)
    per_node: dict[str, list[dict]] = {}
    for c in gentlest.values():
        per_node.setdefault(c["target"], []).append(c)
    picks = []
    for rnd in range(max((len(v) for v in per_node.values()), default=0)):
        picks += sorted((v[rnd] for v in per_node.values() if rnd < len(v)),
                        key=lambda c: (-c["score"], c["target"], c["fault"]))
    items = []
    for c in picks[:max_n]:
        name = c["target"].removeprefix("node:")
        deps = [e["id"] for e in graph.edges if e["to"] == name][:3]
        br = graph.blast_radius(name)
        reason = (f"{c['target']} is on {len(graph.nodes[name]['on_paths'])}/{len(graph.workflows)} workflow paths"
                  f"{', single point of failure' if graph.nodes[name]['spof'] else ''}"
                  f" ({', '.join(deps)}); blast radius {br['nodes']}; "
                  f"{'untested' if c['untested'] else 'retest'} (score {c['score']})")
        items.append(PlanItem(
            id=f"exp-{len(items) + 1:02d}", fault=c["fault"], target=c["target"], rung=c["rung"],
            start_when="in_flight>=3", cooldown_s=60, reason=reason, cites=[c["target"], *deps],
            hypothesis=HYPOTHESES.get(c["fault"], f"{name} failure degrades {list(graph.workflows_affected(name))}")))
    return LLMPlan(experiments=items)


# ------------------------------------------------------------ validation

def validate(plan: LLMPlan, graph: Graph, scored: list[dict]) -> list[str]:
    errs = []
    valid = {(c["target"], c["fault"], c["rung"]) for c in scored}
    if not plan.experiments:
        errs.append("empty plan")
    seen_ids = set()
    for it in plan.experiments:
        if it.id in seen_ids:
            errs.append(f"{it.id}: duplicate id")
        seen_ids.add(it.id)
        if (it.target, it.fault, it.rung) not in valid:
            errs.append(f"{it.id}: ({it.target}, {it.fault}, {it.rung}) is not an enabled catalog candidate")
        if not START_WHEN.match(it.start_when.replace(" ", "")):
            errs.append(f"{it.id}: bad start_when {it.start_when!r}")
        bad = [c for c in it.cites if not graph.has_id(c)]
        if bad:
            errs.append(f"{it.id}: cites unknown ids {bad}")
        if not it.cites:
            errs.append(f"{it.id}: no graph ids cited")
        for ref in re.findall(r"(?:node|edge|wf):\w+(?:-\w+)*(?:->\w+(?:-\w+)*)?", it.reason):
            if not graph.has_id(ref):
                errs.append(f"{it.id}: reason cites unknown id {ref}")
    return errs


# ------------------------------------------------------------ policy guard

def tier1_passed(history: list[dict], catalog: dict) -> str | None:
    for h in history:
        if catalog["faults"].get(h["fault"], {}).get("tier") == 1 and h.get("passed"):
            return h["exp_id"]
    return None


def thresholds_for(fault: str, catalog: dict, policy: dict, overrides: dict | None = None) -> Thresholds:
    entry = catalog["faults"][fault]
    d = policy["defaults"]["abort"]
    tier = policy["tiers"].get(entry["tier"], {})
    base = Thresholds(
        w1_error_rate=d["w1_error_rate"], w1_p95_ms=d["w1_p95_ms"], queue_waiting=d["queue_waiting"],
        max_total_s=tier.get("max_total_s", entry.get("max_total_s", 180)),
        recovery_deadline_s=policy.get("experiments", {}).get(fault, {}).get("recovery_deadline_s",
                                                                             entry.get("recovery_deadline_s", 120)),
        error_rate_armed_after_s=tier.get("error_rate_armed_after_s", d.get("error_rate_armed_after_s", 0)),
        error_rate_abort=tier.get("error_rate_abort", True),
    )
    if overrides:
        allowed = {k: v for k, v in overrides.items() if k in Thresholds.__dataclass_fields__}
        base = base.tightened(Thresholds(**{**base.__dict__, **allowed}))
    return base


def policy_guard(plan: LLMPlan, graph: Graph, catalog: dict, policy: dict, history: list[dict],
                 overrides: dict | None = None) -> tuple[LLMPlan, list[GuardVerdict], list[str]]:
    """Reorder for tier rules and compute per-experiment verdicts. Returns (plan, verdicts, notes)."""
    notes = []
    items = list(plan.experiments)
    rule = policy["rules"].get("tier3_requires_prior_tier1_pass", True)
    tier = lambda it: catalog["faults"][it.fault]["tier"]  # noqa: E731

    # stable sort by tier so tier 3 always follows a tier 1
    reordered = sorted(items, key=tier) if rule else items
    if [i.id for i in reordered] != [i.id for i in items]:
        notes.append("reordered by tier rule: " + " -> ".join(f"{i.id}({i.fault}, tier {tier(i)})" for i in reordered))
    items = [it.model_copy(update={"id": f"exp-{n + 1:02d}"}) for n, it in enumerate(reordered)]

    verdicts = []
    passed = tier1_passed(history, catalog)
    planned_tier1 = None
    for it in items:
        name = it.target.removeprefix("node:")
        br = graph.blast_radius(name)
        th = thresholds_for(it.fault, catalog, policy, overrides)
        verdict, order_after = "approve", None
        if not graph.nodes.get(name, {}).get("injectable", True):
            verdict, rationale = "reject", f"{it.target} is not injectable"
        elif tier(it) >= 3 and rule:
            if passed:
                rationale = f"tier 3 allowed: tier 1 experiment {passed} passed"
            elif planned_tier1:
                order_after = planned_tier1
                rationale = f"tier 3 allowed only after tier 1 experiment {planned_tier1} passes (checked again at run time)"
            else:
                verdict, rationale = "reject", "tier 3 requires a passed tier 1 experiment first"
        else:
            rationale = f"tier {tier(it)}; blast radius {[node_id(n) for n in br['nodes']]}" \
                        f"{' with client impact' if br['client_impact'] else ''}"
        if tier(it) == 1 and planned_tier1 is None:
            planned_tier1 = it.id
        abort = {k: getattr(th, k) for k in ("w1_error_rate", "w1_p95_ms", "queue_waiting", "max_total_s",
                                              "recovery_deadline_s", "error_rate_armed_after_s", "error_rate_abort")}
        verdicts.append(GuardVerdict(
            exp_id=it.id, verdict=verdict, blast_radius=[node_id(n) for n in br["nodes"]],
            workflows_affected={f"wf:{k}": v for k, v in graph.workflows_affected(name).items()},
            abort=abort, rationale=rationale, order_after=order_after))
    return LLMPlan(experiments=items), verdicts, notes


def runtime_guard(item: PlanItem, verdict: GuardVerdict, graph: Graph, catalog: dict, policy: dict,
                  history: list[dict]) -> GuardVerdict:
    """Re-check just before execution with the latest history (tier gate is enforced here)."""
    plan, verdicts, _ = policy_guard(LLMPlan(experiments=[item]), graph, catalog, policy, history)
    v = verdicts[0]
    return v.model_copy(update={"exp_id": verdict.exp_id})


# ------------------------------------------------------------ LLM prompt

def max_plan(policy: dict) -> int:
    return policy["rules"].get("max_experiments_per_plan", 6)


def build_prompt(graph: Graph, scored: list[dict], history: list[dict], policy: dict, feedback: str | None) -> str:
    cands = [{k: c[k] for k in ("target", "fault", "rung", "tier", "kind", "reversible", "score")} for c in scored]
    max_n = max_plan(policy)
    hist = [{k: h.get(k) for k in ("exp_id", "node", "fault", "status", "passed")} for h in history]
    return f"""You are the injection planner of a chaos-engineering harness for an n8n queue-mode app on Kubernetes.
Choose which experiments to run, in what order, and explain why. You do not run anything.

DEPENDENCY GRAPH (edges: A depends on B; blast radius of X = nodes that can reach X):
{graph.summary()}

ENABLED CANDIDATES (only these (target, fault, rung) triples are allowed; score is a deterministic prior;
rungs of one fault are listed gentlest first; reversible faults end on their own after a duration):
{json.dumps(cands, indent=1)}

RUN HISTORY:
{json.dumps(hist) if hist else "none"}

POLICY: tier 3 experiments are only allowed after a tier 1 experiment has passed. Inject only while requests
are in flight: start_when must be of the form "in_flight>=N" with N >= {policy['rules']['require_in_flight_before_inject']['min_in_flight']}.
cooldown_s between 30 and 600. Choose at most {max_n} experiments, at most one rung per (target, fault);
prefer covering different nodes and failure kinds over repeating one.
{f"PREVIOUS ATTEMPT WAS REJECTED: {feedback}" if feedback else ""}
Return JSON: experiments[] each with id (exp-01..), fault, target (node id), rung, start_when, cooldown_s,
reason (ONE line, cite node:/edge: ids from the graph), cites (list of node:/edge:/wf: ids used),
hypothesis (a testable prediction; it is a hypothesis, not a fact)."""


def llm_source(meta: dict) -> str:
    """'llm:api:<model>' for a fresh answer, 'llm:cache:<model>' for a replay of an earlier identical prompt."""
    return f"llm:{meta.get('source', '?')}:{meta.get('model', '?')}"


def is_llm(source: str | None) -> bool:
    return bool(source) and source.startswith("llm")


# ------------------------------------------------------------ LangGraph subgraph

class InjState(TypedDict, total=False):
    graph: Graph
    catalog: dict
    policy: dict
    history: list[dict]
    use_llm: bool
    only: list[str] | None   # restrict candidates to these faults (cli --exp)
    scored: list[dict]
    plan: LLMPlan
    source: str
    errors: list[str]
    feedback: str | None
    loops: int
    verdicts: list[GuardVerdict]
    notes: list[str]


def build_injection_agent(journal: Journal | None = None, llm_call=None):
    j = journal or NullJournal()

    def score(s: InjState) -> InjState:
        scored = score_candidates(s["graph"], s["catalog"], s.get("history", []))
        if s.get("only"):
            scored = [c for c in scored if c["fault"] in s["only"]]
            if not scored:
                alerts.warn("Agent 1", f"none of {s['only']} is an enabled catalog fault for this graph; nothing to plan", j)
        j.write({"event": "agent1_scored", "candidates": scored})
        return {"scored": scored, "loops": 0, "feedback": None}

    def fallback_plan(s: InjState) -> LLMPlan:
        return templated_plan(s["graph"], s["scored"], max_plan(s["policy"]))

    def select(s: InjState) -> InjState:
        if not s.get("use_llm", True):
            alerts.warn("Agent 1 (planner)", "LLM disabled by --no-llm: using the deterministic plan", j, kind="warning")
            return {"plan": fallback_plan(s), "source": "fallback:llm_disabled"}
        prompt = build_prompt(s["graph"], s["scored"], s.get("history", []), s["policy"], s.get("feedback"))
        meta: dict = {}
        try:
            plan = llm.generate_json("injection_select", prompt, LLMPlan, journal=j, call=llm_call, meta=meta)
            return {"plan": plan, "source": llm_source(meta)}
        except llm.LLMUnavailable as exc:
            j.write({"event": "agent1_fallback", "why": str(exc)})
            alerts.llm_fallback("Agent 1 (planner)", f"{exc} -> deterministic plan used instead", j)
            return {"plan": fallback_plan(s), "source": f"fallback:{exc}"}

    def check(s: InjState) -> InjState:
        errs = validate(s["plan"], s["graph"], s["scored"])
        if errs:
            j.write({"event": "agent1_invalid", "errors": errs, "source": s["source"]})
            if is_llm(s["source"]):
                alerts.llm_fallback("Agent 1 (planner)", f"LLM plan failed validation ({'; '.join(errs)[:300]}) "
                                    "-> deterministic plan used instead", j)
            return {"plan": fallback_plan(s), "source": "fallback:invalid_plan", "errors": errs}
        return {"errors": []}

    def guard(s: InjState) -> InjState:
        plan, verdicts, notes = policy_guard(s["plan"], s["graph"], s["catalog"], s["policy"], s.get("history", []))
        rejected = [v for v in verdicts if v.verdict != "approve"]
        if rejected and s.get("loops", 0) < MAX_GUARD_LOOPS and is_llm(s["source"]):
            fb = "; ".join(f"{v.exp_id}: {v.rationale}" for v in rejected)
            j.write({"event": "agent1_guard_reject", "feedback": fb})
            return {"feedback": fb, "loops": s.get("loops", 0) + 1, "verdicts": verdicts}
        if rejected:
            keep = {v.exp_id for v in verdicts if v.verdict == "approve"}
            plan = LLMPlan(experiments=[e for e in plan.experiments if e.id in keep])
            verdicts = [v for v in verdicts if v.exp_id in keep]
            notes.append(f"dropped rejected experiments: {[v.exp_id for v in rejected]}")
            alerts.warn("policy guard", "dropped experiments the guard rejected: "
                        + "; ".join(f"{v.exp_id}: {v.rationale}" for v in rejected), j, kind="warning")
        for n in notes:
            j.write({"event": "guard_note", "note": n})
        j.write({"event": "agent1_plan", "source": s["source"], "plan": plan.model_dump(),
                 "verdicts": [v.model_dump() for v in verdicts]})
        return {"plan": plan, "verdicts": verdicts, "notes": notes, "feedback": None}

    def after_guard(s: InjState) -> str:
        return "select" if s.get("feedback") else END

    g = StateGraph(InjState)
    g.add_node("score", score)
    g.add_node("select", select)
    g.add_node("validate", check)
    g.add_node("guard", guard)
    g.add_edge(START, "score")
    g.add_edge("score", "select")
    g.add_edge("select", "validate")
    g.add_edge("validate", "guard")
    g.add_conditional_edges("guard", after_guard, {"select": "select", END: END})
    return g.compile()
