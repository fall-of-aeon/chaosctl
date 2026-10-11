"""Finished-run API (chaosctl/dash/runs.py + server.run_api) and the fix-summary step, on the recorded fixture run."""
import json
import shutil
from pathlib import Path

import pytest

from chaosctl import fix_summary, llm, report, score
from chaosctl.dash import model, runs, server, sources

FIX = Path(__file__).parent / "fixtures" / "runs"
RUN = "r1010-214152"


@pytest.fixture
def reports(tmp_path, monkeypatch):
    rep = tmp_path / "reports"
    shutil.copytree(FIX / RUN, rep / RUN)
    monkeypatch.setattr(sources, "REPORTS", rep)
    monkeypatch.setattr(sources, "LOADGEN_DB", tmp_path / "missing.sqlite")
    monkeypatch.setattr(sources, "STATE", tmp_path / "state")
    return rep


def api(path, **q):
    body, status = server.run_api(path, lambda k, d: q.get(k, d))
    return body, status


def test_report_api_uses_formula_for_legacy_ai_report(reports):
    exp = report.load_run(reports / RUN)[0]
    expected = score.score_experiment(exp["bundle"], exp["result"])
    legacy = {"source": "llm", "runs": [RUN], "overall": {"score": 1},
              "ranking": {"experiments": [{"exp_ref": f"{RUN}/exp-01", "title": "Worker kill", "score": 1}]},
              "experiments": [{"exp_ref": f"{RUN}/exp-01", "headline": "Worker kill",
                               "ai_score": {"total": 1}, "reference_score": expected}],
              "summary": {"weaknesses": [{"title": "Lost work"}], "fixes": []}}
    (reports / "findings.json").write_text(json.dumps(legacy))
    data, status = api("/api/report")
    assert status == 200 and data["score_method"] == "formula"
    assert data["overall"]["score"] == expected["total"]
    assert data["ranking"]["experiments"][0]["score"] == expected["total"]
    assert data["experiments"][0]["score"] == expected
    assert data["weaknesses"][0]["title"] == "Lost work"
    assert json.loads((reports / "findings.json").read_text()) == legacy  # read-only compatibility


def test_legacy_report_without_formula_score_is_recomputed_from_run_evidence(reports):
    exp = report.load_run(reports / RUN)[0]
    legacy = {"source": "llm", "runs": [RUN], "overall": {"score": 1},
              "ranking": {"experiments": [{"exp_ref": f"{RUN}/exp-01", "score": 1}]},
              "experiments": [{"exp_ref": f"{RUN}/exp-01", "ai_score": {"total": 1}}]}
    (reports / "findings.json").write_text(json.dumps(legacy))
    data, _ = api("/api/report")
    assert data["overall"]["score"] == score.score_experiment(exp["bundle"], exp["result"])["total"]


def test_runs_list_one_entry_per_experiment(reports):
    body, st = api("/api/runs")
    assert st == 200
    (r,) = body["runs"]
    assert r["ref"] == f"{RUN}/exp-01" and r["fault"] == "worker_pod_kill" and r["alias"] == "E1"
    assert r["status"] == "completed" and r["recovery_time_s"] == 33.2 and r["config"] == {"worker_replicas": 2}


def test_summary_has_every_act(reports):
    s, st = api(f"/api/run/{RUN}/summary")
    assert st == 200
    assert s["in_flight"]["total"] == 8 and s["pod"] == "n8n-worker-6cd6854ff5-8nqkn"
    assert s["recovery_time_s"] == 33.2 and s["abort"] is None
    assert [m["label"] for m in s["milestones"]][:2] == ["baseline stable", "inject"]
    assert {p["kind"] for p in s["phases"]} >= {"warmup", "fault"}
    assert s["limits"]["armed_after_s"] == 90 and s["limits"]["w1_p95_ms"] == 20000
    n = s["narration"]
    assert n["inject"] == "Agent 1 killed the n8n-worker pod while 8 orders were in flight."
    assert "1 of 8 in-flight orders were lost" in n["impact"]
    assert set(n) == {"start", "inject", "impact", "diagnose", "fix", "report"}
    assert s["fates"]["total"] == 8


def test_series_is_relative_to_injection_and_honest_about_gaps(reports):
    se, _ = api(f"/api/run/{RUN}/series")
    t = se["cols"]["t"]
    assert t[0] == -60 and 0 in t and t == sorted(t)
    assert all(v is None for v in se["cols"]["w1_ok"])                  # no loadgen rows: no fabricated zeros
    assert any(g["kind"] == "not_recorded" for g in se["gaps"])
    i0 = t.index(1)
    assert se["cols"]["q_active"][i0] is not None                        # queue comes from the evidence trace
    assert any(row["row"].startswith("n8n-worker-") for row in se["pods"])


