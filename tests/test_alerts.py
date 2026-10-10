"""Nothing degrades silently: every fallback is alerted, and --require-llm turns it into a failure."""
import json

import pytest
import yaml

from chaosctl import alerts, llm, rag
from chaosctl.agents import diagnosis as d
from chaosctl.agents import injection as inj
from chaosctl.chaos.chaosmesh import load_catalog
from chaosctl.loadgen import LoadGen
from tests.test_diagnosis import bundle
from tests.test_graph import build

CAT = load_catalog()
POLICY = yaml.safe_load(open("chaosctl/policy.yaml"))


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(llm, "budget", llm._Budget())
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    monkeypatch.setenv("MISTRAL_MODEL", "m")
    monkeypatch.delenv("MISTRAL_FALLBACK_MODEL", raising=False)


def down(*a):
    raise TimeoutError("read timeout")


def plan(call):
    return inj.build_injection_agent(llm_call=call).invoke(
        {"graph": build(), "catalog": CAT, "policy": POLICY, "history": [], "use_llm": True})


def test_agent1_fallback_is_alerted_with_the_reason(tmp_path):
    out = plan(down)
    assert out["source"].startswith("fallback:")
    [a] = [e for e in alerts.seen() if e["kind"] == "llm_fallback"]
    assert a["component"] == "Agent 1 (planner)" and "TimeoutError" in a["reason"]
    assert json.loads((tmp_path / "degraded.jsonl").read_text().splitlines()[-1])["kind"] == "llm_fallback"


def test_agent1_live_and_cached_answers_are_labelled():
    good = lambda m, p, s: json.dumps({"experiments": [{"id": "exp-01", "fault": "worker_pod_kill",
        "target": "node:n8n-worker", "rung": "ungraceful", "start_when": "in_flight>=3", "cooldown_s": 60,
        "reason": "node:n8n-worker", "cites": ["node:n8n-worker"], "hypothesis": "h"}]})  # noqa: E731
    assert plan(good)["source"] == "llm:api:m"
    assert plan(good)["source"] == "llm:cache:m"
    assert alerts.seen() == []


def test_invalid_llm_plan_is_alerted():
    bad = lambda m, p, s: json.dumps({"experiments": [{"id": "exp-01", "fault": "nope", "target": "node:redis",
        "rung": "x", "start_when": "in_flight>=3", "cooldown_s": 60, "reason": "x", "cites": ["node:redis"],
        "hypothesis": "h"}]})  # noqa: E731
    out = plan(bad)
    assert out["source"] == "fallback:invalid_plan"
    assert any("failed validation" in e["reason"] for e in alerts.seen())


def test_require_llm_turns_fallback_into_failure(monkeypatch):
    monkeypatch.setenv("CHAOSCTL_REQUIRE_LLM", "1")
    with pytest.raises(alerts.LLMRequired):
        plan(down)


def test_diagnosis_fallback_is_alerted_and_marked(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    out = d.diagnose(bundle(), use_llm=True)
    assert out["llm_used"] is False and out["source"] == "fallback:MISTRAL_API_KEY not set"
    assert [e["component"] for e in alerts.seen()] == ["Agent 2 (diagnosis)"]


def test_incomplete_evidence_is_alerted():
    b = bundle() | {"collection_errors": ["logs n8n-worker-1: ApiException: 500"]}
    d.diagnose(b, use_llm=False)
    assert any("incomplete" in e["reason"] for e in alerts.seen())


def test_rag_fallback_is_alerted(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(rag, "build_corpus", lambda: [{"id": "playbook:x", "title": "t", "text": "redis restart fix"}])
    out = rag.ask("redis restart")
    assert out["llm_used"] is False and "NOT used" in out["answer"]
    assert alerts.seen()[0]["component"] == "Ask Agent 2 (chat)"


def test_llm_error_message_names_every_cause():
    class Quota(Exception):
        code = 429
    def fake(model, prompt, schema):
        raise Quota("quota exceeded")
    with pytest.raises(llm.LLMUnavailable, match="m: Quota: quota exceeded"):
        llm.generate_json("t", "p", inj.LLMPlan, call=fake)


def test_warn_once_dedupes():
    alerts.warn_once("k", "c", "r")
    alerts.warn_once("k", "c", "r")
    assert len(alerts.seen()) == 1


def test_load_spike_returns_to_base_rate(tmp_path):
    lg = LoadGen(tmp_path / "lg.sqlite", rate=2.0)
    lg.spike(6.0, 30)
    assert lg.rate == 6.0 and lg.spiking
    lg.end_spike()
    assert lg.rate == 2.0 and not lg.spiking


def test_logs_are_limited_to_the_experiment_window():
    from chaosctl.probes import K8sProbe

    class Core:
        def read_namespaced_pod_log(self, pod, ns, timestamps, previous, _request_timeout, **kw):
            return ("2026-10-11T01:20:00.000000000Z ERROR: No space left on device\n"
                    "2026-10-11T01:35:10.500000000Z ERROR: connection refused\n"
                    "2026-10-11T01:50:00.000000000Z late line")
    from datetime import datetime, timezone
    t = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()  # noqa: E731
    k = K8sProbe(core=Core())
    assert k.logs("pg", 50, since_ts=t("2026-10-11T01:35:00"), until_ts=t("2026-10-11T01:40:00")) == \
        ["ERROR: connection refused"]
