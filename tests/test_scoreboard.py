"""`chaosctl eval` scoreboard on small synthetic run folders (no cluster, no LLM)."""
import json

from typer.testing import CliRunner

from chaosctl import cli, scoreboard as sb

DEFAULTS = {"w1_error_rate": 0.5, "w1_p95_ms": 20000, "queue_waiting": 500}
LIMITS = {"w1_error_rate": 0.5, "w1_p95_ms": 20000, "queue_waiting": 500}
T0 = 1_791_670_000.0


def _journal(d, events):
    d.mkdir(parents=True, exist_ok=True)
    (d / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events) + "{damaged line\n")


def _exp(d, exp, fault, target, root, source="llm:api:ministral-14b-latest", saw_symptom=True, limits=LIMITS):
    ev = d / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / f"{exp}.plan.json").write_text(json.dumps({"fault": fault, "target": target, "thresholds": limits}))
    (ev / f"{exp}.json").write_text(json.dumps({"timeline": {"saw_symptom": saw_symptom}, "fates": {"summary": {}}}))
    (ev / f"{exp}.diagnosis.json").write_text(json.dumps(
        {"source": source, "llm_used": source.startswith("llm"), "root_cause": root, "failure_chain": []}))


def _llm(ts, purpose, source="api", latency=2.0, model="ministral-14b-latest"):
    e = {"event": "llm", "purpose": purpose, "model": model, "ts": ts}
    return e | ({"source": source, "latency_s": latency} if source == "api" else
                {"source": "cache"} if source == "cache" else {"result": "error", "err": "ConnectError"})


def _plan(ts, source, **kw):
    return {"event": "agent1_plan", "source": source, "ts": ts, "plan": {"experiments": []},
            "verdicts": [{"abort": LIMITS}]} | kw


def _incident(state, iid, started, dur, triage, source="llm:api:ministral-14b-latest", run_id=None):
    steps = [{"n": i, "by": "triage"} for i in range(4)] if triage else []
    steps += [{"n": 9, "by": "llm"}]
    (state / "incidents").mkdir(parents=True, exist_ok=True)
    (state / "incidents" / f"{iid}.json").write_text(json.dumps(
        {"id": iid, "started": started, "ended": started + dur, "by": "monitor", "steps": steps, "source": source,
         "run_id": run_id}))