def test_logs_filter_count_and_page(reports):
    lg, _ = api(f"/api/run/{RUN}/logs")
    assert lg["total"] > 0 and set(lg["counts"]) == {"severity", "source", "component"}
    assert lg["rows"] == sorted(lg["rows"], key=lambda r: (r["ts"] is not None, r["ts"] or 0), reverse=True)
    k8s, _ = api(f"/api/run/{RUN}/logs", source="k8s")
    assert k8s["rows"] and all(r["source"] == "k8s" for r in k8s["rows"])
    ev, _ = api(f"/api/run/{RUN}/logs", evidence="ev:events")
    assert ev["total"] == k8s["total"]
    page, _ = api(f"/api/run/{RUN}/logs", page_size="2")
    assert len(page["rows"]) == 2 and page["total"] == lg["total"]
    q, _ = api(f"/api/run/{RUN}/logs", q="Killing")
    assert q["rows"] and all("killing" in r["message"].lower() for r in q["rows"])


def test_fates_agent1_diagnosis(reports):
    f, _ = api(f"/api/run/{RUN}/fates")
    assert {r["fate"]: r["count"] for r in f["rows"]}["lost_silently"] == 1
    a1, _ = api(f"/api/run/{RUN}/agent1")
    assert a1["available"] and a1["chosen"]["fault"] == "worker_pod_kill" and a1["alias"] == "E1"
    assert a1["source"]["provider"] == "Gemini" and a1["candidates"][0]["terms"]
    d, _ = api(f"/api/run/{RUN}/diagnosis")
    assert d["available"] and d["grade"]["root_cause_correct"] is True and d["fixes"][0]["runbook"]


def test_fix_summary_and_report_empty_states(reports):
    fs, _ = api(f"/api/run/{RUN}/fix-summary")
    assert fs == {"available": False, "command": f"uv run chaosctl fix-summary --run {RUN} --exp exp-01"}
    rep2, _ = api("/api/report")
    assert rep2["available"] is False


def test_run_without_findings_report_gets_formula_score_from_its_evidence(reports):
    """The story's score appears as soon as the run's evidence bundle exists, without `chaosctl report`."""
    exp = report.load_run(reports / RUN)[0]
    expected = score.score_experiment(exp["bundle"], exp["result"])
    rep, _ = api(f"/api/run/{RUN}/report", exp="exp-01")
    assert rep["available"] is True and rep["computed"] is True and rep["score_method"] == "formula"
    assert rep["overall"]["score"] == expected["total"] and rep["overall"]["verdict"] == expected["band"]
    assert rep["experiments"] == [{"exp_ref": f"{RUN}/exp-01", "score": expected}]
    summ, _ = api(f"/api/run/{RUN}/summary", exp="exp-01")
    assert summ["report_available"] is True and str(expected["total"]) in summ["narration"]["report"]
    assert not (reports / RUN / "findings.json").exists()        # computed on read, nothing written


def test_fix_summary_file_is_not_an_experiment(reports):
    (reports / RUN / "evidence" / "fix_summary.json").write_text(json.dumps({"exp-01": {"summary": "x"}}))
    assert [e["exp_id"] for e in report.load_run(reports / RUN)] == ["exp-01"]
    rep, _ = api(f"/api/run/{RUN}/report")
    assert [e["exp_ref"] for e in rep["experiments"]] == [f"{RUN}/exp-01"]


def test_unknown_run_and_endpoint(reports):
    assert api("/api/run/r0000-000000/summary")[1] == 404
    assert api(f"/api/run/{RUN}/nope")[1] == 404


def _second_run(reports, workers: int, recovery: float):
    """A copy of the fixture run with a different worker count (the 'after fix' run)."""
    dst = reports / "r1010-230000"
    shutil.copytree(reports / RUN, dst)
    pf = dst / "evidence" / "exp-01.plan.json"
    p = json.loads(pf.read_text())
    p["at_inject"]["target_pods"] = p["at_inject"]["target_pods"][:workers]
    pf.write_text(json.dumps(p))
    ev = dst / "evidence" / "exp-01.json"
    e = json.loads(ev.read_text())
    e["recovery_time_s"] = recovery
    ev.write_text(json.dumps(e))
    j = dst / "journal.jsonl"
    j.write_text("\n".join(json.dumps({**json.loads(line), "ts": json.loads(line)["ts"] + 3600})
                           for line in j.read_text().splitlines()) + "\n")


def test_stretch_goals_computed_from_runs(reports):
    g = {x["id"]: x for x in api("/api/stretch-goals")[0]["goals"]}
    assert g["rerun_after_fix"]["status"] != "Done" and g["combined_failures"]["status"] == "Planned"
    assert g["live_dashboard"]["status"] == "Done"
    _second_run(reports, workers=1, recovery=20.0)
    g = {x["id"]: x for x in api("/api/stretch-goals")[0]["goals"]}
    assert g["rerun_after_fix"]["status"] == "Done"
    assert g["rerun_after_fix"]["pair"]["change"] == "worker replicas 2 -> 1"


