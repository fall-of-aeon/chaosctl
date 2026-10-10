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

from chaosctl import llm
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
        crit = graph.nodes[node]["criticality"]
        untested = 0.0 if (node, fault) in tested else 1.0
        info_gain = 1.0 if node not in tested_nodes else 0.5
        tier_risk = (entry["tier"] - 1) / 2
        score = (WEIGHTS["criticality"] * crit + WEIGHTS["untested"] * untested
                 + WEIGHTS["info_gain"] * info_gain - WEIGHTS["tier_risk"] * tier_risk)
        out.append({"target": node_id(node), "fault": fault, "rung": rung, "tier": entry["tier"],
                    "score": round(score, 3), "criticality": crit, "untested": untested,
                    "info_gain": info_gain, "tier_risk": tier_risk})
    return sorted(out, key=lambda c: (-c["score"], c["target"], c["fault"], c["rung"]))


# ------------------------------------------------------------ fallback

HYPOTHESES = {
    "worker_pod_kill": "in-flight W1 requests fail or hang while W2 keeps returning 200 and the wait queue grows; "
                       "stalled jobs may be re-run after lock expiry, creating duplicates",
    "redis_pod_kill": "queued and active jobs are lost because redis has no persistence; n8n may restart "
                      "if the outage exceeds the 10 s redis timeout",
}


def templated_plan(graph: Graph, scored: list[dict]) -> LLMPlan:
    items = []
    seen = set()
    for c in scored:
        key = (c["target"], c["fault"])
        if key in seen:
            continue
        seen.add(key)
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

def build_prompt(graph: Graph, scored: list[dict], history: list[dict], policy: dict, feedback: str | None) -> str:
    cands = [{k: c[k] for k in ("target", "fault", "rung", "tier", "score")} for c in scored]
    hist = [{k: h.get(k) for k in ("exp_id", "node", "fault", "status", "passed")} for h in history]
    return f"""You are the injection planner of a chaos-engineering harness for an n8n queue-mode app on Kubernetes.
Choose which experiments to run, in what order, and explain why. You do not run anything.

DEPENDENCY GRAPH (edges: A depends on B; blast radius of X = nodes that can reach X):
{graph.summary()}

ENABLED CANDIDATES (only these (target, fault, rung) triples are allowed; score is a deterministic prior):
{json.dumps(cands, indent=1)}

RUN HISTORY:
{json.dumps(hist) if hist else "none"}

POLICY: tier 3 experiments are only allowed after a tier 1 experiment has passed. Inject only while requests
are in flight: start_when must be of the form "in_flight>=N" with N >= {policy['rules']['require_in_flight_before_inject']['min_in_flight']}.
cooldown_s between 30 and 600.
{f"PREVIOUS ATTEMPT WAS REJECTED: {feedback}" if feedback else ""}
Return JSON: experiments[] each with id (exp-01..), fault, target (node id), rung, start_when, cooldown_s,
reason (ONE line, cite node:/edge: ids from the graph), cites (list of node:/edge:/wf: ids used),
hypothesis (a testable prediction; it is a hypothesis, not a fact)."""


# ------------------------------------------------------------ LangGraph subgraph

class InjState(TypedDict, total=False):
    graph: Graph
    catalog: dict
    policy: dict
    history: list[dict]
    use_llm: bool
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
        j.write({"event": "agent1_scored", "candidates": scored})
        return {"scored": scored, "loops": 0, "feedback": None}

    def select(s: InjState) -> InjState:
        if not s.get("use_llm", True):
            return {"plan": templated_plan(s["graph"], s["scored"]), "source": "fallback:llm_disabled"}
        prompt = build_prompt(s["graph"], s["scored"], s.get("history", []), s["policy"], s.get("feedback"))
        try:
            plan = llm.generate_json("injection_select", prompt, LLMPlan, journal=j, call=llm_call)
            return {"plan": plan, "source": "llm"}
        except llm.LLMUnavailable as exc:
            j.write({"event": "agent1_fallback", "why": str(exc)})
            return {"plan": templated_plan(s["graph"], s["scored"]), "source": f"fallback:{exc}"}

    def check(s: InjState) -> InjState:
        errs = validate(s["plan"], s["graph"], s["scored"])
        if errs:
            j.write({"event": "agent1_invalid", "errors": errs, "source": s["source"]})
            return {"plan": templated_plan(s["graph"], s["scored"]), "source": "fallback:invalid_plan", "errors": errs}
        return {"errors": []}

    def guard(s: InjState) -> InjState:
        plan, verdicts, notes = policy_guard(s["plan"], s["graph"], s["catalog"], s["policy"], s.get("history", []))
        rejected = [v for v in verdicts if v.verdict != "approve"]
        if rejected and s.get("loops", 0) < MAX_GUARD_LOOPS and s["source"] == "llm":
            fb = "; ".join(f"{v.exp_id}: {v.rationale}" for v in rejected)
            j.write({"event": "agent1_guard_reject", "feedback": fb})
            return {"feedback": fb, "loops": s.get("loops", 0) + 1, "verdicts": verdicts}
        if rejected:
            keep = {v.exp_id for v in verdicts if v.verdict == "approve"}
            plan = LLMPlan(experiments=[e for e in plan.experiments if e.id in keep])
            verdicts = [v for v in verdicts if v.exp_id in keep]
            notes.append(f"dropped rejected experiments: {[v.exp_id for v in rejected]}")
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
