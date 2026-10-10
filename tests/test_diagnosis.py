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
