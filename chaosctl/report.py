"""Findings report (`chaosctl report`): one Markdown + JSON document from what runs already record.

Written by Agent 2 in report mode (agents/reporter.py): it analyses each experiment, ranks weaknesses and
fixes, and scores resilience, checked by validators and a number fact-check. If the LLM is unavailable or its
output stays invalid, a deterministic draft is written instead and clearly labelled as such.

Reads only reports/<run>/{results.json, journal.jsonl, evidence/*} and the fault knowledge base, so it
works for any fault in the catalog and never re-measures anything. Missing pieces (no diagnosis yet,
no results.json) are reported as missing instead of failing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from chaosctl import llm
from chaosctl.agents import reporter
from chaosctl.agents.diagnosis import PLAYBOOK, blind, detect, grade
from chaosctl.fault_kb import FaultKB, load_kb
from chaosctl.score import CONFIG as SCORE_CONFIG, score_experiment, score_overall

SEVERITY = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
TIMELINE = [("t_first_symptom", "first symptom"), ("t_pod_ready", "target replaced/Ready"),
            ("t_queue_drained", "queue back to baseline"), ("t_slis_recovered", "SLIs back to baseline"),
            ("t_last_inflight_resolved", "last in-flight order resolved")]


def _read(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def run_dirs(reports: Path) -> list[Path]:
    """Run directories that contain at least one experiment evidence bundle, oldest first."""
    dirs = [d for d in reports.glob("r*") if d.is_dir() and any(_bundle_files(d))]
    return sorted(dirs, key=lambda d: d.name)


def _bundle_files(rdir: Path):
    return (p for p in sorted((rdir / "evidence").glob("*.json"))
            if not p.name.endswith((".plan.json", ".diagnosis.json")))


def _read_journal(path: Path) -> list[dict]:
    """Journal entries; a damaged line (e.g. a run killed mid-write) is skipped, not fatal."""
    out = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_run(rdir: Path) -> list[dict]:
    """Every experiment of one run with all its records."""
    journal = _read_journal(rdir / "journal.jsonl")
    results = {r["exp_id"]: r for r in (_read(rdir / "results.json") or [])}
    starts = {e["plan"]["id"]: e for e in journal if e.get("event") == "experiment_start" and e.get("plan")}
    plans = [e for e in journal if e.get("event") == "agent1_plan"]
    scored = [e for e in journal if e.get("event") == "agent1_scored"]
    models = sorted({e["model"] for e in journal if e.get("event") == "llm" and e.get("source") in ("api", "cache")})
    exps = []
    for bf in _bundle_files(rdir):
        bundle = json.loads(bf.read_text())
        exp_id = bundle.get("exp_id") or bf.stem
        meta = _read(bf.with_suffix(".plan.json")) or {}
        result = results.get(exp_id) or {k: bundle.get(k) for k in
                                          ("status", "fates", "timeline", "recovery_time_s", "manual_action_needed")}
        result = {**meta, **result}
        start = starts.get(exp_id) or {}
        exps.append({"run_id": rdir.name, "exp_id": exp_id, "bundle": bundle, "result": result,
                     "plan_item": start.get("plan") or {}, "guard": start.get("guard") or {},
                     "plan_source": plans[-1]["source"] if plans else None,
                     "candidates": scored[-1]["candidates"] if scored else [],
                     "llm_models": models, "diagnosis": _read(bf.with_suffix(".diagnosis.json"))})
    return exps


def analyse(exp: dict, kb: FaultKB) -> dict:
    """Derived facts for one experiment: knowledge, detectors, expected vs observed, grade, score."""
    res = exp["result"]
    fault = res.get("fault") or exp["plan_item"].get("fault") or "unknown"
    know = kb.get(fault)
    detectors = (exp["diagnosis"] or {}).get("detectors") or detect(blind(exp["bundle"]))
    fired = {h["id"] for h in detectors}
    expected = set(know.symptoms.expected)
    known = expected | set(know.symptoms.possible)
    diag = exp["diagnosis"]
    return {
        "fault": fault, "knowledge": know, "detectors": detectors,
        "expected_seen": sorted(expected & fired), "expected_missing": sorted(expected - fired),
        "unexpected": sorted(fired - known) if known else [],
        "grade": grade(diag, {"fault": fault, "target": res.get("target", "")}) if diag else None,
        "score": score_experiment(exp["bundle"], res),
    }


# ------------------------------------------------------------ markdown

def _fmt(v, unit="", nd=1):
    if v is None:
        return "n/a"
    return f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}"


def _pct(v):
    return "n/a" if v is None else f"{100 * v:.0f} %"


def _experiment_md(exp: dict, a: dict) -> list[str]:
    res, item, guard, know = exp["result"], exp["plan_item"], exp["guard"], a["knowledge"]
    name = f"{know.alias + ' · ' if know.alias else ''}{know.title}"
    out = [f"### {exp['run_id']} / {exp['exp_id']} · {name} → {res.get('target', '?')} ({res.get('status', '?')})", ""]
    if not know.documented:
        out += [f"> No knowledge file for `{a['fault']}` yet: expected symptoms and hypothesis are unknown.", ""]
    out += [f"**What was injected:** {know.summary or a['fault']}", ""]
    if item:
        out += [f"**Why Agent 1 chose it** ({exp['plan_source'] or 'unknown source'}): {item.get('reason', 'n/a')}",
                f"Cites: {', '.join(item.get('cites') or []) or 'n/a'}", ""]
    if guard:
        ab = guard.get("abort") or {}
        out += [f"**Safety guard:** {guard.get('verdict', 'n/a')} · blast radius {guard.get('blast_radius', [])} · "
                f"abort if W1 error rate > {ab.get('w1_error_rate', 'n/a')}, p95 > {ab.get('w1_p95_ms', 'n/a')} ms, "
                f"queue > {ab.get('queue_waiting', 'n/a')}, total > {ab.get('max_total_s', 'n/a')} s", ""]
    out += ["**Expected vs observed**", "",
            f"- Hypothesis: {item.get('hypothesis') or know.hypothesis or 'none recorded'}",
            f"- Expected symptoms seen: {', '.join(a['expected_seen']) or 'none'}",
            f"- Expected but not seen: {', '.join(a['expected_missing']) or 'none'}",
            f"- Not anticipated by the knowledge base: {', '.join(a['unexpected']) or 'none'}", ""]
    if res.get("status") == "skipped":
        out += [f"Skipped: {res.get('skip_reason')} {res.get('problems') or ''}", ""]
        return out
    tl = res.get("timeline") or {}
    out += ["| Timeline (s after injection) | |", "|---|---|"]
    out += [f"| {label} | {_fmt(tl.get(k), ' s')} |" for k, label in TIMELINE]
    if res.get("abort_reason"):
        out += [f"| automatic stop | {res['abort_reason']} |"]
    out += [f"| manual action needed | {res.get('manual_action_needed')} |", ""]
    summary = ((res.get("fates") or exp["bundle"].get("fates") or {}).get("summary")) or {}
    if summary:
        out += ["| In-flight order fate | count |", "|---|---|"]
        out += [f"| {k.replace('_', ' ')} | {v} |" for k, v in summary.items() if v] + [""]
    if a["detectors"]:
        out += ["**Detectors fired**", ""]
        out += [f"- {h['severity']}: `{h['id']}`: {h['detail']}" for h in a["detectors"]] + [""]
    d = exp["diagnosis"]
    if d:
        g = a["grade"]
        out += [f"**Agent 2 (blind, {d.get('source')})**: {d.get('root_cause')}",
                "Failure chain: " + " → ".join(d.get("failure_chain") or []),
                "Fixes: " + ", ".join(f"`{f['playbook_id']}`" for f in d.get("fixes") or []),
                f"Graded against the real injection: {'correct' if g and g['root_cause_correct'] else 'missed'}", ""]
    else:
        out += [f"**Agent 2:** not run yet (`uv run chaosctl diagnose --run {exp['run_id']} --exp {exp['exp_id']}`).", ""]
    s = a["score"]
    if s.get("scored"):
        out += [f"**Resilience score: {s['total']} / 100 ({s['band']})**"
                + (f" · coverage {s['coverage']:.0%} of weights" if s["coverage"] < 1 else ""), "",
                "| Component | Weight | Score | Evidence |", "|---|---|---|---|"]
        out += [f"| {k.replace('_', ' ')} | {s['weights'][k]:.0%} | {_fmt(c['score'])} | {c['why']} |"
                for k, c in s["components"].items()] + [""]
    else:
        out += [f"Not scored: {s.get('why')}", ""]
    return out


def weaknesses(analysed: list[tuple[dict, dict]]) -> list[dict]:
    agg: dict[str, dict] = {}
    for exp, a in analysed:
        for h in a["detectors"]:
            w = agg.setdefault(h["id"], {"id": h["id"], "severity": h["severity"], "details": [], "seen_in": []})
            if SEVERITY[h["severity"]] < SEVERITY[w["severity"]]:
                w["severity"] = h["severity"]
            w["details"].append(h["detail"])
            w["seen_in"].append(f"{exp['run_id']}/{exp['exp_id']}")
    for w in agg.values():
        w["fixes"] = [f for f, pb in PLAYBOOK.items() if w["id"] in pb["addresses"]]
    return sorted(agg.values(), key=lambda w: (SEVERITY[w["severity"]], -len(w["seen_in"]), w["id"]))


def recommended_fixes(analysed: list[tuple[dict, dict]], weak: list[dict]) -> list[dict]:
    recs: dict[str, dict] = {}
    for exp, _ in analysed:
        for rank, f in enumerate((exp["diagnosis"] or {}).get("fixes") or []):
            r = recs.setdefault(f["playbook_id"], {"id": f["playbook_id"], "by_agent2": 0, "best_rank": rank, "why": f["why"]})
            r["by_agent2"] += 1
            r["best_rank"] = min(r["best_rank"], rank)
    for w in weak:
        for f in w["fixes"]:
            recs.setdefault(f, {"id": f, "by_agent2": 0, "best_rank": 99, "why": None})
            recs[f].setdefault("addresses", set()).add(w["id"])
    out = []
    for r in recs.values():
        pb = PLAYBOOK.get(r["id"], {})
        out.append({**r, "addresses": sorted(r.get("addresses", set())), "title": pb.get("title", r["id"]),
                    "effort": pb.get("effort"), "impact": pb.get("impact"), "verify": pb.get("verify")})
    impact = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda r: (-r["by_agent2"], r["best_rank"], -len(r["addresses"]),
                                      impact.get(r["impact"], 3), r["id"]))


def build_fallback(exps: list[dict], kb: FaultKB | None = None, reason: str | None = None) -> tuple[str, dict]:
    """Deterministic draft, used only when the AI writer is unavailable or disabled."""
    kb = kb or load_kb()
    analysed = [(e, analyse(e, kb)) for e in exps]
    overall = score_overall([a["score"] for _, a in analysed])
    weak = weaknesses(analysed)
    fixes = recommended_fixes(analysed, weak)
    runs = sorted({e["run_id"] for e in exps})
    md = [f"# Findings report: {', '.join(runs)}", "",
          f"> **Deterministic draft, not AI-written** ({reason or 'AI writer disabled with --no-llm'}). "
          "Re-run `uv run chaosctl report` when Mistral is reachable.", "",
          f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `chaosctl report` from the run journals, evidence bundles "
          "and Agent 2 diagnoses.", "", "## 1. Summary", ""]
    if overall:
        md += [f"**Resilience score: {overall['mean']} / 100 ({overall['band']})**, mean of {overall['experiments']} "
               f"scored experiment(s); worst {overall['worst']}.", ""]
    md += ["| Experiment | Fault | Target | Status | Score | Agent 2 root cause |", "|---|---|---|---|---|---|"]
    for e, a in analysed:
        s, g = a["score"], a["grade"]
        md.append(f"| {e['run_id']}/{e['exp_id']} | {a['knowledge'].alias or ''} {a['knowledge'].title} | "
                  f"{e['result'].get('target', '?')} | {e['result'].get('status', '?')} | "
                  f"{(str(s['total']) + ' (' + s['band'] + ')') if s.get('scored') else 'n/a'} | "
                  f"{'not run' if g is None else ('correct' if g['root_cause_correct'] else 'missed')} |")
    md += ["", "## 2. Environment and baseline", ""]
    base = next((e["bundle"].get("baseline") for e in exps if e["bundle"].get("baseline")), None) or {}
    w1, w2, q = base.get("w1") or {}, base.get("w2") or {}, base.get("queue") or {}
    md += [f"- Steady load: {base.get('rate', 'n/a')} orders/s (baseline captured over {base.get('duration_s', 'n/a')} s)",
           f"- W1 (sync): success {_pct(w1.get('success_rate'))}, p50 {_fmt(w1.get('p50_ms'), ' ms', 0)}, "
           f"p95 {_fmt(w1.get('p95_ms'), ' ms', 0)}",
           f"- W2 (async): success {_pct(w2.get('success_rate'))}, p95 {_fmt(w2.get('p95_ms'), ' ms', 0)}",
           f"- Queue: waiting mean {q.get('wait_mean', 'n/a')}, max {q.get('wait_max', 'n/a')}",
           f"- Agent 1 plan source: {', '.join(sorted({str(e['plan_source']) for e in exps}))}; "
           f"LLM models used: {', '.join(sorted({m for e in exps for m in e['llm_models']})) or 'none recorded'}", ""]
    md += ["## 3. Experiments", ""]
    for e, a in analysed:
        md += _experiment_md(e, a)
    md += ["## 4. Weaknesses, ranked by severity", "", "| Severity | Weakness | Seen in | Evidence | Fixes |",
           "|---|---|---|---|---|"]
    md += [f"| {w['severity']} | `{w['id']}` | {len(w['seen_in'])}× | {w['details'][0]} | "
           f"{', '.join(f'`{f}`' for f in w['fixes']) or 'none in playbook'} |" for w in weak]
    md += ["", "## 5. Recommended fixes (advisory; an engineer applies them)", "",
           "| # | Fix | Recommended by | Addresses | Effort / impact | Verify |", "|---|---|---|---|---|---|"]
    md += [f"| {i} | {r['title']} (`{r['id']}`) | "
           f"{('Agent 2 (' + str(r['by_agent2']) + '×)') if r['by_agent2'] else 'playbook match'} | "
           f"{', '.join(r['addresses']) or '-'} | {r['effort']} / {r['impact']} | `{r['verify']}` |"
           for i, r in enumerate(fixes, 1)]
    w = SCORE_CONFIG["weights"]
    md += ["", "## 6. How the score is computed", "",
           f"R = {w['availability']:.2f}·A + {w['self_recovery']:.2f}·S + {w['latency']:.2f}·L + "
           f"{w['data_integrity']:.2f}·D + {w['detectability']:.2f}·M, each 0-100 (weights in `chaosctl/scoring.yaml`).",
           "- A availability: successful W1 responses / all W1 responses after injection.",
           "- S self-recovery: 100 if SLIs are back within 30 s, falling to 70 at the recovery deadline; 60 if pods "
           "recovered but SLIs did not within the window; 40 if the watchdog restarted something; 0 if LOCKED.",
           "- L latency: 100 if peak W1 p95 ≤ 1.5× baseline, 0 at the abort limit, linear in between.",
           "- D data integrity: in-flight orders completed exactly once / all in-flight orders.",
           "- M detectability: 0 if n8n-main stayed Ready through a consumer/queue outage, 100 if health checks "
           "showed it, n/a if no such outage happened.",
           "Components without data are n/a and the other weights are rescaled (coverage is shown). The score "
           "compares experiments on this testbed; it is not a certified reliability measure.", ""]
    data = {"source": f"fallback:{reason or 'llm_disabled'}", "runs": runs, "overall": overall, "weaknesses": weak,
            "fixes": fixes,
            "experiments": [{"run_id": e["run_id"], "exp_id": e["exp_id"], "fault": a["fault"],
                             "target": e["result"].get("target"), "status": e["result"].get("status"),
                             "score": a["score"], "grade": a["grade"], "expected_seen": a["expected_seen"],
                             "expected_missing": a["expected_missing"], "unexpected": a["unexpected"]}
                            for e, a in analysed]}
    return "\n".join(md), data


def build(exps: list[dict], kb: FaultKB | None = None, use_llm: bool = True, call=None) -> tuple[str, dict]:
    """AI-written report; deterministic draft only if the AI is disabled, unavailable or stays invalid."""
    kb = kb or load_kb()
    if not use_llm:
        return build_fallback(exps, kb)
    analysed = [(e, analyse(e, kb)) for e in exps]
    calls: list[dict] = []
    try:
        ai = reporter.write_report([{**e, "detectors": a["detectors"], "grade": a["grade"]} for e, a in analysed],
                                   kb, journal=calls.append, call=call)
    except llm.LLMUnavailable as exc:
        return build_fallback(exps, kb, reason=f"AI writer unavailable: {exc}")
    return render_ai(analysed, ai, calls)


def _section(title: str, text: str) -> list[str]:
    return [f"**{title}:** {text}", ""]


def render_ai(analysed: list[tuple[dict, dict]], ai: dict, calls: list[dict]) -> tuple[str, dict]:
    s: reporter.FindingsSummary = ai["summary"]
    reps = {r.exp_ref: r for r in ai["experiments"]}
    models = sorted({c["model"] for c in calls if c.get("model") and c.get("source") in ("api", "cache")})
    runs = sorted({e["run_id"] for e, _ in analysed})
    md = [f"# Findings report: {', '.join(runs)}", "",
          f"_Written by Agent 2 ({', '.join(models) or 'Mistral'}) on {time.strftime('%Y-%m-%d %H:%M')} from the run "
          "journals and evidence. Every cited id and fix is validated and every number is fact-checked against the "
          "measurements._", "", "## 1. Executive summary", "", s.executive_summary, "",
          f"**Resilience score (Agent 2): {s.overall_score} / 100 ({reporter.band(s.overall_score)})**", "",
          "| Experiment | Fault | Status | Score | Headline |", "|---|---|---|---|---|"]
    for e, a in analysed:
        r = reps[f"{e['run_id']}/{e['exp_id']}"]
        md.append(f"| {r.exp_ref} | {a['knowledge'].alias or ''} {a['knowledge'].title} | "
                  f"{e['result'].get('status', '?')} | {r.total()} ({r.verdict()}) | {r.headline} |")
    md += ["", "## 2. Environment and baseline", "", s.environment, "", "## 3. Experiments", ""]
    formula = {}
    for e, a in analysed:
        r = reps[f"{e['run_id']}/{e['exp_id']}"]
        f = a["score"].get("total") if a["score"].get("scored") else None
        formula[r.exp_ref] = f
        md += [f"### {r.exp_ref} · {a['knowledge'].alias + ' · ' if a['knowledge'].alias else ''}"
               f"{a['knowledge'].title} → {e['result'].get('target', '?')} ({e['result'].get('status', '?')})", "",
               f"_{r.headline}_", ""]
        md += _section("Why Agent 1 chose it", r.why_chosen)
        md += _section("Expected vs observed", r.expected_vs_observed)
        md += _section("Customer impact", r.impact)
        md += _section("Recovery", r.recovery)
        md += _section("Root cause", r.root_cause)
        md += [f"Evidence: {', '.join(f'`{x}`' for x in r.evidence)}", "",
               f"**Resilience score: {r.total()} / 100 ({r.verdict()})**, weighted from Agent 2's component scores", "",
               "| Component | Weight | Score | Reason |", "|---|---|---|---|"]
        w = reporter.SCORE_CONFIG["weights"]
        md += [f"| {c.component.replace('_', ' ')} | {w.get(c.component, 0):.0%} | "
               f"{'n/a' if c.score is None else c.score} | {c.reason} |" for c in r.scores]
        if f is not None and r.total() is not None and abs(f - r.total()) > 20:
            md += ["", f"> Cross-check: the measurement formula in `chaosctl/score.py` gives {f} for this experiment "
                       f"({abs(f - r.total()):.0f} points from Agent 2's score)."]
        tl = e["result"].get("timeline") or {}
        summary = ((e["result"].get("fates") or e["bundle"].get("fates") or {}).get("summary")) or {}
        md += ["", "<details><summary>Measurements</summary>", "",
               "| Seconds after injection | |", "|---|---|"]
        md += [f"| {label} | {_fmt(tl.get(k), ' s')} |" for k, label in TIMELINE]
        md += [""] + (["| In-flight order fate | count |", "|---|---|"]
                      + [f"| {k.replace('_', ' ')} | {v} |" for k, v in summary.items() if v] if summary else [])
        md += ["", "Detectors: " + ("; ".join(f"{h['severity']} `{h['id']}`: {h['detail']}" for h in a["detectors"])
                                    or "none"), "", "</details>", ""]
    md += ["## 4. Weaknesses, ranked by severity", "", "| # | Severity | Weakness | Seen in | Evidence | Fixes |",
           "|---|---|---|---|---|---|"]
    md += [f"| {i} | {w.severity} | **{w.title}**: {w.explanation} | {', '.join(w.seen_in)} | "
           f"{', '.join(f'`{x}`' for x in w.evidence)} | {', '.join(f'`{x}`' for x in w.fixes)} |"
           for i, w in enumerate(s.weaknesses, 1)]
    md += ["", "## 5. Recommended fixes (advisory; an engineer applies them)", "",
           "| Priority | Fix | Why | Addresses | Effort / impact | Verify |", "|---|---|---|---|---|---|"]
    for fx in sorted(s.fixes, key=lambda x: x.priority):
        pb = PLAYBOOK[fx.playbook_id]
        md.append(f"| {fx.priority} | {pb['title']} (`{fx.playbook_id}`) | {fx.why} | {', '.join(fx.addresses)} | "
                  f"{pb['effort']} / {pb['impact']} | `{pb['verify']}` |")
    md += ["", "## 6. Limitations", "", s.limitations, "", "## 7. How this report was produced", "",
           "Agent 2 wrote and scored each experiment from the recorded facts (timeline, order fates, measurements, "
           "detectors, Agent 1's plan and its own blind diagnosis), then wrote the summary, weakness ranking and fix "
           "priorities. Validators rejected unknown ids, fixes outside `playbook.yaml`, malformed scores and "
           "inconsistent rankings (up to two retries with the errors as feedback); numbers in the text were checked against "
           "the measurements. The total is the weighted mean of Agent 2's component scores.", "",
           "Scoring rubric given to Agent 2:", "", "```", reporter.RUBRIC, "```"]
    if ai["unverified_numbers"]:
        md += ["", "**Fact-check flags** (numbers not traceable to the measurements, or claims the counts contradict): "
               + "; ".join(f"{k}: {', '.join(v)}" for k, v in ai["unverified_numbers"].items())]
    data = {"source": "llm", "models": models, "runs": runs,
            "overall": {"score": s.overall_score, "verdict": reporter.band(s.overall_score)},
            "summary": s.model_dump(),
            "experiments": [{**r.model_dump(), "total": r.total(), "verdict": r.verdict()} for r in ai["experiments"]],
            "formula_cross_check": formula, "unverified_numbers": ai["unverified_numbers"], "llm_calls": calls}
    return "\n".join(md), data


def write(exps: list[dict], out_md: Path, kb: FaultKB | None = None, use_llm: bool = True) -> dict:
    md, data = build(exps, kb, use_llm=use_llm)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md)
    out_md.with_suffix(".json").write_text(json.dumps(data, indent=2, default=str))
    return data