def _world(tmp_path):
    reports, state = tmp_path / "reports", tmp_path / "state"
    a = reports / "r1011-100000"     # LLM plan, correct + no-impact diagnoses
    _journal(a, [_plan(T0, "llm:api:ministral-14b-latest"), _llm(T0, "injection_select", latency=10.0),
                 _llm(T0 + 50, "diagnosis", latency=4.0), _llm(T0 + 60, "diagnosis", "cache")])
    _exp(a, "exp-01", "worker_pod_kill", "node:n8n-worker", "The n8n-worker pod was killed mid-job; queue stalled.")
    _exp(a, "exp-02", "worker_pod_kill", "node:n8n-worker", "No degradation.", saw_symptom=False)
    b = reports / "r1011-110000"     # validator rejected the plan, diagnosis missed, one template fallback
    _journal(b, [{"event": "agent1_invalid", "ts": T0 + 3600, "errors": [
                     "exp-04: cites unknown ids ['edge:x->y']", "exp-05: (node:redis, redis_pod_kill, x) is not an "
                     "enabled catalog candidate"]},
                 {"event": "agent1_self_correct", "ts": T0 + 3601, "errors": []},
                 {"event": "degraded", "kind": "llm_fallback", "component": "Agent 1 (planner)", "ts": T0 + 3602,
                  "reason": "LLM plan failed validation (exp-04: ...) -> deterministic plan used instead"},
                 _plan(T0 + 3603, "fallback:invalid_plan"),
                 _llm(T0 + 3600, "injection_select", latency=6.0), _llm(T0 + 3650, "diagnosis", "failed"),
                 {"event": "degraded", "kind": "llm_fallback", "component": "Agent 2 (diagnosis)", "ts": T0 + 3660,
                  "reason": "no valid response for diagnosis: ConnectError -> detector-only template"}])
    _exp(b, "exp-01", "redis_pod_kill", "node:redis", "Postgres was slow, so W1 timed out.")
    _exp(b, "exp-02", "redis_pod_kill", "node:redis", "Template", source="fallback:no valid response for diagnosis")
    (b / "evidence" / "fix_summary.json").write_text(json.dumps(
        {"exp-01": {"source": "llm:api:m"}, "exp-02": {"source": "deterministic fallback"}}))
    t = reports / "r1011-120000"     # demo with a tightened auto-stop limit: a test run
    _journal(t, [_plan(T0 + 7200, "llm:cache:ministral-14b-latest"), _llm(T0 + 7200, "injection_select", "cache")])
    _exp(t, "exp-01", "worker_pod_kill", "node:n8n-worker", "Postgres disk full.",
         limits=LIMITS | {"queue_waiting": 20})
    _journal(reports / "ks-cli-1-1", [{"event": "kill_switch", "ts": T0 + 7300}])
    p = reports / "plans"            # dashboard plan, journaled again by the run that executes it
    _journal(p, [_plan(T0 + 30, "llm:api:ministral-14b-latest"), _plan(T0 + 40, "fallback:llm_disabled")])
    _journal(reports / "r1011-130000", [_plan(T0 + 9000, "llm:api:ministral-14b-latest", planned_at=T0 + 30)])
    (reports / "findings.json").write_text(json.dumps({
        "source": "llm", "runs": ["r1011-100000", "r1011-110000"], "experiments": [
            {"exp_ref": f"{run}/exp-{exp:02d}"} for run in (a.name, b.name) for exp in (1, 2)],
        "unverified_numbers": {"r1011-110000/exp-01": ["12.7", "3"]},
        "llm_calls": [{"purpose": "report_summary", "model": "m", "source": "api", "latency_s": 20.0},
                      {"purpose": "report_score", "model": "m", "source": "cache"}]}))
    _incident(state, "inc-1", T0 + 100, 40.0, triage=False, run_id="r1011-100000")
    _incident(state, "inc-2", T0 + 200, 50.0, triage=False)
    _incident(state, "inc-3", T0 + 4000, 20.0, triage=True)
    _incident(state, "inc-4", T0 + 4100, 0.0, triage=True, source="rule:healthy (triage normal, no LLM needed)")
    _incident(state, "inc-5", T0 + 7250, 30.0, triage=True, run_id="r1011-120000")
    (state / "degraded.jsonl").write_text(json.dumps(
        {"event": "degraded", "kind": "llm_fallback", "component": "Ask Agent 2 (chat)", "ts": T0, "reason": "x"}) + "\n")
    return reports, state


def _eval(tmp_path, **kw):
    reports, state = _world(tmp_path)
    return sb.evaluate(reports, state, defaults=DEFAULTS, **kw)


def test_diagnosis_accuracy_grades_llm_only(tmp_path):
    d = _eval(tmp_path)["diagnosis"]
    assert (d["diagnoses"], d["llm"], d["fallback"]) == (5, 4, 1)
    assert (d["CORRECT"], d["MISSED"], d["NO_IMPACT"]) == (1, 2, 1)   # test run's "Postgres disk full" is MISSED
    assert d["accuracy_pct"] == 33.3
    assert sum(d["fallback_template_verdicts"].values()) == 1


def test_exclude_tests_skips_kill_switch_and_tightened_limits(tmp_path):
    data = _eval(tmp_path, exclude_tests=True)
    assert data["skipped"] == {"ks-cli-1-1": "kill-switch test", "r1011-120000": "test limits: queue_waiting=20"}
    d = data["diagnosis"]
    assert (d["CORRECT"], d["MISSED"], d["NO_IMPACT"], d["accuracy_pct"]) == (1, 1, 1, 50.0)
    # the test run's live investigation goes too
    assert "inc-5" not in {r["id"] for r in data["investigation_time"]["rows"]}


