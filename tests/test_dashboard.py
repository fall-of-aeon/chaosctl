"""Dashboard: node-state rule, graph -> layout, derived tables, API response shapes."""
import json
import re
from pathlib import Path

import pytest
import yaml

from chaosctl.dash import hub as hub_mod
from chaosctl.dash import model, sources
from chaosctl.graph import Graph

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parents[1]


def fixture_graph() -> dict:
    docs = []
    for f in sorted((FIX / "manifests").glob("*.yaml")):
        docs += [d for d in yaml.safe_load_all(f.read_text()) if d]
    return Graph.build(docs).to_dict()


# ------------------------------------------------------------------ node state rule

@pytest.mark.parametrize("sig,last_bad,want", [
    ({}, None, "Unknown"),
    ({"ready": 1, "desired": 1}, None, "Healthy"),
    ({"ready": 0, "desired": 1}, None, "Down"),
    ({"ready": 1, "desired": 1, "reachable": False, "probe": "Redis probe"}, None, "Down"),
    ({"reachable": False}, None, "Down"),
    ({"ready": 1, "desired": 2}, None, "Degraded"),
    ({"ready": 1, "desired": 1, "restarted_recently": True}, None, "Degraded"),
    ({"ready": 1, "desired": 1, "error_rate": 0.5}, None, "Degraded"),
    ({"ready": 1, "desired": 1, "error_rate": 0.05}, None, "Healthy"),
    ({"ready": 1, "desired": 1, "latency_ratio": 2.5}, None, "Degraded"),
    ({"ready": 2, "desired": 2, "queue_excess": 6}, None, "Degraded"),
    ({"ready": 2, "desired": 2, "queue_excess": 5}, None, "Healthy"),
    ({"ready": 1, "desired": 1, "reachable": True, "keys_from": 40, "keys_to": 3}, None, "Degraded"),
    ({"ready": 1, "desired": 1, "reachable": True, "injected_error_rate": 0.2}, None, "Degraded"),
    ({"ready": 1, "desired": 1}, 100.0, "Recovering"),   # bad 10 s ago (now = 110)
    ({"ready": 1, "desired": 1}, 60.0, "Healthy"),       # bad 50 s ago: past the 30 s hold
])
def test_node_state_rule(sig, last_bad, want):
    state, why = model.node_state(sig, last_bad, now=110.0)
    assert state == want
    assert (why == []) == (state == "Healthy")


def test_down_wins_over_degraded():
    assert model.node_state({"ready": 0, "desired": 2, "error_rate": 0.9}, None, 0)[0] == "Down"


def test_signals_come_from_graph_roles():
    g = fixture_graph()
    roles = model.roles(g)
    assert roles["n8n-main"] == "client_facing" and roles["n8n-worker"] == "consumer"
    assert roles["redis"] == "queue" and roles["postgres"] == "db" and roles["mock-api"] == "mock"
    sample = {"load_on": True, "w1_err_rate": 0.4, "w1_p95": 30000, "redis": {"reachable": True, "wait": 30, "active": 0, "dbsize": 2},
              "postgres": {"reachable": False}, "mock": {"reachable": True, "error_rate": 0.0},
              "pods": {n: {"ready_count": 1} for n in ("n8n-main", "n8n-worker", "redis", "postgres", "mock-api")}}
    base = {"w1": {"p95_ms": 5000}, "queue": {"wait_max": 0}}
    sigs = model.node_signals(g, sample, base, {"restarted": {}, "keys_before": 40})
    states = {n: model.node_state(s, None, 0)[0] for n, s in sigs.items()}
    assert states == {"n8n-main": "Degraded", "n8n-worker": "Degraded", "redis": "Degraded",
                      "postgres": "Down", "mock-api": "Healthy"}
    assert "client" not in sigs and "prometheus" not in sigs
    # a backlog draining (65 of 80 keys left) is not state loss
    drain = model.node_signals(g, {**sample, "redis": {"reachable": True, "wait": 0, "active": 0, "dbsize": 65}}, base,
                               {"restarted": {}, "keys_before": 80})
    assert model.node_state(drain["redis"], None, 0)[0] == "Healthy"


# ------------------------------------------------------------------ layout

