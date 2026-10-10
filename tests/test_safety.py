import time

import pytest

from chaosctl import safety
from chaosctl.journal import Journal
from chaosctl.safety import SafetyController, State, Thresholds, recovery_watchdog


def ctl(tmp_path, sample=None, th=None):
    aborts = []
    c = SafetyController(sample_fn=sample or (lambda: {}), on_abort=aborts.append,
                         journal=Journal(tmp_path / "j.jsonl"), kill_file=tmp_path / "KILL", tick_s=0.05)
    c.arm(th or Thresholds(allowed_unready=["n8n-worker", "n8n-main"]))
    for s in (State.PLANNED, State.PREFLIGHT_OK, State.WAIT_IN_FLIGHT, State.INJECTING, State.OBSERVING):
        c.transition(s)
    return c, aborts


def healthy():
    return {"queue": {"reachable": True, "wait": 5}, "w1": {"n": 10, "error_rate": 0.0, "p95_ms": 4500},
            "pods": {"n8n-worker": {"ready": True}, "postgres": {"ready": True}}}


def test_no_abort_when_healthy(tmp_path):
    c, _ = ctl(tmp_path)
    assert c.check({**healthy(), "ts": time.time()}) is None


@pytest.mark.parametrize("patch,needle", [
    ({"queue": {"reachable": True, "wait": 900}}, "queue_waiting"),
    ({"w1": {"n": 10, "error_rate": 0.9, "p95_ms": 100}}, "w1_error_rate"),
    ({"w1": {"n": 10, "error_rate": 0.0, "p95_ms": 30000}}, "w1_p95_ms"),
    ({"pods": {"postgres": {"ready": False}}}, "blast_radius_breach"),
])
def test_abort_reasons(tmp_path, patch, needle):
    c, _ = ctl(tmp_path)
    assert needle in c.check({**healthy(), **patch, "ts": time.time()})


def test_target_unready_is_allowed(tmp_path):
    c, _ = ctl(tmp_path)
    s = {**healthy(), "pods": {"n8n-worker": {"ready": False}}, "ts": time.time()}
    assert c.check(s) is None


def test_max_total_cap(tmp_path):
    c, _ = ctl(tmp_path, th=Thresholds(max_total_s=10))
    assert "max_total_s" in c.check({**healthy(), "ts": c.t_inject + 11})


def test_error_rate_arming_and_tier3(tmp_path):
    c, _ = ctl(tmp_path, th=Thresholds(error_rate_armed_after_s=90))
    bad = {**healthy(), "w1": {"n": 10, "error_rate": 0.9}}
    assert c.check({**bad, "ts": c.t_inject + 10}) is None
    assert c.check({**bad, "ts": c.t_inject + 95}) is not None
    c3, _ = ctl(tmp_path, th=Thresholds(error_rate_abort=False))
    assert c3.check({**bad, "ts": c3.t_inject + 95}) is None


def test_loop_aborts_once_and_on_kill_file(tmp_path):
    c, aborts = ctl(tmp_path, sample=lambda: {**healthy(), "queue": {"reachable": True, "wait": 10_000}})
    c.start(); time.sleep(0.3); c.stop()
    assert len(aborts) == 1 and "queue_waiting" in aborts[0] and c.state == State.ABORTING

    c2, aborts2 = ctl(tmp_path, sample=healthy)
    c2.start(); time.sleep(0.15)
    assert aborts2 == []
    (tmp_path / "KILL").touch()
    time.sleep(0.2); c2.stop()
    assert aborts2 == ["kill_switch"]


def test_kill_method(tmp_path):
    c, aborts = ctl(tmp_path, sample=healthy)
    c.start(); c.kill("sigint"); time.sleep(0.2); c.stop()
    assert aborts == ["kill_switch"]


def test_illegal_transition(tmp_path):
    c = SafetyController(lambda: {}, lambda r: None, kill_file=tmp_path / "K")
    with pytest.raises(RuntimeError):
        c.transition(State.INJECTING)


def test_thresholds_cannot_be_loosened():
    default = Thresholds(w1_error_rate=0.5, queue_waiting=500, max_total_s=180)
    loose = Thresholds(w1_error_rate=0.99, queue_waiting=10_000, max_total_s=9999)
    t = default.tightened(loose)
    assert (t.w1_error_rate, t.queue_waiting, t.max_total_s) == (0.5, 500, 180)
    strict = default.tightened(Thresholds(queue_waiting=1))
    assert strict.queue_waiting == 1


def test_watchdog_ready_and_restart(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    r = recovery_watchdog(lambda: {"redis": {"ready": True}}, ["redis"], 1, lambda n: None, j, poll_s=0.01)
    assert r["recovered"] and not r["manual_action_needed"]

    state = {"ready": False}
    restarted = []

    def restart(n):
        restarted.append(n); state["ready"] = True

    r = recovery_watchdog(lambda: {"redis": dict(state)}, ["redis"], 0.05, restart, j, restart_grace_s=1, poll_s=0.01)
    assert restarted == ["redis"] and r["recovered"] and r["manual_action_needed"]
