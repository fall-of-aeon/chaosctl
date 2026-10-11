"""Live agents: Agent 2's tool-using investigation, the always-on watcher, the dashboard's control endpoints and the
campaign's human-approved (preplanned) path."""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from chaosctl import llm
from chaosctl.agents import investigator as inv
from chaosctl.dash import watcher as wmod

NOW = 10_000.0


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path)
    monkeypatch.setenv("MISTRAL_MODEL", "m")
    monkeypatch.delenv("MISTRAL_FALLBACK_MODEL", raising=False)
    monkeypatch.setattr(llm, "budget", llm._Budget())


def points(worker_down_from=None, n=120):
    """Hub-style 1 s points: steady load, then (optionally) the queue backs up with nothing completing."""
    out = []
    for i in range(n):
        t = NOW - n + i
        bad = worker_down_from is not None and i >= worker_down_from
        out.append({"t": t, "load_on": True, "w1_ok": 0 if bad else 1.0, "w1_err": 0, "w1_p95": 25000 if bad else 4100,
                    "w2_ok": 1.0, "w2_err": 0, "redis_up": True, "q_wait": (i - worker_down_from) * 2 if bad else 0,
                    "q_active": 0 if bad else 2, "dbsize": 40,
                    "pods": {"n8n-worker": [["n8n-worker-x", "Running", not bad, 0]], "redis": [["redis-0", "Running", True, 0]]}})
    return out


def toolkit(pts=None, events=None, logs=None):
    pods = {"n8n-worker": [{"name": "n8n-worker-x", "phase": "Running", "ready": False, "restarts": 1, "created": NOW - 20}],
            "redis": [{"name": "redis-0", "phase": "Running", "ready": True, "restarts": 0, "created": NOW - 900}]}
    return inv.ToolKit(pods=lambda: pods, events=lambda: events or [], logs=lambda s, since: logs or [],
                       redis=lambda: {"reachable": True, "wait": 40, "active": 0, "dbsize": 40},
                       postgres=lambda: {"reachable": True, "recon_gap": 0, "recon_checked": 50},
                       points=lambda: pts if pts is not None else points(worker_down_from=90),
                       baseline=lambda: {"w1": {"p95_ms": 4100, "success_rate": 1.0}, "queue": {"wait_max": 2}},
                       graph=lambda: {"nodes": [{"name": "n8n-worker", "spof": True, "replicas": 1, "blast_radius": {"nodes": ["node:n8n-main"]}}],
                                      "edges": [{"from": "n8n-worker", "to": "redis", "flow": "dequeue", "mode": "async"}]},
                       downstream=lambda: [{"status": 200, "ms": 4}], now=lambda: NOW)


def scripted_llm(steps, diagnosis):
    """Answers Step prompts from `steps` in order, then the Diagnosis prompt. Records the prompts."""
    seen = {"step": [], "conclude": []}
    it = iter(steps)

    def call(model, prompt, schema):
        if schema.get("title") == "Step":
            seen["step"].append(prompt)
            return json.dumps(next(it))
        seen["conclude"].append(prompt)
        return json.dumps(diagnosis)
    return call, seen


def step(action, kind="check", service="none", since=120, thought="t"):
    return {"kind": kind, "thought": thought, "action": action, "service": service, "since_s": since}


GOOD_DIAG = {"thoughts": [{"step": "observe", "text": "0 active, 40 waiting"}, {"step": "conclude", "text": "worker gone"}],
             "root_cause": "The n8n-worker pod was not Ready, so nothing consumed the Redis queue.",
             "failure_chain": ["worker not Ready", "queue grows", "W1 times out"],
             "findings": [{"title": "consumers down", "evidence": ["tool:1", "consumers_down"]}],
             "fixes": [{"playbook_id": "scale_workers", "why": "a second worker keeps consuming"}]}


# ------------------------------------------------------------------ Agent 2 investigation

TRIAGE = ["pod_status", "client_slis", "queue_stats", "recent_events", "database_check"]


def test_investigation_triage_then_targeted_tools_then_concludes_blind():
    call, seen = scripted_llm([step("service_logs", service="n8n-worker"), step("conclude", "conclude")], GOOD_DIAG)
    updates = []
    out = inv.investigate("- n8n-worker is Down: no Ready pod", toolkit(logs=["error: lost connection", "chaos-daemon injected"]),
                          llm_call=call, on_step=lambda u: updates.append(len(u["steps"])))
    assert [s["action"] for s in out["steps"]] == TRIAGE + ["service_logs"]
    assert [s["by"] for s in out["steps"]] == ["triage"] * 5 + ["llm"]
    assert len(seen["step"]) == 2                         # the triage costs no LLM turn
    assert out["llm_used"] and out["source"].startswith("llm:api")
    assert out["diagnosis"]["root_cause"].startswith("The n8n-worker")
    assert updates[:2] == [5, 6]                          # triage streamed at once, then every follow-up
    logs = out["steps"][5]["result"]["lines"]
    assert logs == ["error: lost connection"]             # the injector's own lines never reach the agent
    # the triage results are fed into the first step's prompt
    assert "tool:1" in seen["step"][0] and "n8n-worker-x" in seen["step"][0]
    assert "consumers_down" in seen["conclude"][0]        # detectors run on the live points


