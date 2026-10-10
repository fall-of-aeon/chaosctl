import json
from pathlib import Path

import pytest
import yaml

from chaosctl import llm
from chaosctl.agents import injection as inj
from chaosctl.chaos.chaosmesh import load_catalog
from tests.test_graph import build

CAT = load_catalog(Path(__file__).parent / "fixtures" / "catalog.yaml")  # frozen; live catalog may change
POLICY = yaml.safe_load(open("chaosctl/policy.yaml"))


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(llm, "budget", llm._Budget())
    monkeypatch.setenv("MISTRAL_MODEL", "m")
    monkeypatch.delenv("MISTRAL_FALLBACK_MODEL", raising=False)


E1E2 = ["worker_pod_kill", "redis_pod_kill"]


def run(use_llm=True, call=None, history=None, only=E1E2):
    agent = inj.build_injection_agent(llm_call=call)
    return agent.invoke({"graph": build(), "catalog": CAT, "policy": POLICY,
                         "history": history or [], "use_llm": use_llm, "only": only})


def test_scored_candidates_only_enabled():
    scored = inj.score_candidates(build(), CAT, [])
    enabled = {f for f, e in CAT["faults"].items() if e.get("enabled")}
    assert {c["fault"] for c in scored} == enabled
    assert not any(CAT["faults"][c["fault"]].get("test_only") for c in scored)
    assert {(c["target"], c["fault"], c["rung"]) for c in scored} >= {
        ("node:n8n-worker", "worker_pod_kill", "ungraceful"), ("node:redis", "redis_pod_kill", "ungraceful"),
        ("node:n8n-worker", "worker_redis_partition", "d60"), ("node:mock-api", "mockapi_latency", "past_timeout")}


def test_full_catalog_fallback_is_capped_diverse_and_gentle():
    a, b = run(use_llm=False, only=None), run(use_llm=False, only=None)
    assert a["plan"] == b["plan"]
    exps = a["plan"].experiments
    assert len(exps) == POLICY["rules"]["max_experiments_per_plan"]
    assert len({e.target for e in exps}) >= 4                      # nodes take turns
    tiers = [CAT["faults"][e.fault]["tier"] for e in exps]
    assert tiers == sorted(tiers)                                  # tier rule
    for e in exps:                                                 # gentlest rung of each fault
        assert e.rung == next(iter(CAT["faults"][e.fault]["ladder"]))
    assert all(v.verdict == "approve" for v in a["verdicts"])


def test_fallback_plan_orders_e1_before_e2_and_is_deterministic():
    a, b = run(use_llm=False), run(use_llm=False)
    assert a["plan"] == b["plan"]
    faults = [e.fault for e in a["plan"].experiments]
    assert faults == ["worker_pod_kill", "redis_pod_kill"]
    v = {x.exp_id: x for x in a["verdicts"]}
    assert v["exp-02"].order_after == "exp-01" and v["exp-02"].verdict == "approve"
    assert v["exp-02"].blast_radius == ["node:n8n-main", "node:n8n-worker"]
    assert v["exp-02"].abort["error_rate_abort"] is False   # tier 3: outage by design
    for e in a["plan"].experiments:
        assert all(build().has_id(c) for c in e.cites)


def llm_plan(items):
    return lambda model, prompt, schema: json.dumps({"experiments": items})


ITEM = dict(start_when="in_flight>=3", cooldown_s=60, hypothesis="h")


def test_guard_reorders_llm_plan_redis_first():
    call = llm_plan([
        {**ITEM, "id": "exp-01", "fault": "redis_pod_kill", "target": "node:redis", "rung": "ungraceful",
         "reason": "node:redis is a SPOF (edge:n8n-main->redis)", "cites": ["node:redis", "edge:n8n-main->redis"]},
        {**ITEM, "id": "exp-02", "fault": "worker_pod_kill", "target": "node:n8n-worker", "rung": "ungraceful",
         "reason": "node:n8n-worker consumes jobs", "cites": ["node:n8n-worker"]},
    ])
    out = run(call=call)
    assert out["source"] == "llm:api:m"
    assert [e.fault for e in out["plan"].experiments] == ["worker_pod_kill", "redis_pod_kill"]
    assert any("reordered" in n for n in out["notes"])


@pytest.mark.parametrize("bad", [
    {"fault": "redis_pod_kill", "target": "node:postgres", "rung": "ungraceful", "reason": "x", "cites": ["node:postgres"]},
    {"fault": "worker_cpu_stress", "target": "node:n8n-worker", "rung": "w1_load80", "reason": "x", "cites": ["node:n8n-worker"]},
    {"fault": "worker_pod_kill", "target": "node:n8n-worker", "rung": "ungraceful", "reason": "x", "cites": ["node:kafka"]},
    {"fault": "worker_pod_kill", "target": "node:n8n-worker", "rung": "ungraceful", "reason": "see edge:redis->postgres", "cites": ["node:n8n-worker"]},
])
def test_invalid_llm_plans_fall_back(bad):
    out = run(call=llm_plan([{**ITEM, "id": "exp-01", **bad}]))
    assert out["source"] == "fallback:invalid_plan"
    assert [e.fault for e in out["plan"].experiments] == ["worker_pod_kill", "redis_pod_kill"]


def test_llm_down_falls_back():
    def boom(*a):
        raise TimeoutError()
    out = run(call=boom)
    assert out["source"].startswith("fallback:")
    assert len(out["plan"].experiments) == 2


def test_runtime_guard_rejects_tier3_without_pass_and_allows_after():
    g = build()
    plan = run(use_llm=False)
    e2, v2 = plan["plan"].experiments[1], plan["verdicts"][1]
    assert inj.runtime_guard(e2, v2, g, CAT, POLICY, []).verdict == "reject"
    hist = [{"exp_id": "exp-01", "node": "n8n-worker", "fault": "worker_pod_kill", "status": "completed", "passed": True}]
    assert inj.runtime_guard(e2, v2, g, CAT, POLICY, hist).verdict == "approve"


def test_guard_cannot_loosen_thresholds():
    th = inj.thresholds_for("worker_pod_kill", CAT, POLICY, overrides={"queue_waiting": 99999, "w1_error_rate": 0.99})
    assert th.queue_waiting == 500 and th.w1_error_rate == 0.5
    th2 = inj.thresholds_for("worker_pod_kill", CAT, POLICY, overrides={"queue_waiting": 1})
    assert th2.queue_waiting == 1
