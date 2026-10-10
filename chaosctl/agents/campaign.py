"""Campaign graph (BUILD_PLAN 9.2): load_graph -> injection_agent -> [human_gate] -> execute
(wait_in_flight, inject, observe, recover, collect_evidence inside Runner) -> update_history -> loop.

The diagnosis agent (Day 2) slots in after execute.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from chaosctl.agents.injection import GuardVerdict, LLMPlan, build_injection_agent, runtime_guard
from chaosctl.chaos.chaosmesh import load_catalog
from chaosctl.graph import Graph
from chaosctl.journal import Journal

HISTORY_PATH = Path(__file__).parents[2] / "state" / "history.json"


def load_history() -> list[dict]:
    return json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else []


def save_history(h: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(h, indent=2))


class CampaignState(TypedDict, total=False):
    graph: Graph
    catalog: dict
    policy: dict
    history: list[dict]
    use_llm: bool
    plan: LLMPlan
    verdicts: list[GuardVerdict]
    plan_source: str
    queue: list[str]          # exp ids left to run
    only: list[str] | None    # restrict to these faults
    max_experiments: int | None
    results: list[dict]
    stop_reason: str | None


def build_campaign(runner, graph_loader, policy: dict, journal: Journal, approve_fn=None, llm_call=None):
    agent = build_injection_agent(journal, llm_call=llm_call)

    def load_graph(s: CampaignState) -> CampaignState:
        g = graph_loader()
        journal.write({"event": "graph_loaded", "nodes": len(g.nodes), "edges": len(g.edges)})
        return {"graph": g, "catalog": load_catalog(), "policy": policy,
                "history": load_history(), "results": [], "stop_reason": None}

    def plan(s: CampaignState) -> CampaignState:
        out = agent.invoke({"graph": s["graph"], "catalog": s["catalog"], "policy": s["policy"],
                            "history": s["history"], "use_llm": s.get("use_llm", True)})
        exps = out["plan"].experiments
        if s.get("only"):
            exps = [e for e in exps if e.fault in s["only"]]
        if s.get("max_experiments"):
            exps = exps[: s["max_experiments"]]
        return {"plan": LLMPlan(experiments=exps), "verdicts": out["verdicts"], "plan_source": out["source"],
                "queue": [e.id for e in exps]}

    def execute(s: CampaignState) -> CampaignState:
        exp_id = s["queue"][0]
        item = next(e for e in s["plan"].experiments if e.id == exp_id)
        verdict = next(v for v in s["verdicts"] if v.exp_id == exp_id)
        verdict = runtime_guard(item, verdict, s["graph"], s["catalog"], s["policy"], s["history"])
        journal.write({"event": "runtime_guard", "exp_id": exp_id, "verdict": verdict.model_dump()})
        if verdict.verdict != "approve":
            res = {"exp_id": exp_id, "fault": item.fault, "target": item.target, "status": "skipped",
                   "skip_reason": f"guard: {verdict.rationale}"}
        elif approve_fn and not approve_fn(item, verdict):
            res = {"exp_id": exp_id, "fault": item.fault, "target": item.target, "status": "skipped",
                   "skip_reason": "human declined"}
        else:
            res = runner.run_experiment(item.model_dump(), verdict.model_dump())
        res["plan_reason"], res["hypothesis"] = item.reason, item.hypothesis
        return {"results": s["results"] + [res], "queue": s["queue"][1:]}

    def update_history(s: CampaignState) -> CampaignState:
        res = s["results"][-1]
        rec = {"exp_id": res["exp_id"], "run_id": getattr(runner, "run_id", None),
               "node": res["target"].removeprefix("node:"), "fault": res["fault"], "status": res["status"],
               "passed": res["status"] == "completed" and not res.get("manual_action_needed"), "ts": time.time()}
        hist = s["history"] + [rec]
        save_history(hist)
        stop = None
        if runner.ctl and runner.ctl.abort_reason == "kill_switch":
            stop = "kill_switch"
        elif res["status"] == "aborted":
            stop = f"aborted: {res.get('abort_reason')}"
        if res.get("watchdog", {}).get("recovered") is False:
            stop = "rollback_failed"
        return {"history": hist, "stop_reason": stop}

    def cooldown(s: CampaignState) -> CampaignState:
        item = next((e for e in s["plan"].experiments if e.id == s["results"][-1]["exp_id"]), None)
        if s["queue"] and item and s["results"][-1]["status"] != "skipped":
            journal.write({"event": "cooldown", "seconds": item.cooldown_s})
            t0 = time.time()
            while time.time() - t0 < item.cooldown_s:
                if runner.ctl and runner.ctl.aborted:
                    break
                time.sleep(1)
        return {}

    def more(s: CampaignState) -> str:
        if s.get("stop_reason") or not s.get("queue"):
            return "end"
        return "next"

    g = StateGraph(CampaignState)
    g.add_node("load_graph", load_graph)
    g.add_node("injection_agent", plan)
    g.add_node("execute", execute)
    g.add_node("update_history", update_history)
    g.add_node("cooldown", cooldown)
    g.add_edge(START, "load_graph")
    g.add_edge("load_graph", "injection_agent")
    g.add_conditional_edges("injection_agent", lambda s: "execute" if s.get("queue") else "end",
                            {"execute": "execute", "end": END})
    g.add_edge("execute", "update_history")
    g.add_conditional_edges("update_history", more, {"next": "cooldown", "end": END})
    g.add_edge("cooldown", "execute")
    return g.compile()