def test_triage_tools_run_in_parallel():
    tk = toolkit()
    slow = {}
    for name in ("pods", "redis", "events", "points"):
        f = tk.g[name]
        slow[name] = (lambda f: (lambda *a: (time.sleep(0.3), f(*a))[1]))(f)
    tk.g.update(slow)
    call, _ = scripted_llm([step("conclude", "conclude")], GOOD_DIAG)
    t0 = time.time()
    out = inv.investigate("- n8n-worker is Down", tk, llm_call=call)
    assert [s["action"] for s in out["steps"]] == TRIAGE
    assert out["steps"][0]["took_s"] < 0.9                 # 5 tools x 0.3 s sequentially would be >= 1.5 s
    assert time.time() - t0 < 2.5


def test_conclusion_must_cite_its_own_tool_calls_and_the_playbook():
    bad = dict(GOOD_DIAG, findings=[{"title": "x", "evidence": ["tool:9"]}], fixes=[{"playbook_id": "reboot_the_world", "why": "y"}])
    call, _ = scripted_llm([step("pod_status", "observe"), step("conclude", "conclude")], bad)
    out = inv.investigate("- n8n-worker is Down", toolkit(), llm_call=call)
    assert not out["llm_used"] and out["source"].startswith("fallback:invalid")
    assert all(f["playbook_id"] in inv.dg.PLAYBOOK for f in out["diagnosis"]["fixes"])


def test_repeated_call_is_replaced_and_followups_are_bounded():
    call, seen = scripted_llm([step("pod_status", "observe")] * 20, GOOD_DIAG)
    out = inv.investigate("- n8n-worker is Down", toolkit(), llm_call=call, max_steps=2)
    acts = [s["action"] for s in out["steps"]]
    assert acts[:5] == TRIAGE and len(acts) == 6 and len(set(acts)) == 6   # pod_status was already in the triage
    assert len(seen["step"]) == 2


def test_conclude_right_after_triage_is_allowed():
    call, seen = scripted_llm([step("conclude", "conclude")], GOOD_DIAG)
    out = inv.investigate("- redis is Down", toolkit(), llm_call=call)
    assert [s["action"] for s in out["steps"]] == TRIAGE
    assert len(seen["step"]) == 1 and len(seen["conclude"]) == 1


def test_llm_down_falls_back_to_fixed_sequence_and_says_so():
    from chaosctl import alerts

    def call(*a):
        raise TimeoutError()
    out = inv.investigate("- n8n-worker is Down", toolkit(), llm_call=call)
    rest = [a for a, _ in inv.DEFAULT_SEQUENCE if a not in TRIAGE]
    assert [s["action"] for s in out["steps"]] == TRIAGE + rest
    assert [s["by"] for s in out["steps"]] == ["triage"] * 5 + ["rule"] * len(rest)
    assert not out["llm_used"]
    assert "consumers_down" in {h["id"] for h in out["detectors"]}
    assert any(a["component"] == "Agent 2 (live investigation)" for a in alerts.seen())


def test_tools_clamp_and_report_errors():
    tk = toolkit()
    assert tk.run("service_logs", "not-a-service")["error"].startswith("service must be")
    assert tk.run("queue_stats", since_s=99999)["window_s"] == 600
    tk.g["pods"] = lambda: (_ for _ in ()).throw(RuntimeError("api down"))
    assert tk.run("pod_status")["error"] == "RuntimeError: api down"
    quiet = toolkit(pts=points()).run("run_detectors")
    assert quiet["hits"] == []


# ------------------------------------------------------------------ watcher

class FakeHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.components = {"nodes": {}}
        self.points, self.graph, self.src = [], None, None

    def set(self, **states):
        self.components = {"nodes": {n: {"name": n, "kind": "app", "state": s, "why": w} for n, (s, w) in states.items()}}


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    monkeypatch.setattr(wmod, "INCIDENTS", tmp_path / "incidents")
    monkeypatch.setattr(wmod, "STATE", tmp_path)
    monkeypatch.setattr(wmod, "REPORTS", tmp_path / "reports")
    calls = []

    def fake(trigger, tools, **kw):
        calls.append(trigger)
        kw["on_step"]({"phase": "step", "steps": [{"n": 1}]})
        return {"steps": [{"n": 1}], "source": "llm:api:m", "llm_used": True, "detectors": [],
                "diagnosis": {"root_cause": "worker down", "fixes": [{"playbook_id": "scale_workers", "why": "w"}]}}
    hub = FakeHub()
    w = wmod.Agent2Watcher(hub, investigate=fake)
    w._toolkit = lambda: None
    w.calls = calls
    return hub, w