def test_layout_is_deterministic_and_complete():
    g = fixture_graph()
    a, b = model.layout(g), model.layout(json.loads(json.dumps(g)))
    assert a == b
    drawn = {n["name"] for n in g["nodes"] if n["kind"] != "obs"}
    assert set(a["nodes"]) == drawn
    assert {e["id"] for e in a["edges"]} == {e["id"] for e in g["edges"]}
    lane = {it.get("graph_node") for it in a["lane"]["items"]}
    assert {"prometheus", "grafana"} <= lane


def test_layout_zones_left_to_right_and_no_overlap():
    L = model.layout(fixture_graph())
    x = {n: v["x"] for n, v in L["nodes"].items()}
    assert x["client"] < x["n8n-main"] < x["redis"] < x["mock-api"]
    assert L["nodes"]["n8n-main"]["zone"] == "app" and L["nodes"]["postgres"]["zone"] == "data"
    boxes = list(L["nodes"].values())
    for i, p in enumerate(boxes):
        for q in boxes[i + 1:]:
            assert p["x"] + p["w"] <= q["x"] or q["x"] + q["w"] <= p["x"] or p["y"] + p["h"] <= q["y"] or q["y"] + q["h"] <= p["y"]
    logical = [e for e in L["edges"] if e["logical"]]
    assert [e["id"] for e in logical] == ["edge:n8n-main->n8n-worker"] and "via redis" in logical[0]["label"]


def test_layout_places_unknown_node_by_kind():
    g = fixture_graph()
    g["nodes"].append({"name": "cache", "kind": "queue"})
    L = model.layout(g)
    assert L["nodes"]["cache"]["zone"] == "data"


# ------------------------------------------------------------------ derived tables

def test_candidates_have_no_indistinguishable_rows():
    catalog = sources.load_catalog()
    scored = [{"target": "node:redis", "fault": "redis_latency", "rung": r, "tier": 2, "score": 0.75, "untested": 1}
              for r in ("l100ms", "l500ms", "l2000ms")]
    scored.append({"target": "node:redis", "fault": "redis_pod_kill", "rung": "ungraceful", "tier": 3, "score": 0.9, "untested": 1})
    rows = model.candidates_table(scored, catalog, history=[], chosen={("node:redis", "redis_latency", "l100ms")})
    keys = [(r["target"], r["fault"], r["variant"]) for r in rows]
    assert len(keys) == len(set(keys))
    assert rows[0]["status"] == "Allowed" and rows[0]["chosen"]
    kill = next(r for r in rows if r["fault"] == "redis_pod_kill")
    assert kill["status"] == "Blocked by policy"
    assert any(r["status"] == "Not enabled" for r in rows)
    order = [r["status"] for r in rows]
    assert order == sorted(order, key=["Allowed", "Blocked by policy", "Not enabled"].index)


def test_fate_rows_mute_zero_and_flag_bad():
    f = model.fate_rows({"completed_once": 7, "lost_silently": 1, "duplicated": 0},
                        [{"fate": "lost_silently", "workflow": "W2", "request_id": "abc"}])
    rows = {r["fate"]: r for r in f["rows"]}
    assert f["total"] == 8
    assert rows["completed_once"]["bad"] is False and rows["duplicated"]["muted"] and not rows["duplicated"]["bad"]
    assert rows["lost_silently"]["bad"] and rows["lost_silently"]["request_ids"] == ["W2 abc"]


def test_redact():
    line = "N8N_ENCRYPTION_KEY=abc123 password=hunter2 postgres://u:pw@host/db Authorization: Bearer abcdefghijkl"
    out = model.redact(line)
    for secret in ("abc123", "hunter2", ":pw@", "abcdefghijkl"):
        assert secret not in out


def test_workflow_strip_silent_w2():
    g = fixture_graph()
    states = {"n8n-main": "Healthy", "n8n-worker": "Degraded", "redis": "Healthy", "postgres": "Healthy", "mock-api": "Healthy"}
    sample = {"load_on": True, "w1_err_rate": 0.0, "w2_err_rate": 0.0, "w1_p95": 5000}
    wf = {w["name"]: w for w in model.workflow_strip(g, states, sample, {"w1": {"p95_ms": 5000}}, "n8n-worker")}
    assert wf["W2"]["text"].startswith("silent") and wf["W2"]["expected"] == "silent"
    assert wf["W1"]["expected"] == "client_visible"
    off = model.workflow_strip(g, states, {"load_on": False}, None, None)
    assert all("load generator off" in w["text"] for w in off)


