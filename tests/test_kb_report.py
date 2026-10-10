import json
import shutil

import pytest
from typer.testing import CliRunner

from chaosctl import cli, llm, report, score
from chaosctl.agents import reporter
from chaosctl.agents import diagnosis as d
from chaosctl.fault_kb import KB_DIR, load_kb

@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    """No test may reach Mistral: no key, isolated cache, fresh budget."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_MODEL", "m")
    monkeypatch.delenv("MISTRAL_FALLBACK_MODEL", raising=False)
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path / "llm")
    monkeypatch.setattr(llm, "budget", llm._Budget())
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)


CATALOG = {"faults": {"worker_pod_kill": {"tier": 1, "enabled": True}, "redis_pod_kill": {"tier": 3, "enabled": True},
                      "pod_failure": {"tier": 1, "test_only": True}, "worker_graceful_kill": {"tier": 1}}}


# ------------------------------------------------------------ fixtures: a run shaped like r1010-163348 (E1 after fix)

def e1_bundle(fates=None, recovery=78.8):
    trace = [{"t": float(t),
              "w1": {"n": 10, "ok": 10 if t < 0 or t > 40 else 6, "p95_ms": 30000 if 0 <= t <= 40 else 6000,
                     "success_rate": 1.0 if t < 0 or t > 40 else 0.6},
              "w2": {"n": 10, "ok": 10, "success_rate": 1.0},
              "queue": {"reachable": True, "wait": 40 if 0 <= t <= 40 else 0, "active": 0 if 0 <= t <= 30 else 8,
                        "dbsize": 60},
              "pods": {"n8n-main": {"ready": True}, "n8n-worker": {"ready": t > 33}}} for t in range(-5, 90, 5)]
    return {"exp_id": "exp-01", "trace": trace, "events": [],
            "baseline": {"rate": 2.0, "duration_s": 120, "w1": {"p95_ms": 6000, "p50_ms": 4100, "success_rate": 1.0},
                         "w2": {"p95_ms": 100, "success_rate": 1.0}, "queue": {"wait_mean": 0.0, "wait_max": 1}},
            "fates": fates if fates is not None else {"summary": {"completed_once": 7, "lost_silently": 0, "duplicated": 0,
                                                                  "phantom_success": 10, "lost_visible": 11}},
            "timeline": {"t_first_symptom": 1.0, "t_pod_ready": 33.2, "t_slis_recovered": recovery,
                         "saw_symptom": True},
            "restarts_delta": {}, "recovery_time_s": recovery, "manual_action_needed": False, "status": "completed"}


def make_run(root, run_id="r1010-163348", fault="worker_pod_kill", target="n8n-worker", diagnosis=True,
             results=True, journal=True, bundle=None):
    rdir = root / "reports" / run_id
    (rdir / "evidence").mkdir(parents=True)
    b = bundle or e1_bundle()
    (rdir / "evidence" / "exp-01.json").write_text(json.dumps(b))
    (rdir / "evidence" / "exp-01.plan.json").write_text(json.dumps({"fault": fault, "target": target, "rung": "ungraceful"}))
    item = {"id": "exp-01", "fault": fault, "target": f"node:{target}", "rung": "ungraceful",
            "reason": "node:n8n-worker is a single point of failure", "cites": ["node:n8n-worker"],
            "hypothesis": "W1 slows down, W2 keeps returning 200"}
    if journal:
        lines = [{"event": "agent1_scored", "candidates": [{"target": f"node:{target}", "fault": fault, "score": 0.86}]},
                 {"event": "agent1_plan", "source": "llm", "plan": {"experiments": [item]},
                  "verdicts": [{"exp_id": "exp-01", "verdict": "approve", "rationale": "tier 1",
                                "abort": {"w1_error_rate": 0.5, "w1_p95_ms": 20000, "queue_waiting": 500,
                                          "max_total_s": 180}}]},
                 {"event": "llm", "purpose": "injection_select", "model": "ministral-14b-latest", "source": "api"},
                 {"event": "experiment_start", "plan": item,
                  "guard": {"verdict": "approve", "blast_radius": ["node:n8n-main"],
                            "abort": {"w1_error_rate": 0.5, "w1_p95_ms": 20000, "queue_waiting": 500, "max_total_s": 180}}}]
        (rdir / "journal.jsonl").write_text("".join(json.dumps({"ts": 1791650000.0 + i, **x}) + "\n"
                                                    for i, x in enumerate(lines)))      # like Journal.write
    if results:
        (rdir / "results.json").write_text(json.dumps([{
            "exp_id": "exp-01", "fault": fault, "target": target, "status": b["status"], "fates": b["fates"],
            "timeline": b["timeline"], "recovery_time_s": b["recovery_time_s"], "manual_action_needed": False,
            "watchdog": {"recovered": True, "manual_action_needed": False},
            "thresholds": {"w1_p95_ms": 20000, "recovery_deadline_s": 120}}]))
    if diagnosis:
        out = d.diagnose(b, use_llm=False)
        (rdir / "evidence" / "exp-01.diagnosis.json").write_text(json.dumps(out))
    return rdir


# ------------------------------------------------------------ knowledge base

def test_kb_is_consistent_with_catalog_playbook_and_detectors():
    kb = load_kb()
    # a catalog fault without a knowledge file is allowed (generic defaults), so the catalog can change freely
    assert [p for p in kb.check(d.DETECTOR_IDS) if "has no knowledge" not in p] == []
    assert kb.resolve("e1") == "worker_pod_kill" and kb.resolve("E2") == "redis_pod_kill"
    assert kb.resolve("worker_pod_kill") == "worker_pod_kill"


def test_new_catalog_fault_without_knowledge_still_works(tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    shutil.copy(KB_DIR / "worker_pod_kill.yaml", kb_dir)
    cat = {"faults": {**CATALOG["faults"], "load_spike": {"tier": 2, "enabled": True}}}
    kb = load_kb(cat, kb_dir)
    spike = kb.get("load_spike")
    assert not spike.documented and spike.title == "load spike" and spike.keywords_for("loadgen") == ["loadgen"]
    problems = kb.check(d.DETECTOR_IDS)
    assert any("load_spike" in p for p in problems) and any("redis_pod_kill" in p for p in problems)


def test_kb_check_flags_bad_references(tmp_path):
    (tmp_path / "x_fault.yaml").write_text(
        "fault: x_fault\nalias: E1\ntitle: X\nsymptoms: {expected: [no_such_detector]}\nrelated_fixes: [no_such_fix]\n")
    shutil.copy(KB_DIR / "worker_pod_kill.yaml", tmp_path)
    problems = load_kb(CATALOG, tmp_path).check(d.DETECTOR_IDS)
    joined = "\n".join(problems)
    assert "not in catalog.yaml" in joined and "no_such_fix" in joined and "no_such_detector" in joined
    assert "alias E1" in joined


def test_file_name_must_match_fault(tmp_path):
    (tmp_path / "a.yaml").write_text("fault: b\ntitle: B\n")
    with pytest.raises(ValueError):
        load_kb(CATALOG, tmp_path)


def test_blind_fallback_root_cause_comes_from_kb_signatures():
    worker = d.fallback(d.detect(d.blind(e1_bundle())))
    assert worker.root_cause == load_kb().get("worker_pod_kill").signature.root_cause
    hits = [{"id": "queue_unreachable", "severity": "Critical", "detail": "x"},
            {"id": "consumers_down", "severity": "High", "detail": "y"}]
    assert "Redis" in d.fallback(hits).root_cause  # higher priority signature wins
    assert "single clear cause" in d.fallback([{"id": "latency_degradation", "severity": "High", "detail": "z"}]).root_cause


def test_grade_uses_kb_keywords():
    diag = {"root_cause": "The queue consumer stopped", "failure_chain": []}
    assert d.grade(diag, {"fault": "worker_pod_kill", "target": "node:n8n-worker"})["root_cause_correct"]
    wrong = {"root_cause": "Postgres ran out of connections", "failure_chain": []}
    assert not d.grade(wrong, {"fault": "worker_pod_kill", "target": "node:n8n-worker"})["root_cause_correct"]


# ------------------------------------------------------------ score

def test_score_e1_after_fix_matches_formulas():
    s = score.score_experiment(e1_bundle(), {"status": "completed", "watchdog": {"recovered": True},
                                             "thresholds": {"w1_p95_ms": 20000, "recovery_deadline_s": 120}})
    c = {k: v["score"] for k, v in s["components"].items()}
    assert c["data_integrity"] == 25.0                     # 7 of 28 completed once
    assert c["latency"] == 0                               # 30 s peak >= 20 s abort limit
    assert c["self_recovery"] == pytest.approx(100 - 30 * (78.8 - 30) / 90, abs=0.1)
    assert c["detectability"] == 0                         # n8n-main stayed Ready while consumers were down
    assert 0 < c["availability"] < 100
    w = score.CONFIG["weights"]
    expect = sum(w[k] * v for k, v in c.items())
    assert s["coverage"] == 1.0 and s["total"] == pytest.approx(expect, abs=0.2)


def test_score_missing_components_rescale_weights():
    s = score.score_experiment(e1_bundle(fates={}), {"status": "completed"})
    assert s["components"]["data_integrity"]["score"] is None
    assert s["coverage"] == pytest.approx(0.85)


def test_score_recovery_cases():
    base = e1_bundle(recovery=None)
    assert score.score_experiment(base, {"watchdog": {"recovered": False}})["components"]["self_recovery"]["score"] == 0
    assert score.score_experiment({**base, "manual_action_needed": True}, {})["components"]["self_recovery"]["score"] == 40
    assert score.score_experiment(base, {})["components"]["self_recovery"]["score"] == 60
    assert score.score_experiment(e1_bundle(recovery=20), {})["components"]["self_recovery"]["score"] == 100


def test_skipped_experiment_is_not_scored():
    assert score.score_experiment(e1_bundle(), {"status": "skipped", "skip_reason": "baseline_unstable"})["scored"] is False


# ------------------------------------------------------------ report

def test_report_full_run(tmp_path):
    rdir = make_run(tmp_path)
    exps = report.load_run(rdir)
    md, data = report.build(exps, use_llm=False)
    for section in ("## 1. Summary", "## 2. Environment and baseline", "## 3. Experiments",
                    "## 4. Weaknesses, ranked by severity", "## 5. Recommended fixes", "## 6. How the score"):
        assert section in md
    assert "E1 · Worker pod kill" in md and "ministral-14b-latest" in md
    assert "Why Agent 1 chose it** (llm)" in md and "Graded against the real injection: correct" in md
    e = data["experiments"][0]
    assert e["expected_seen"] == ["consumers_down", "latency_degradation"] and e["score"]["scored"]
    sev = [w["severity"] for w in data["weaknesses"]]
    assert sev == sorted(sev, key=report.SEVERITY.get) and sev[0] == "Critical"
    assert data["fixes"][0]["by_agent2"] == 1                # Agent 2's own picks rank first
    assert data["overall"]["score"] == e["score"]["total"]


def test_report_with_only_evidence_files(tmp_path):
    rdir = make_run(tmp_path, diagnosis=False, results=False, journal=False)
    md, data = report.build(report.load_run(rdir), use_llm=False)
    assert "Agent 2:** not run yet" in md and data["experiments"][0]["grade"] is None
    assert data["experiments"][0]["score"]["scored"]


def test_report_for_fault_unknown_to_kb(tmp_path):
    rdir = make_run(tmp_path, fault="load_spike", target="loadgen")
    md, data = report.build(report.load_run(rdir), use_llm=False)
    assert "No knowledge file for `load_spike`" in md and data["experiments"][0]["fault"] == "load_spike"


def test_cli_report_writes_markdown_and_json(tmp_path, monkeypatch):
    make_run(tmp_path, run_id="r1010-100000")
    make_run(tmp_path, run_id="r1010-110000")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    r = CliRunner().invoke(cli.app, ["report", "--no-llm"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "reports" / "r1010-110000" / "findings.md").exists()     # newest run by default
    r = CliRunner().invoke(cli.app, ["report", "--all"])            # no key here: labelled deterministic draft
    data = json.loads((tmp_path / "reports" / "findings.json").read_text())
    assert r.exit_code == 0 and data["runs"] == ["r1010-100000", "r1010-110000"]
    assert data["source"].startswith("fallback:AI writer unavailable")


def test_cli_report_without_runs(tmp_path, monkeypatch):
    (tmp_path / "reports").mkdir()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert CliRunner().invoke(cli.app, ["report"]).exit_code == 1


# ------------------------------------------------------------ AI-written report (fake model, no network)

def _exp_json(ref="r1010-163348/exp-01", text="11 of 28 orders were lost visibly; recovery took 78.8 s.", **over):
    d = {"exp_ref": ref, "headline": "Worker kill hurt W1.", "why_chosen": "single point of failure",
         "expected_vs_observed": "W1 slowed as predicted; no duplicates.", "impact": text, "recovery": "78.8 s, no help.",
         "root_cause": "The worker stopped; the blind diagnosis was correct.", "evidence": ["consumers_down", "fates"]}
    d.update(over)
    return json.dumps(d)


def _score_json(values=(80, 85, 0, 25, 0), ref="r1010-163348/exp-01", **over):
    d = {"exp_ref": ref, "scores": [{"component": c, "score": v, "reason": "judged from the measured evidence"}
                                    for c, v in zip(reporter.COMPONENTS, values)]}
    d.update(over)
    return json.dumps(d)


def _summary_json(**over):
    d = {"executive_summary": "Killing the worker lost 11 orders visibly.", "environment": "n8n queue mode.",
         "weaknesses": [{"title": "Lost orders", "severity": "Critical", "explanation": "11 lost visibly",
                         "evidence": ["lost_work"], "seen_in": ["r1010-163348/exp-01"], "fixes": ["scale_workers"]},
                        {"title": "Green health checks", "severity": "Medium", "explanation": "n8n-main stayed Ready",
                         "evidence": ["detectability_gap"], "seen_in": ["r1010-163348/exp-01"],
                         "fixes": ["queue_depth_alert"]}],
         "fixes": [{"playbook_id": "scale_workers", "priority": 1, "why": "spare consumer", "addresses": ["Lost orders"]}],
         "limitations": "Only one fault type."}
    d.update(over)
    return json.dumps(d)


def fake_model(exp_answers, summary_answers, score_answers=None):
    seen = []
    queues = {"ExperimentReport": exp_answers, "FindingsSummary": summary_answers,
              "ScoreJudgement": score_answers or [_score_json()]}

    def call(model, prompt, schema):
        seen.append(schema["title"])
        queue = queues[schema["title"]]
        return queue.pop(0) if len(queue) > 1 else queue[0]
    call.seen = seen
    return call


def test_ai_report_is_written_by_the_model(tmp_path):
    rdir = make_run(tmp_path)
    call = fake_model([_exp_json()], [_summary_json()])
    md, data = report.build(report.load_run(rdir), call=call)
    assert data["source"] == "llm"
    assert call.seen == ["ScoreJudgement"] * reporter.SAMPLES + ["ExperimentReport", "FindingsSummary"]
    ai = data["experiments"][0]["ai_score"]
    assert ai["total"] == round(0.30 * 80 + 0.25 * 85 + 0.20 * 0 + 0.15 * 25 + 0.10 * 0, 1)  # the AI's judgement
    assert data["overall"]["score"] == ai["total"] and data["ranking"]["experiments"][0]["score"] == ai["total"]
    assert data["experiments"][0]["reference_score"]["total"] == score.score_experiment(
        e1_bundle(), json.loads((rdir / "results.json").read_text())[0])["total"]        # formula kept as reference
    assert "Written and scored by Agent 2" in md and "Killing the worker lost 11 orders visibly." in md
    assert "Deterministic draft" not in md and data["unverified_numbers"] == {}


def test_invalid_ai_output_is_retried_with_feedback(tmp_path):
    rdir = make_run(tmp_path)
    prompts = []
    bad = _exp_json(evidence=["made_up_id"])
    call = fake_model([bad, _exp_json()], [_summary_json(fixes=[{"playbook_id": "rm -rf", "priority": 1, "why": "x",
                                                                  "addresses": []}]), _summary_json()])
    orig = llm.generate_json

    def spy(purpose, prompt, *a, **k):
        if purpose != "report_score":
            prompts.append(prompt)
        return orig(purpose, prompt, *a, **k)
    import chaosctl.agents.reporter as rp
    rp.llm.generate_json, saved = spy, rp.llm.generate_json
    try:
        md, data = report.build(report.load_run(rdir), call=call)
    finally:
        rp.llm.generate_json = saved
    assert data["source"] == "llm"
    assert "unknown evidence ids ['made_up_id']" in prompts[1] and "fixes not in playbook ['rm -rf']" in prompts[3]


def test_ai_output_still_invalid_after_retry_falls_back(tmp_path):
    rdir = make_run(tmp_path)
    call = fake_model([_exp_json()], [_summary_json()], [_score_json(scores=[])])
    md, data = report.build(report.load_run(rdir), call=call)
    assert data["source"].startswith("fallback:AI writer unavailable") and "Deterministic draft" in md


def test_fact_check_catches_invented_numbers_and_false_claims(tmp_path, monkeypatch):
    rdir = make_run(tmp_path)
    lie = _exp_json(text="12345 orders were lost silently.")
    md, data = report.build(report.load_run(rdir), call=fake_model([lie], [_summary_json()]))
    flags = data["unverified_numbers"]["r1010-163348/exp-01"]
    assert "12345" in flags and any("lost silently" in f for f in flags) and "Fact-check flags" in md
    # a corrected second answer clears the flags (fresh cache, or the first answers are replayed)
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path / "llm2")
    md, data = report.build(report.load_run(rdir), call=fake_model([lie, _exp_json()], [_summary_json()]))
    assert data["unverified_numbers"] == {}


def test_summary_rules(tmp_path):
    facts = [reporter.experiment_facts({**report.load_run(make_run(tmp_path))[0], "detectors": [], "grade": None},
                                       load_kb())]
    r = reporter.ExperimentReport.model_validate_json(_exp_json())
    fired = {"lost_work", "detectability_gap"}
    ok = reporter.FindingsSummary.model_validate_json(_summary_json())
    assert reporter.validate_summary(ok, [r], fired) == []
    unordered = reporter.FindingsSummary.model_validate_json(_summary_json(weaknesses=list(reversed(
        json.loads(_summary_json())["weaknesses"]))))
    assert any("most severe first" in e for e in reporter.validate_summary(unordered, [r], fired))
    assert reporter.false_claims("with no silent data loss", facts) == []
    assert reporter.false_claims("orders were lost silently", facts)


def test_damaged_journal_line_is_skipped(tmp_path):
    rdir = make_run(tmp_path)
    with (rdir / "journal.jsonl").open("a") as f:
        f.write('{"event": "llm", "trunc')            # run killed mid-write
    exps = report.load_run(rdir)
    assert exps[0]["plan_item"]["fault"] == "worker_pod_kill"


# ------------------------------------------------------------ ranking + dashboard panel data

def test_ranking_orders_least_resilient_first():
    comps = lambda a, l: {"availability": a, "self_recovery": 90, "latency": l, "data_integrity": 50, "detectability": None}
    rk = report.ranking([
        {"exp_ref": "r1/exp-01", "title": "Worker pod kill", "alias": "E1", "target": "n8n-worker", "status": "completed",
         "score": 56, "verdict": "fragile", "components": comps(80, 20)},
        {"exp_ref": "r2/exp-01", "title": "Redis pod kill", "alias": "E2", "target": "redis", "status": "aborted",
         "score": 31, "verdict": "failing", "components": comps(40, 0)},
        {"exp_ref": "r3/exp-01", "title": "Skipped one", "alias": None, "target": "redis", "status": "skipped",
         "score": None, "verdict": None, "components": {}},
    ])
    assert [e["exp_ref"] for e in rk["experiments"]] == ["r2/exp-01", "r1/exp-01", "r3/exp-01"]
    assert [e["rank"] for e in rk["experiments"]] == [1, 2, None]               # unscored is listed, not ranked
    assert rk["experiments"][0]["weakest"] == "latency"
    names = [c["component"] for c in rk["components"]]
    assert names[0] == "latency" and names[-1] == "detectability"               # n/a sorts last
    lat = rk["components"][0]
    assert lat["mean"] == 10.0 and lat["n"] == 2


def test_both_report_paths_include_ranking(tmp_path):
    rdir = make_run(tmp_path)
    for kwargs in ({"use_llm": False}, {"call": fake_model([_exp_json()], [_summary_json()])}):
        md, data = report.build(report.load_run(rdir), **kwargs)
        assert "### Resilience ranking (least resilient first)" in md
        assert data["ranking"]["experiments"][0]["exp_ref"] == "r1010-163348/exp-01"
        assert {"score", "verdict"} <= set(data["overall"])


def test_dashboard_reads_newest_report(tmp_path, monkeypatch):
    from chaosctl import dashboard
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    assert dashboard.resilience() is None                                       # no report yet
    exps = report.load_run(make_run(tmp_path))
    report.write(exps, tmp_path / "reports" / "r1010-163348" / "findings.md", use_llm=False)
    r = dashboard.resilience()
    assert r["path"] == "reports/r1010-163348/findings.json" and r["ranking"]["experiments"][0]["rank"] == 1
    assert r["overall"]["score"] == r["ranking"]["experiments"][0]["score"]


# ------------------------------------------------------------ AI score: median of samples, calibration memory

def test_ai_score_is_median_of_independent_judgements(tmp_path):
    rdir = make_run(tmp_path)
    samples = [_score_json((90, 90, 10, 30, 0)), _score_json((70, 60, 30, 20, 0)), _score_json((80, 85, 20, 25, 0))]
    md, data = report.build(report.load_run(rdir), call=fake_model([_exp_json()], [_summary_json()], samples))
    comps = {k: c["score"] for k, c in data["experiments"][0]["ai_score"]["components"].items()}
    assert comps == {"availability": 80, "self_recovery": 85, "latency": 20, "data_integrity": 25, "detectability": 0}
    assert data["experiments"][0]["ai_score"]["spread"] > 0 and "median of 3 judgements" in md


def test_scores_are_remembered_and_used_for_calibration(tmp_path, monkeypatch):
    from chaosctl import score_memory
    make_run(tmp_path, run_id="r1010-100000")
    report.build(report.load_run(tmp_path / "reports" / "r1010-100000"), call=fake_model([_exp_json(
        ref="r1010-100000/exp-01")], [_summary_json(weaknesses=[])], [_score_json(ref="r1010-100000/exp-01")]))
    mem = score_memory.load()
    assert [m["exp_ref"] for m in mem] == ["r1010-100000/exp-01"] and mem[0]["ai_total"] is not None
    prompts = []
    orig = llm.generate_json

    def spy(purpose, prompt, *a, **k):
        prompts.append((purpose, prompt))
        return orig(purpose, prompt, *a, **k)
    monkeypatch.setattr(reporter.llm, "generate_json", spy)
    make_run(tmp_path, run_id="r1010-110000")
    md, data = report.build(report.load_run(tmp_path / "reports" / "r1010-110000"), call=fake_model(
        [_exp_json(ref="r1010-110000/exp-01")], [_summary_json(weaknesses=[])], [_score_json(ref="r1010-110000/exp-01")]))
    score_prompt = next(p for purpose, p in prompts if purpose == "report_score")
    assert "r1010-100000/exp-01" in score_prompt                              # earlier judgement shown as calibration
    assert data["calibrated_on"] == ["r1010-100000/exp-01"] and "calibrated on 1 earlier" in md
    assert len(score_memory.load()) == 2


def test_deterministic_draft_does_not_touch_score_memory(tmp_path):
    from chaosctl import score_memory
    report.build(report.load_run(make_run(tmp_path)), use_llm=False)
    assert score_memory.load() == []