def test_plan_validity_and_rejection_reasons(tmp_path):
    p = _eval(tmp_path, exclude_tests=True)["planner"]
    assert p["plans"] == 4                       # the re-journaled dashboard plan (planned_at) counts once
    assert p["by_source"] == {"api": 2, "cache": 0, "llm": 0, "fallback": 1, "disabled": 1, "rule": 0}
    assert (p["llm_plans"], p["attempted_llm"], p["llm_valid_pct"]) == (2, 3, 66.7)
    assert p["validator_rejections"] == 1 and p["self_corrections"] == 1
    assert p["rejection_reasons"] == {"cites an id not in the graph": 1, "not an enabled catalog candidate": 1}
    assert p["fallback_reasons"] == {"invalid_plan (validator)": 1}
    assert len(p["fallback_events"]) == 1


def test_component_fallback_rates_and_source_mix(tmp_path):
    c = _eval(tmp_path, exclude_tests=True)["components"]
    assert c["diagnosis"]["decisions"]["fallback"] == 1 and c["diagnosis"]["fallback_rate_pct"] == 25.0
    assert c["diagnosis"]["calls"] == {"calls": 3, "api": 1, "cache": 1, "failed": 1}
    assert c["diagnosis"]["fallback_reasons"] == {"API unreachable / no valid response": 1}
    assert c["planner"]["fallback_rate_pct"] == 33.3
    assert c["fix summary"]["decisions"]["api"] == 1 and c["fix summary"]["fallback_rate_pct"] == 50.0
    assert c["reporter"]["decisions"]["api"] == 1 and c["reporter"]["calls"]["calls"] == 2
    assert c["chat"]["decisions"]["fallback"] == 1
    assert c["chat"]["fallback_rate_pct"] is None
    assert c["live investigation"]["decisions"]["rule"] == 1


def test_fact_check_pass_rate(tmp_path):
    fc = _eval(tmp_path)["fact_check"]
    assert (fc["experiments"], fc["passed"], fc["pass_rate_pct"]) == (4, 3, 75.0)
    assert fc["rows"][0]["flagged_numbers"] == 2


def test_llm_calls_and_latency_per_run(tmp_path):
    calls = _eval(tmp_path, exclude_tests=True)["llm_calls"]
    rows = {r["run"]: r for r in calls["rows"]}
    assert (rows["r1011-100000"]["calls"], rows["r1011-100000"]["api"], rows["r1011-100000"]["cache"]) == (3, 2, 1)
    assert rows["r1011-100000"]["latency_total_s"] == 14.0 and rows["r1011-100000"]["latency_max_s"] == 10.0
    assert rows["r1011-110000"]["failed"] == 1
    assert rows["findings.json (reporter)"]["latency_total_s"] == 20.0
    assert calls["total"] == 7 and calls["latency_median_s"] == 8.0


def test_investigation_time_old_vs_new(tmp_path):
    inv = _eval(tmp_path)["investigation_time"]
    assert inv["old_no_triage"]["n"] == 2 and inv["old_no_triage"]["median_s"] == 45.0
    assert inv["new_triage"]["n"] == 3 and inv["new_triage"]["median_s"] == 20.0
    assert inv["new_llm_concluded"]["n"] == 2 and inv["new_llm_concluded"]["median_s"] == 25.0


def test_runs_filter(tmp_path):
    data = _eval(tmp_path, runs=["r1011-110000", "nope"])
    assert data["runs"] == ["r1011-110000"] and data["skipped"] == {"nope": "not found"}
    assert data["diagnosis"]["diagnoses"] == 2
    assert data["investigation_time"]["rows"] == []      # no incident belongs to that run
    assert data["components"]["chat"]["decisions"]["fallback"] == 0
    assert data["fact_check"]["reports"] == 1             # findings.json covers the run
    assert data["fact_check"]["experiments"] == 2
    assert data["fact_check"]["passed"] == 1


