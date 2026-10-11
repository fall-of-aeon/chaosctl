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


def test_evidence_ids_with_appended_details_map_to_the_allowed_id():
    from chaosctl.agents import diagnosis as dg
    allowed = {"ev:trace", "ev:baseline", "ev:timeline", "ev:logs:n8n-worker", "det:queue_backlog"}
    assert dg.canonical_evidence("ev:trace: w1_ok=0 after 10.9s", allowed) == "ev:trace"
    assert dg.canonical_evidence("ev:baseline.w2.ok=100", allowed) == "ev:baseline"
    assert dg.canonical_evidence("ev:logs:n8n-worker: ECONNRESET", allowed) == "ev:logs:n8n-worker"
    assert dg.canonical_evidence("ev:trace", allowed) == "ev:trace"
    assert dg.canonical_evidence("ev:traceback", allowed) is None          # not a boundary: unknown, not ev:trace
    assert dg.canonical_evidence("ev:logs:redis", allowed) is None
    d = dg.Diagnosis(thoughts=[], root_cause="x", failure_chain=[], fixes=[],
                     findings=[dg.Finding(title="a", evidence=["ev:trace: w2_ok=5", "ev:trace: q_wait=40", "ev:nope"]),
                               dg.Finding(title="b", evidence=["ev:nope"])])
    dg.normalize_evidence(d, allowed)
    assert d.findings[0].evidence == ["ev:trace"]
    assert dg.validate(d, allowed, quiet=True) == ["finding cites unknown evidence ['ev:nope']"]   # b had nothing valid


def test_diagnosis_retries_once_with_feedback_before_falling_back(monkeypatch):
    from chaosctl.agents import diagnosis as dg
    calls = []
    def fake(purpose, prompt, model_cls, journal=None, meta=None, **kw):
        calls.append(prompt)
        ev = ["ev:made_up"] if len(calls) == 1 else ["ev:trace"]
        fix = next(iter(dg.PLAYBOOK))
        return dg.Diagnosis(thoughts=[], root_cause="worker down", failure_chain=[],
                            findings=[dg.Finding(title="t", evidence=ev)], fixes=[dg.FixPick(playbook_id=fix, why="w")])
    monkeypatch.setattr(dg.llm, "generate_json", fake)
    out = dg.diagnose({"trace": [], "logs": {}, "events": []})
    assert len(calls) == 2 and "REJECTED" in calls[1]
    assert out["llm_used"] is True and out["root_cause"] == "worker down"
