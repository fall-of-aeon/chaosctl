from chaosctl.agents import diagnosis as d


def bundle():
    trace = [{"t": t, "w1": {"n": 4, "ok": 4, "p95_ms": 30000 if t > 0 else 4700, "success_rate": 1},
              "w2": {"n": 4, "ok": 4, "success_rate": 1.0},
              "queue": {"reachable": True, "wait": 40 if t > 0 else 0, "active": 0 if t > 0 else 8, "dbsize": 50},
              "pods": {"n8n-main": {"ready": True}}} for t in range(-3, 10)]
    return {"trace": trace, "baseline": {"w1": {"p95_ms": 4700}, "queue": {"wait_max": 1}},
            "fates": {"summary": {"lost_silently": 1}}, "timeline": {}, "restarts_delta": {},
            "events": [{"object": "chaosctl-r1-worker-pod-kill", "message": "Successfully apply chaos", "reason": "Applied"},
                       {"object": "n8n-worker-1", "message": "Stopping container", "reason": "Killing"}]}


def test_detectors_and_blindness():
    b = d.blind(bundle())
    assert [e["object"] for e in b["events"]] == ["n8n-worker-1"]
    ids = {h["id"] for h in d.detect(b)}
    assert {"consumers_down", "silent_async_failure", "lost_work", "detectability_gap", "latency_degradation"} <= ids
    assert "queue_state_lost" not in ids


def test_fallback_only_playbook_fixes_and_validator():
    out = d.diagnose(bundle(), use_llm=False)
    assert out["fixes"] and all(f["playbook_id"] in d.PLAYBOOK for f in out["fixes"])
    bad = d.Diagnosis(thoughts=[], root_cause="x", failure_chain=[], findings=[],
                      fixes=[d.FixPick(playbook_id="rm -rf /", why="x")])
    assert d.validate(bad, set())


def g(root, target, fault="x"):
    return d.grade({"root_cause": root, "failure_chain": []}, {"target": f"node:{target}", "fault": fault})["root_cause_correct"]


def test_grader_is_strict_on_real_llm_answers():
    # these two were graded CORRECT by the old substring grader
    assert not g("Postgres disk full (ev:logs:postgres-0) blocked order writes, causing silent failures for W1 "
                 "clients. Worker restarts followed.", "n8n-worker", "worker_container_kill")
    assert not g("Postgres disk full caused job write failures, stalling n8n workers and degrading W1 latency while "
                 "W2 clients remained unaware", "n8n-main", "traffic_spike")      # "remained" is not "main"
    assert g("The n8n worker container crashed and its active jobs were lost", "n8n-worker")
    assert g("Postgres disk full (No space left on device) delayed order writes", "postgres", "postgres_disk_full")
    assert g("A burst of incoming traffic overloaded the queue and workers", "n8n-main", "traffic_spike")
    assert g("The mock downstream API returned HTTP 500 errors", "mock-api", "mockapi_http_500")


def test_quiet_run_may_have_no_fix():
    quiet = [{"id": "latency_degradation", "severity": "Medium", "detail": "x"}]
    nofix = d.Diagnosis(thoughts=[], root_cause="no real degradation", failure_chain=[], findings=[], fixes=[])
    assert d.validate(nofix, set(), quiet=d.is_quiet(quiet)) == []
    assert d.validate(nofix, set(), quiet=False) == ["no fixes"]


def test_no_impact_runs_are_not_graded():
    diag = {"root_cause": "No degradation detected", "failure_chain": []}
    plan = {"target": "node:postgres", "fault": "postgres_overload"}
    assert d.grade(diag, plan, {"timeline": {"saw_symptom": False}})["verdict"] == "NO_IMPACT"
    assert d.grade(diag, plan, {"timeline": {"saw_symptom": True}})["verdict"] == "MISSED"

    lost = {"timeline": {"saw_symptom": False}, "fates": {"summary": {"lost_silently": 4}}}
    assert d.grade(diag, plan, lost)["verdict"] == "MISSED"          # silent loss is impact