def test_llm_source_labels():
    assert model.llm_source("llm", [{"source": "api", "model": "gemini-3.8-flash"}])["text"] == "LLM (Gemini, gemini-3.8-flash)"
    assert model.llm_source("llm", [{"source": "cache", "model": "ministral-14b-latest"}])["kind"] == "cached"
    fb = model.llm_source("fallback:MISTRAL_API_KEY not set", [{"result": "no_api_key"}])
    assert fb["text"].startswith("rule-based ranking") and fb["why"] == "no API key configured"


def test_llm_source_parses_new_source_format():
    assert model.llm_source("llm:cache:ministral-14b-latest", [])["text"] == "LLM (Mistral, ministral-14b-latest), cached answer"


def test_alert_banner_groups_and_labels():
    items = [{"kind": "llm_fallback", "component": "Agent 1 (planner)", "reason": "MISTRAL_MODEL not set", "ts": 10},
             {"kind": "llm_fallback", "component": "Agent 1 (planner)", "reason": "MISTRAL_MODEL not set", "ts": 20},
             {"kind": "error", "component": "probe", "reason": "token=abc123 refused", "ts": 15}]
    b = model.alert_banner(items)
    assert [g["label"] for g in b] == ["LLM NOT USED", "HARNESS ERROR"]
    assert b[0]["count"] == 2 and b[0]["last_ts"] == 20
    assert "abc123" not in b[1]["reason"]


# ------------------------------------------------------------------ API responses (replay of a recorded run)

@pytest.fixture
def replay_hub(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "REPORTS", FIX / "runs")
    monkeypatch.setattr(hub_mod, "REPORTS", FIX / "runs")
    monkeypatch.setattr(sources, "LOADGEN_DB", FIX / "runs" / "missing.sqlite")
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(fixture_graph()))
    monkeypatch.setattr(hub_mod, "GRAPH_JSON", graph_file)
    src = sources.ReplaySources("r1010-214152")
    h = hub_mod.Hub(src)
    return src, h


def _tick_at(src, h, monkeypatch, t):
    monkeypatch.setattr(src, "now", lambda: t)
    monkeypatch.setattr(src, "loop_index", lambda: 0)
    h.tick()


def test_api_shapes_mid_experiment(replay_hub, monkeypatch):
    src, h = replay_hub
    for dt in range(0, 12):
        _tick_at(src, h, monkeypatch, src.t_inject + dt)
    st = h.api_state()
    for key in ("now", "mode", "sources", "campaign", "kill_switch", "loadgen", "phases", "experiment", "workflows", "alerts"):
        assert key in st
    assert st["mode"] == "replay" and st["replay"]["run_id"] == "r1010-214152"
    x = st["experiment"]
    assert x["active"] and x["target"] == "n8n-worker" and x["blast_radius"] == ["n8n-main"]
    assert x["pod"] == "n8n-worker-6cd6854ff5-8nqkn"           # from the recorded Chaos Mesh event
    assert x["in_flight"]["total"] == 8
    assert len(st["phases"]["steps"]) == 8
    comps = h.api_components()
    assert set(comps["nodes"]) >= {"client", "n8n-main", "n8n-worker", "redis", "postgres", "mock-api"}
    for n in comps["nodes"].values():
        assert {"state", "why", "ready", "desired", "pods", "restarts_delta", "metric"} <= set(n)
    m = h.api_metrics(60)
    assert m["points"] and set(m["points"][0]) >= {"t", "w1_ok", "w1_p95", "w2_ok", "q_wait", "q_active", "dbsize", "recon_gap"}
    assert all(p["w1_ok"] is None for p in m["points"])         # no loadgen data: no fabricated zeros
    g = h.api_graph()
    assert g["graph"] and g["layout"]["nodes"]
    assert h.api_experiment("current", None)["fates"]["total"] == 0   # fates not written yet at this time