def wait_done(w):
    for _ in range(100):
        if w.current and w.current["status"] != "investigating":
            return
        time.sleep(0.02)


def test_watcher_triggers_after_three_bad_ticks_once_per_incident(watcher):
    hub, w = watcher
    hub.set(**{"n8n-worker": ("Down", ["no Ready pod"]), "redis": ("Healthy", [])})
    w.tick(1.0); w.tick(2.0)
    assert w.view()["state"] == "suspicious" and not w.calls
    w.tick(3.0)
    wait_done(w)
    assert w.calls == ["- n8n-worker is Down: no Ready pod"]
    assert w.current["status"] == "concluded" and w.current["diagnosis"]["fixes"][0]["title"]
    for t in range(4, 20):                 # same incident still going: no second investigation
        w.tick(float(t))
    assert len(w.calls) == 1 and w.view()["state"] == "concluded"
    hub.set(**{"n8n-worker": ("Healthy", [])})
    w.tick(100.0)
    assert w.view()["state"] == "re-arming"
    w.tick(100.0 + wmod.REARM_S)
    assert w.view()["state"] == "watching"
    saved = list(wmod.INCIDENTS.glob("inc-*.json"))
    assert len(saved) == 1


def test_watcher_ignores_configuration_reasons_and_can_pause(watcher):
    hub, w = watcher
    hub.set(**{"mock-api": ("Degraded", ["mock error rate 20%"])})
    for t in range(10):
        w.tick(float(t))
    assert not w.calls                     # a harness setting is not a symptom (and would name the injection)
    w.enabled = False
    hub.set(**{"redis": ("Down", ["Redis probe unreachable"])})
    for t in range(10, 20):
        w.tick(float(t))
    assert not w.calls and w.view()["state"] == "paused"


def test_manual_investigation_when_healthy(watcher):
    hub, w = watcher
    hub.set(**{"redis": ("Healthy", [])})
    rec = w.manual()
    wait_done(w)
    assert rec["by"] == "operator" and "health check" in w.calls[0]


# ------------------------------------------------------------------ server: control endpoints

def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, body, header=True):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-Chaosctl": "1"} if header else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_control_posts_need_the_header_and_live_mode():
    from chaosctl.dash.server import make_handler

    class Ctl:
        def __init__(self):
            self.planned = []

        def plan(self, exclude):
            self.planned.append(exclude)
            return {"state": "planning"}

    ctl = Ctl()
    srv, base = serve(make_handler(hub=None, control=ctl, watcher=object()))
    try:
        assert post(base + "/api/agent1/plan", {}, header=False)[0] == 403      # a page on another site cannot do this
        assert not ctl.planned
        assert post(base + "/api/agent1/plan", {"exclude": ["redis_pod_kill"]}) == (200, {"state": "planning"})
        assert ctl.planned == [["redis_pod_kill"]]
        assert post(base + "/api/nope", {})[0] == 404
    finally:
        srv.shutdown()
    srv, base = serve(make_handler(hub=None))
    try:
        assert post(base + "/api/kill", {})[0] == 409                         # replay mode: no controls
    finally:
        srv.shutdown()


def test_control_refusals_are_shown_not_raised(tmp_path, monkeypatch):
    from chaosctl.dash import control as cmod

    monkeypatch.setattr(cmod, "PROPOSALS", tmp_path / "p")
    monkeypatch.setattr(cmod, "STATE", tmp_path)
    monkeypatch.setattr(cmod, "ROOT", tmp_path)

    class Src:
        mode = "live"
    c = cmod.Control(type("H", (), {"src": Src()})())
    with pytest.raises(cmod.ControlError, match="no such proposal"):
        c.approve("abc")
    (tmp_path / "KILL").touch()
    assert any("kill switch" in b for b in c.blockers()) and any("baseline" in b for b in c.blockers())
    c.state, c.proposal = "proposed", {"proposal_id": "abc", "item": {}}
    with pytest.raises(cmod.ControlError, match="kill switch"):
        c.approve("abc")
    assert c.view()["state"] == "proposed"


# ------------------------------------------------------------------ campaign: the approved plan runs as approved