def test_compare_defaults_to_the_before_after_pair(reports):
    _second_run(reports, workers=1, recovery=20.0)
    c, st = api("/api/compare")
    assert st == 200 and c["a"]["summary"]["run_id"] == RUN and c["b"]["summary"]["run_id"] == "r1010-230000"
    assert [d["metric"] for d in c["deltas"]] == ["recovery time", "peak W1 p95", "lost or duplicated orders", "auto-stop"]
    assert c["verdict"].startswith("After: recovery 1.7x faster")


# ------------------------------------------------------------------ pure wording rules

def _s(**kw):
    base = {"status": "completed", "fates": model.fate_rows({"completed_once": 5}), "restarts_delta": {}, "peak_p95": {"ratio": 1.0}}
    return {**base, **kw}


def test_hypothesis_verdicts():
    lost = model.fate_rows({"completed_once": 4, "lost_silently": 1})
    assert model.judge_hypothesis("queued jobs are lost", _s(fates=lost))["verdict"] == "confirmed"
    assert model.judge_hypothesis("queued jobs are lost", _s())["verdict"] == "refuted"
    assert model.judge_hypothesis("no job is lost", _s())["verdict"] == "confirmed"
    v = model.judge_hypothesis("jobs are lost because redis has no persistence; n8n may restart", _s())
    assert [c["claim"] for c in v["claims"]] == ["orders lost", "a process restarted"] and v["claims"][1]["hedged"]
    assert model.judge_hypothesis("something vague", _s())["verdict"] == "not judged"
    assert model.judge_hypothesis("jobs are lost", _s(status="running"))["verdict"] == "not judged"


def test_fallback_badge_is_readable():
    assert model.fallback_badge({"kind": "fallback", "why": "MISTRAL_MODEL not set"}) == \
        "Rule-based fallback: Mistral model not configured (MISTRAL_MODEL not set)"
    assert model.fallback_badge({"kind": "llm", "text": "LLM (Mistral, x)"}) == "LLM (Mistral, x)"


# ------------------------------------------------------------------ fix summary

DIAG = {"root_cause": "The worker was terminated without graceful shutdown. More text.",
        "failure_chain": ["Worker pod terminated", "Job lost"], "findings": [{"title": "lost work"}],
        "fixes": [{"playbook_id": "graceful_shutdown", "why": "x"}, {"playbook_id": "scale_workers", "why": "y"}]}


def fake(answer: dict):
    return lambda model_name, prompt, schema: json.dumps(answer)


def test_fix_summary_llm_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTRAL_MODEL", "fake-model")
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path)
    out = fix_summary.build(DIAG, call=fake({"what_broke": "A worker died mid-job.", "why": "No graceful stop.",
                                             "fixes": [{"playbook_id": "graceful_shutdown", "action": "Give workers time to finish."}]}))
    assert out["source"].startswith("llm:api:fake-model") and out["what_broke"] == "A worker died mid-job."
    f = out["fixes"][0]
    from chaosctl.agents.diagnosis import PLAYBOOK
    assert f["verify"] == PLAYBOOK["graceful_shutdown"]["verify"] and f["effort"] == PLAYBOOK["graceful_shutdown"]["effort"]
    assert "Blind" not in fix_summary.prompt(DIAG, ["graceful_shutdown"]) and "worker_pod_kill" not in fix_summary.prompt(DIAG, ["graceful_shutdown"])


def test_fix_summary_rejects_ids_outside_playbook(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTRAL_MODEL", "fake-model")
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path)
    out = fix_summary.build(DIAG, call=fake({"what_broke": "x", "why": "y", "fixes": [{"playbook_id": "rewrite_it_in_rust", "action": "z"}]}))
    assert out["source"] == "deterministic fallback" and "unknown playbook id" in out["fallback_reason"]
    assert [f["playbook_id"] for f in out["fixes"]] == ["graceful_shutdown", "scale_workers"]


def test_fix_summary_fallback_without_llm(monkeypatch, tmp_path):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_MODEL", "m")
    out = fix_summary.build(DIAG)
    assert out["source"] == "deterministic fallback" and out["what_broke"] == "The worker was terminated without graceful shutdown."
    p = fix_summary.write(tmp_path / "run", "exp-01", out) if (tmp_path / "run" / "evidence").mkdir(parents=True) is None else None
    assert json.loads(p.read_text())["exp-01"]["source"] == "deterministic fallback"


def test_running_experiment_shows_the_baseline_file_until_its_bundle_exists(reports):
    """Act 0 must show the baseline while the run is going; the evidence bundle only arrives at the end."""
    bundle = reports / RUN / "evidence" / "exp-01.json"
    recorded = json.loads(bundle.read_text())["baseline"]
    bundle.unlink()
    server._cache.clear()        # other tests cached this fixture's summary under the same journal/evidence stamp
    state = sources.STATE
    state.mkdir(parents=True, exist_ok=True)
    (state / "baseline.json").write_text(json.dumps({**recorded, "w1": {**recorded["w1"], "p95_ms": 1234}}))
    s, _ = api(f"/api/run/{RUN}/summary", exp="exp-01")
    assert s["baseline"]["w1_p95_ms"] == 1234 and s["baseline"]["duration_s"] == recorded["duration_s"]