def test_api_after_recovery_matches_evidence(replay_hub, monkeypatch):
    src, h = replay_hub
    evidence = json.loads((FIX / "runs" / "r1010-214152" / "evidence" / "exp-01.json").read_text())
    _tick_at(src, h, monkeypatch, src.end - 1)
    st = h.api_state()
    assert st["experiment"] is None and st["last_experiment"]["recovery_time_s"] == evidence["recovery_time_s"]
    tl = st["last_experiment"]["timeline"]
    assert tl["recovery_time_s"] == round(evidence["timeline"]["t_slis_recovered"] - 0, 1)
    e = h.api_experiment("current", None)
    summary = evidence["fates"]["summary"]
    assert {r["fate"]: r["count"] for r in e["fates"]["rows"]} == {k: summary.get(k, 0) for k in model.FATE_MEANING}
    assert e["diagnosis"]["root_cause"] and e["grade"]["root_cause_correct"] is True
    assert e["diagnosis"]["source"]["provider"] == "Gemini"         # what the journal recorded, not a hardcoded label
    a1 = h.api_agent1()
    assert a1["available"] and a1["chosen"]["fault"] == "worker_pod_kill"
    events = h.api_events(0)["events"]
    assert {e["source"] for e in events} >= {"k8s", "harness", "safety"}


# ------------------------------------------------------------------ static page

def test_page_is_offline_writes_only_through_agent_controls_and_invents_nothing():
    """No generated numbers, nothing loaded from another host. Besides the advisory chat, the only writes are the
    agent controls in agents.js (Agent 1 plan/approve, kill switch, traffic, Agent 2), all sent with the
    X-Chaosctl header; no endpoint injects or applies anything directly. Controls have readable text labels."""
    static = ROOT / "chaosctl" / "dash" / "static"
    files = [f for f in static.rglob("*") if f.is_file()]
    text = {f.name: f.read_text() for f in files}
    for name, t in text.items():
        assert "Math.random" not in t, name
        assert not re.search(r"(src|href)=[\"']https?://", t), f"{name} loads from another host"
        assert not re.search(r"import .* from [\"']https?://", t), name
    js = "".join(t for n, t in text.items() if n.endswith(".js") and n != "agents.js")
    assert re.findall(r'method: "(\w+)"', js) == ["POST"] and '"/api/chat"' in js
    assert not re.search(r"/api/(inject|kill|apply|delete|agent1|agent2|traffic|run/\S+/(start|stop))", js)
    ag = text["agents.js"]
    assert re.findall(r'method: "(\w+)"', ag) == ["POST"] and '"X-Chaosctl": "1"' in ag
    assert set(re.findall(r'"(/api/[\w/]+)"', ag)) == {"/api/agent1/plan", "/api/agent1/approve", "/api/agent1/cancel",
                                                       "/api/kill", "/api/kill/reset", "/api/traffic",
                                                       "/api/agent2/investigate", "/api/agent2/pause"}
    assert not re.search(r"/api/(inject|apply|delete)", ag)


# ------------------------------------------------------------------ resilience panel (moved from test_kb_report.py)

def test_dashboard_reads_newest_report(tmp_path):
    from chaosctl import report
    from tests.test_kb_report import make_run
    assert sources.find_resilience(tmp_path / "reports") is None                 # no report yet
    exps = report.load_run(make_run(tmp_path))
    report.write(exps, tmp_path / "reports" / "r1010-163348" / "findings.md", use_llm=False)
    r = sources.find_resilience(tmp_path / "reports")
    assert r["path"] == "reports/r1010-163348/findings.json" and r["ranking"]["experiments"][0]["rank"] == 1
    assert r["overall"]["score"] == r["ranking"]["experiments"][0]["score"]
    assert sources.find_resilience(tmp_path / "reports", only_run="r9999-000000") is None


def test_report_step_lights_up_only_when_run_is_reported():
    ev = [{"event": "experiment_end", "ts": 1}, {"event": "inject", "ts": 0}]
    assert model.phase_strip(ev, 2, True, True, True, reported=False)["steps"][7]["status"] == "todo"
    assert model.phase_strip(ev, 2, True, True, True, reported=True)["steps"][7]["status"] == "done"