def test_campaign_runs_exactly_the_approved_plan(monkeypatch):
    from chaosctl.agents import campaign
    from chaosctl.journal import NullJournal

    class Agent:
        def invoke(self, s):
            raise AssertionError("must not re-plan an approved experiment")
    monkeypatch.setattr(campaign, "build_injection_agent", lambda *a, **k: Agent())
    monkeypatch.setattr(campaign, "load_catalog", lambda: {"faults": {}, "targets": {}})
    monkeypatch.setattr(campaign, "load_history", lambda: [])
    monkeypatch.setattr(campaign, "save_history", lambda h: None)
    monkeypatch.setattr(campaign, "runtime_guard", lambda item, v, *a: v)
    ran = []

    class Runner:
        ctl = None
        run_id = "r1"

        def run_experiment(self, plan, guard):
            ran.append((plan["fault"], plan["target"], guard["verdict"]))
            return {"exp_id": plan["id"], "fault": plan["fault"], "target": plan["target"], "status": "completed"}

    class G:
        nodes, edges = {}, []
    item = {"id": "exp-01", "fault": "worker_pod_kill", "target": "node:n8n-worker", "rung": "r1", "start_when": "in_flight>=3",
            "cooldown_s": 60, "reason": "r", "cites": [], "hypothesis": "h"}
    verdict = {"exp_id": "exp-01", "verdict": "approve", "blast_radius": [], "workflows_affected": {}, "abort": {}, "rationale": "ok"}
    camp = campaign.build_campaign(Runner(), lambda: G(), {}, NullJournal())
    out = camp.invoke({"only": None, "max_experiments": 1, "use_llm": True,
                       "preplanned": {"item": item, "verdict": verdict, "source": "llm:api:m", "proposal_id": "p1"}})
    assert ran == [("worker_pod_kill", "node:n8n-worker", "approve")]
    assert out["plan_source"] == "llm:api:m" and out["results"][0]["status"] == "completed"


def test_live_investigation_uses_the_fast_model_and_can_be_switched_back(monkeypatch):
    models = {"Step": [], "Diagnosis": []}

    def call(model, prompt, schema):
        models[schema["title"]].append(model)
        return json.dumps(step("conclude", "conclude") if schema["title"] == "Step" else GOOD_DIAG)
    inv.investigate("- n8n-worker is Down", toolkit(), llm_call=call)
    assert models == {"Step": [inv.STEP_MODEL_DEFAULT], "Diagnosis": [inv.CONCLUDE_MODEL_DEFAULT]}
    monkeypatch.setenv("CHAOSCTL_AGENT2_CONCLUDE_MODEL", "")      # back to MISTRAL_MODEL ("m" in these tests)
    models = {"Step": [], "Diagnosis": []}
    inv.investigate("- n8n-worker is Degraded", toolkit(), llm_call=call)    # new prompt: not answered from the cache
    assert models == {"Step": [inv.STEP_MODEL_DEFAULT], "Diagnosis": ["m"]}



def healthy_toolkit():
    tk = toolkit(pts=points())
    tk.g["pods"] = lambda: {"n8n-worker": [{"name": "w", "phase": "Running", "ready": True, "restarts": 0, "created": NOW - 900}],
                            "redis": [{"name": "redis-0", "phase": "Running", "ready": True, "restarts": 0, "created": NOW - 900}]}
    tk.g["redis"] = lambda: {"reachable": True, "wait": 0, "active": 2, "dbsize": 40}
    return tk


def test_healthy_system_is_reported_at_once_without_an_llm():
    def call(*a):
        raise AssertionError("no LLM call expected for a healthy system")
    out = inv.investigate("- no service is Down or Degraded right now; an operator asked for a health check",
                          healthy_toolkit(), llm_call=call)
    assert out.get("healthy") and not out["llm_used"] and out["source"].startswith("rule:healthy")
    assert [s["action"] for s in out["steps"]] == TRIAGE
    assert out["diagnosis"]["fixes"] == [] and out["diagnosis"]["root_cause"].startswith("No incident found")


def test_one_unready_pod_or_a_silent_loss_is_not_healthy():
    tk = healthy_toolkit()
    tk.g["postgres"] = lambda: {"reachable": True, "recon_gap": 1, "recon_checked": 50}
    call, seen = scripted_llm([step("conclude", "conclude")], GOOD_DIAG)
    out = inv.investigate("- operator check", tk, llm_call=call)
    assert not out.get("healthy") and len(seen["conclude"]) == 1
    assert out["source"].startswith(("llm", "fallback"))



def test_a_down_service_in_the_trigger_always_gets_the_full_investigation():
    # live incident 2026-10-11: the monitor saw mock-api Down (its probe failed) while every triage check was normal
    call, seen = scripted_llm([step("downstream_check", service="mock-api"), step("conclude", "conclude")], GOOD_DIAG)
    out = inv.investigate("- mock-api is Down: mock-api admin probe unreachable", healthy_toolkit(), llm_call=call)
    assert not out.get("healthy") and len(seen["step"]) == 2 and len(seen["conclude"]) == 1
    assert [s["action"] for s in out["steps"]] == TRIAGE + ["downstream_check"]