def test_since_filter(tmp_path):
    data = _eval(tmp_path, since=str(T0 + 3000))
    assert "r1011-100000" not in data["runs"] and data["skipped"]["r1011-100000"] == "before --since"
    assert {r["id"] for r in data["investigation_time"]["rows"]} == {"inc-3", "inc-4", "inc-5"}
    assert data["planner"]["plans"] == 3                 # plans/ events before --since are dropped
    assert sb.parse_since("2026-10-11T05:00") > 0


def test_output_is_deterministic(tmp_path):
    reports, state = _world(tmp_path)
    first = sb.evaluate(reports, state, defaults=DEFAULTS)
    second = sb.evaluate(reports, state, defaults=DEFAULTS)
    assert first == second
    one, two = tmp_path / "one", tmp_path / "two"
    sb.write(first, one)
    sb.write(second, two)
    assert (one / "eval.json").read_bytes() == (two / "eval.json").read_bytes()
    a = (one / "eval.md").read_text()
    b = (two / "eval.md").read_text()
    assert a == b and "| Agent 2 blind diagnosis accuracy" in a and "1/3 CORRECT (33.3%)" in a


def test_cli_writes_eval_md_and_json(tmp_path, monkeypatch):
    reports, _ = _world(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    res = CliRunner().invoke(cli.app, ["eval", "--exclude-tests", "--runs", "r1011-100000,r1011-110000"])
    assert res.exit_code == 0, res.output
    assert "## Scoreboard" in res.output
    data = json.loads((reports / "eval.json").read_text())
    assert data["runs"] == ["r1011-100000", "r1011-110000"]
    assert (reports / "eval.md").read_text().startswith("# chaosctl eval")


def test_summary_flags_do_not_reduce_experiment_pass_rate(tmp_path):
    reports, state = _world(tmp_path)
    p = reports / "findings.json"
    findings = json.loads(p.read_text())
    findings["unverified_numbers"]["summary"] = ["99"]
    p.write_text(json.dumps(findings))
    fc = sb.evaluate(reports, state, defaults=DEFAULTS)["fact_check"]
    assert (fc["experiments"], fc["passed"], fc["pass_rate_pct"]) == (4, 3, 75.0)
    assert fc["rows"][0]["summary_flagged"] is True


def test_findings_filter_excludes_flags_from_other_runs(tmp_path):
    reports, state = _world(tmp_path)
    fc = sb.evaluate(reports, state, runs=["r1011-100000"], defaults=DEFAULTS)["fact_check"]
    assert (fc["experiments"], fc["passed"], fc["pass_rate_pct"]) == (2, 2, 100.0)
    assert fc["rows"][0]["flagged"] == {}


def test_empty_input_writes_reports(tmp_path):
    reports = tmp_path / "reports"
    data = sb.evaluate(reports, tmp_path / "state", defaults=DEFAULTS)
    sb.write(data, reports)
    assert data["runs"] == [] and data["llm_calls"]["total"] == 0
    assert data["diagnosis"]["accuracy_pct"] is None
    assert (reports / "eval.json").exists()


def test_cli_rejects_invalid_since(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    res = CliRunner().invoke(cli.app, ["eval", "--since", "not-a-date"])
    assert res.exit_code == 2
    assert "expected an ISO date-time or epoch seconds" in res.output


def test_findings_in_symlinked_run_folder(tmp_path):
    reports, state = _world(tmp_path)
    archived = tmp_path / "archived"
    archived.mkdir()
    (archived / "journal.jsonl").write_text(json.dumps(_plan(T0, "llm:api:m")) + "\n")
    (archived / "findings.json").write_text(json.dumps({
        "source": "llm", "runs": ["r-archived"], "experiments": [{"exp_ref": "r-archived/exp-01"}]}))
    (reports / "r-archived").symlink_to(archived, target_is_directory=True)
    fc = sb.evaluate(reports, state, runs=["r-archived"], defaults=DEFAULTS)["fact_check"]
    assert fc["reports"] == 1 and fc["experiments"] == 1 and fc["passed"] == 1
