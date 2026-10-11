"""`chaosctl eval`: a scoreboard of the agents, computed only from what the runs recorded (no LLM calls).

Reads reports/<run>/{journal.jsonl, results.json, evidence/*.diagnosis.json, evidence/*.plan.json,
evidence/fix_summary.json, investigations/*.json}, reports/**/findings.json, state/incidents/ and
state/degraded.jsonl, and reports:
  - Agent 2 diagnosis accuracy (diagnosis.grade, LLM diagnoses only; template fallbacks counted apart)
  - Agent 1 plan validity (LLM vs fallback plans, validator rejections and their reasons)
  - LLM fallback rate and source mix (api / cache / fallback) per component
  - fact-check pass rate of the findings reports (unverified_numbers)
  - LLM calls and latency per run
  - live investigation time, old (no triage) vs new (triage snapshot first)
The output is deterministic: the same recorded data always gives the same eval.md / eval.json.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime
from pathlib import Path

import yaml

POLICY = Path(__file__).parent / "policy.yaml"
COMPONENTS = ["planner", "diagnosis", "live investigation", "chat", "reporter", "fix summary"]
PURPOSE_COMPONENT = {
    "injection_select": "planner", "diagnosis": "diagnosis", "investigate_step": "live investigation",
    "investigate_conclude": "live investigation", "rag_chat": "chat", "report_score": "reporter",
    "report_experiment": "reporter", "report_summary": "reporter", "fix_summary": "fix summary",
}
DEGRADED_COMPONENT = {
    "Agent 1 (planner)": "planner", "Agent 2 (diagnosis)": "diagnosis",
    "Agent 2 (live investigation)": "live investigation", "Ask Agent 2 (chat)": "chat", "Fix summary": "fix summary",
}
# source classes, in table order: live LLM call, cached LLM answer, LLM (old runs did not record api/cache),
# fallback after an LLM failure, LLM switched off on purpose (--no-llm), rule (no LLM needed)
SOURCE_CLASSES = ["api", "cache", "llm", "fallback", "disabled", "rule"]
LIMIT_KEYS = ("w1_error_rate", "w1_p95_ms", "queue_waiting")  # tighter than policy defaults = a test run
FAILED_RESULTS = ("error", "invalid_json", "no_model", "no_api_key")


# ------------------------------------------------------------------ loading
def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue  # a run killed mid-write leaves a damaged last line
        if isinstance(e, dict):
            out.append(e)
    return out


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def source_class(source: str | None) -> str:
    s = (source or "").strip()
    if s.startswith("llm:api"):
        return "api"
    if s.startswith("llm:cache"):
        return "cache"
    if s.startswith("llm"):
        return "llm"
    if "llm_disabled" in s or "disabled" in s.lower() and "llm" in s.lower():
        return "disabled"
    if s.startswith("rule"):
        return "rule"
    return "fallback"


def is_llm(cls: str) -> bool:
    return cls in ("api", "cache", "llm")


def parse_since(since: str | float | None) -> float | None:
    """Epoch seconds, or an ISO date/time (local time when no zone is given)."""
    if since in (None, ""):
        return None
    try:
        return float(since)
    except (TypeError, ValueError):
        return datetime.fromisoformat(str(since)).timestamp()


def policy_limits(path: Path = POLICY) -> dict:
    abort = (yaml.safe_load(path.read_text()) or {}).get("defaults", {}).get("abort", {})
    return {k: abort[k] for k in LIMIT_KEYS if k in abort}


class Run:
    def __init__(self, d: Path):
        self.dir, self.id = d, d.name
        self.journal = _read_jsonl(d / "journal.jsonl")
        self.results = _read_json(d / "results.json") or []
        ev = d / "evidence"
        self.diagnoses = sorted(ev.glob("*.diagnosis.json")) if ev.is_dir() else []
        self.plans = {p.name.removesuffix(".plan.json"): _read_json(p) or {} for p in sorted(ev.glob("*.plan.json"))} \
            if ev.is_dir() else {}
        self.fix_summary = _read_json(ev / "fix_summary.json") or {}
        tss = [e["ts"] for e in self.journal if isinstance(e.get("ts"), (int, float))]
        tss += [(r.get("timeline") or {}).get("t_inject") or 0 for r in self.results if isinstance(r, dict)]
        self.ts = min((t for t in tss if t), default=None)

    def thresholds(self) -> list[dict]:
        out = [r.get("thresholds") or {} for r in self.results if isinstance(r, dict)]
        out += [p.get("thresholds") or {} for p in self.plans.values()]
        for e in self.journal:
            if e.get("event") == "agent1_plan":
                out += [(v or {}).get("abort") or {} for v in e.get("verdicts") or []]
        return [t for t in out if t]

    def test_limits(self, defaults: dict) -> list[str]:
        """Limits tighter than the policy defaults (e.g. `demo --queue-abort 20` to test the auto-stop)."""
        found = set()
        for t in self.thresholds():
            for k, v in defaults.items():
                if isinstance(t.get(k), (int, float)) and t[k] < v:
                    found.add(f"{k}={t[k]}")
        return sorted(found)


def select_runs(reports: Path, runs: list[str] | None = None, since: float | None = None,
                exclude_tests: bool = False, defaults: dict | None = None) -> tuple[list[Run], dict]:
    """Runs to score, and why the others were skipped."""
    defaults = policy_limits() if defaults is None else defaults
    dirs = sorted(d for d in reports.iterdir() if d.is_dir()
                  and ((d / "journal.jsonl").exists() or (d / "evidence").is_dir())) if reports.is_dir() else []
    chosen, skipped = [], {}
    for d in dirs:
        if runs and d.name not in runs:
            continue
        r = Run(d)
        if since is not None and not any((e.get("ts") or 0) >= since for e in r.journal) \
                and not (r.ts and r.ts >= since):
            skipped[r.id] = "before --since"
            continue
        if exclude_tests:
            if r.id.startswith("ks-"):
                skipped[r.id] = "kill-switch test"
                continue
            if lim := r.test_limits(defaults):
                skipped[r.id] = "test limits: " + ", ".join(lim)
                continue
        chosen.append(r)
    for r in runs or []:
        if not (reports / r).is_dir():
            skipped[r] = "not found"
    return chosen, skipped


def _events(run: Run, since: float | None) -> list[dict]:
    return [e for e in run.journal if since is None or (e.get("ts") or 0) >= since]


def _since_ok(run: Run, since: float | None) -> bool:
    return since is None or (run.ts or 0) >= since


# ------------------------------------------------------------------ metrics
def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "median_s": None, "mean_s": None, "min_s": None, "max_s": None}
    return {"n": len(xs), "median_s": round(statistics.median(xs), 1), "mean_s": round(statistics.fmean(xs), 1),
            "min_s": round(min(xs), 1), "max_s": round(max(xs), 1)}


def diagnosis_accuracy(runs: list[Run], since: float | None) -> dict:
    from chaosctl.agents.diagnosis import grade

    rows = []
    for run in runs:
        if not _since_ok(run, since):
            continue
        for p in run.diagnoses:
            exp = p.name.removesuffix(".diagnosis.json")
            diag, plan = _read_json(p), run.plans.get(exp)
            if not diag or not plan or "root_cause" not in diag:
                continue
            bundle = _read_json(run.dir / "evidence" / f"{exp}.json")
            cls = source_class(diag.get("source"))
            g = grade(diag, plan, bundle)
            rows.append({"exp_ref": f"{run.id}/{exp}", "fault": g["fault"], "target": g["actual_target"],
                         "source": diag.get("source", "")[:80], "source_class": cls, "llm": is_llm(cls),
                         "verdict": g["verdict"]})
    llm_rows = [r for r in rows if r["llm"]]
    counts = {v: sum(r["verdict"] == v for r in llm_rows) for v in ("CORRECT", "MISSED", "NO_IMPACT")}
    graded = counts["CORRECT"] + counts["MISSED"]
    fb = [r for r in rows if not r["llm"]]
    return {"diagnoses": len(rows), "llm": len(llm_rows), "fallback": len(fb), **counts,
            "accuracy_pct": _pct(counts["CORRECT"], graded),
            "fallback_template_verdicts": {v: sum(r["verdict"] == v for r in fb) for v in ("CORRECT", "MISSED", "NO_IMPACT")},
            "rows": rows}


def _rejection_kind(err: str) -> str:
    e = err.lower()
    if "unknown id" in e:
        return "cites an id not in the graph"
    if "not an enabled catalog candidate" in e:
        return "not an enabled catalog candidate"
    if "already planned" in e:
        return "same fault planned twice"
    if "rung" in e:
        return "unknown rung"
    return "other"


def _fallback_kind(reason: str) -> str:
    r = reason.lower()
    if "failed validation" in r:
        return "validator rejected the answer"
    if "budget" in r:
        return "LLM call budget exhausted"
    if "disabled" in r:
        return "LLM disabled"
    if "connecterror" in r or "no valid response" in r or "timeout" in r:
        return "API unreachable / no valid response"
    return "other"


def plan_validity(runs: list[Run], since: float | None) -> dict:
    plans, invalid, self_correct, degraded = [], [], 0, []
    for run in runs:
        for e in _events(run, since):
            ev = e.get("event")
            if ev == "agent1_plan":
                plans.append((run.id, e))
            elif ev == "agent1_invalid":
                invalid.append(e)
            elif ev == "agent1_self_correct":
                self_correct += 1
            elif ev == "degraded" and DEGRADED_COMPONENT.get(e.get("component")) == "planner":
                degraded.append(e)
    # a plan approved on the dashboard is journaled again by the run that executes it (planned_at = first ts)
    seen = {round(e["ts"], 3) for _, e in plans if isinstance(e.get("ts"), (int, float))}
    plans = [(r, e) for r, e in plans if not (e.get("planned_at") and round(e["planned_at"], 3) in seen)]
    by_class = {c: 0 for c in SOURCE_CLASSES}
    fallback_reasons: dict[str, int] = {}
    for _, e in plans:
        c = source_class(e.get("source"))
        by_class[c] += 1
        if c == "fallback":
            k = str(e.get("source", "")).removeprefix("fallback:")
            k = "invalid_plan (validator)" if k == "invalid_plan" else _fallback_kind(k)
            fallback_reasons[k] = fallback_reasons.get(k, 0) + 1
    kinds: dict[str, int] = {}
    for e in invalid:
        for err in e.get("errors") or []:
            k = _rejection_kind(str(err))
            kinds[k] = kinds.get(k, 0) + 1
    llm_n = by_class["api"] + by_class["cache"] + by_class["llm"]
    attempted = len(plans) - by_class["disabled"]
    return {"plans": len(plans), "by_source": by_class, "llm_plans": llm_n, "attempted_llm": attempted,
            "llm_valid_pct": _pct(llm_n, attempted), "fallback_reasons": dict(sorted(fallback_reasons.items())),
            "validator_rejections": len(invalid), "self_corrections": self_correct,
            "rejection_reasons": dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))),
            "partial_drops": sum(e.get("kind") == "warning" and "dropped" in str(e.get("reason")) for e in degraded),
            "fallback_events": [str(e.get("reason", ""))[:200] for e in degraded if e.get("kind") == "llm_fallback"]}


def load_incidents(state: Path, runs: list[Run], run_filter: bool, excluded: set[str], since: float | None) -> list[dict]:
    found: dict[str, dict] = {}
    for run in runs:  # copies kept with the run
        for p in sorted((run.dir / "investigations").glob("*.json")):
            if (d := _read_json(p)) and d.get("id"):
                found.setdefault(d["id"], d)
    if not run_filter:
        for p in sorted((state / "incidents").glob("inc-*.json")):
            if (d := _read_json(p)) and d.get("id"):
                found[d["id"]] = d
    run_ids = {r.id for r in runs}
    out = []
    for i, d in sorted(found.items()):
        if d.get("run_id") in excluded or (run_filter and d.get("run_id") not in run_ids):
            continue
        if since is not None and (d.get("started") or 0) < since:
            continue
        out.append(d)
    return out


def investigation_time(incidents: list[dict]) -> dict:
    rows = []
    for d in incidents:
        if not isinstance(d.get("started"), (int, float)) or not isinstance(d.get("ended"), (int, float)):
            continue
        triage = any(s.get("by") == "triage" for s in d.get("steps") or [])
        rows.append({"id": d["id"], "run_id": d.get("run_id"), "by": d.get("by"), "triage": triage,
                     "source_class": source_class(d.get("source")), "duration_s": round(d["ended"] - d["started"], 1),
                     "steps": len(d.get("steps") or [])})
    old = [r["duration_s"] for r in rows if not r["triage"]]
    new = [r["duration_s"] for r in rows if r["triage"]]
    new_llm = [r["duration_s"] for r in rows if r["triage"] and is_llm(r["source_class"])]
    old_llm = [r["duration_s"] for r in rows if not r["triage"] and is_llm(r["source_class"])]
    return {"old_no_triage": _stats(old), "new_triage": _stats(new), "old_llm_concluded": _stats(old_llm),
            "new_llm_concluded": _stats(new_llm), "rows": rows}


def load_findings(reports: Path, runs: list[Run], run_filter: bool, excluded: set[str]) -> list[tuple[str, dict]]:
    """Findings reports covering at least one selected run (any report when no --runs / --since narrows the set)."""
    # rglob does not descend into symlinked run folders (as used by local worktrees).
    paths = sorted(set(reports.rglob("findings.json")) | {r.dir / "findings.json" for r in runs})
    ids = {r.id for r in runs}
    out = []
    for p in paths:
        d = _read_json(p)
        if not isinstance(d, dict):
            continue
        covered = set(d.get("runs") or [])
        if not covered and p.parent.name in excluded:
            continue
        if run_filter and not covered and p.parent != reports and p.parent.name not in ids:
            continue
        if run_filter and covered and not covered & ids:
            continue
        if covered and covered <= excluded:
            continue
        if run_filter or excluded:
            # Combined reports can contain experiments from both selected and skipped runs.
            allowed = ids if run_filter else covered - excluded or {p.parent.name}
            exps = d.get("experiments") or []
            if isinstance(exps, list) and exps and all(e.get("exp_ref") for e in exps):
                exps = [e for e in exps if e["exp_ref"].split("/", 1)[0] in allowed]
                if not exps:
                    continue
                refs = {e["exp_ref"] for e in exps}
                d = dict(d, experiments=exps, unverified_numbers={
                    k: v for k, v in (d.get("unverified_numbers") or {}).items()
                    if k in refs or k == "summary"})
        out.append((str(p.relative_to(reports)), d))
    return out


def fact_check(findings: list[tuple[str, dict]]) -> dict:
    rows = []
    for name, d in findings:
        exps = d.get("experiments") or []
        flagged = d.get("unverified_numbers") or {}
        n = len(exps) if isinstance(exps, list) else (d.get("overall") or {}).get("experiments", 0)
        rows.append({"report": name, "source": d.get("source"), "experiments": n,
                     "flagged_experiments": sum(k != "summary" and bool(v) for k, v in flagged.items()),
                     "summary_flagged": bool(flagged.get("summary")),
                     "flagged_numbers": sum(len(v) for v in flagged.values()),
                     "flagged": {k: list(v) for k, v in sorted(flagged.items())}})
    llm_rows = [r for r in rows if is_llm(source_class(r["source"]))]
    total = sum(r["experiments"] for r in llm_rows)
    ok = total - sum(r["flagged_experiments"] for r in llm_rows)
    return {"reports": len(rows), "llm_reports": len(llm_rows), "experiments": total, "passed": ok,
            "pass_rate_pct": _pct(ok, total), "rows": rows}


def _call(e: dict) -> str:
    if e.get("source") in ("api", "cache"):
        return e["source"]
    return "failed" if e.get("result") in FAILED_RESULTS else "other"


def llm_calls(runs: list[Run], since: float | None, extra: list[tuple[str, list[dict]]]) -> dict:
    groups = [(r.id, [e for e in _events(r, since) if e.get("event") == "llm"]) for r in runs] + extra
    rows = []
    for name, calls in groups:
        if not calls:
            continue
        lat = [e["latency_s"] for e in calls if e.get("source") == "api" and isinstance(e.get("latency_s"), (int, float))]
        rows.append({"run": name, "calls": len(calls), "api": sum(_call(e) == "api" for e in calls),
                     "cache": sum(_call(e) == "cache" for e in calls),
                     "failed": sum(_call(e) == "failed" for e in calls),
                     "latency_total_s": round(sum(lat), 1), "latency_median_s": round(statistics.median(lat), 2) if lat else None,
                     "latency_max_s": round(max(lat), 2) if lat else None,
                     "models": sorted({e["model"] for e in calls if e.get("model")})})
    all_calls = [e for _, c in groups for e in c]
    lat = [e["latency_s"] for e in all_calls if e.get("source") == "api" and isinstance(e.get("latency_s"), (int, float))]
    by_purpose: dict[str, dict] = {}
    for e in all_calls:
        p = by_purpose.setdefault(str(e.get("purpose")), {"calls": 0, "api": 0, "cache": 0, "failed": 0, "lat": []})
        p["calls"] += 1
        if (k := _call(e)) in p:
            p[k] += 1
        if e.get("source") == "api" and isinstance(e.get("latency_s"), (int, float)):
            p["lat"].append(e["latency_s"])
    purposes = {k: {"calls": v["calls"], "api": v["api"], "cache": v["cache"], "failed": v["failed"],
                    "latency_median_s": round(statistics.median(v["lat"]), 2) if v["lat"] else None}
                for k, v in sorted(by_purpose.items())}
    return {"total": len(all_calls), "api": sum(_call(e) == "api" for e in all_calls),
            "cache": sum(_call(e) == "cache" for e in all_calls), "failed": sum(_call(e) == "failed" for e in all_calls),
            "latency_median_s": round(statistics.median(lat), 2) if lat else None,
            "latency_total_s": round(sum(lat), 1), "by_purpose": purposes, "rows": rows}


def components(runs: list[Run], since: float | None, diag: dict, plans: dict, inv: dict, findings, calls: dict,
               chat_degraded: list[dict]) -> dict:
    """Per component: decisions by source class (from the artifacts) and LLM calls by outcome (from the journals)."""
    dec = {c: {k: 0 for k in SOURCE_CLASSES} for c in COMPONENTS}
    dec["planner"] = dict(plans["by_source"])
    for r in diag["rows"]:
        dec["diagnosis"][r["source_class"]] += 1
    for r in inv["rows"]:
        dec["live investigation"][r["source_class"]] += 1
    for _, d in findings:
        src = d.get("source") or ""
        if src == "llm":
            srcs = {c.get("source") for c in d.get("llm_calls") or []}
            dec["reporter"]["api" if "api" in srcs else "cache" if "cache" in srcs else "llm"] += 1
        else:
            dec["reporter"][source_class(src)] += 1
    for run in runs:
        if _since_ok(run, since):
            for v in run.fix_summary.values():
                dec["fix summary"][source_class((v or {}).get("source"))] += 1
    dec["chat"]["fallback"] = len(chat_degraded)
    call_mix = {c: {"calls": 0, "api": 0, "cache": 0, "failed": 0} for c in COMPONENTS}
    for purpose, v in calls["by_purpose"].items():
        c = PURPOSE_COMPONENT.get(purpose)
        if c:
            for k in ("calls", "api", "cache", "failed"):
                call_mix[c][k] += v[k]
    reasons: dict[str, dict[str, int]] = {c: {} for c in COMPONENTS}
    deg = [e for r in runs for e in _events(r, since) if e.get("event") == "degraded"] + chat_degraded
    for e in deg:
        c = DEGRADED_COMPONENT.get(e.get("component"))
        if c and e.get("kind") == "llm_fallback":
            k = _fallback_kind(str(e.get("reason", "")))
            reasons[c][k] = reasons[c].get(k, 0) + 1
    out = {}
    for c in COMPONENTS:
        d = dec[c]
        llm_n, fb = d["api"] + d["cache"] + d["llm"], d["fallback"]
        out[c] = {"decisions": d, "decided": sum(d.values()), "fallback_rate_pct": _pct(fb, llm_n + fb),
                  "calls": call_mix[c], "fallback_reasons": dict(sorted(reasons[c].items()))}
    out["chat"]["note"] = "answered chat questions are not journaled; only fallbacks (state/degraded.jsonl) are counted"
    out["chat"]["fallback_rate_pct"] = None  # successful chat count is unavailable
    return out


def evaluate(reports: Path, state: Path, runs: list[str] | None = None, since: str | float | None = None,
             exclude_tests: bool = False, defaults: dict | None = None) -> dict:
    t0 = parse_since(since)
    chosen, skipped = select_runs(reports, runs, t0, exclude_tests, defaults)
    excluded = {k for k, v in skipped.items() if v != "before --since" and v != "not found"}
    run_filter = bool(runs)
    diag = diagnosis_accuracy(chosen, t0)
    plans = plan_validity(chosen, t0)
    incidents = load_incidents(state, chosen, run_filter, excluded, t0)
    inv = investigation_time(incidents)
    findings = load_findings(reports, chosen, run_filter or t0 is not None, excluded)
    extra = []
    if not run_filter:  # investigations outside a run journal to state/incidents/journal.jsonl
        ev = [e for e in _read_jsonl(state / "incidents" / "journal.jsonl")
              if e.get("event") == "llm" and (t0 is None or (e.get("ts") or 0) >= t0)]
        extra.append(("state/incidents (live investigations)", ev))
    for name, d in findings:
        extra.append((f"{name} (reporter)", [dict(c, event="llm") for c in d.get("llm_calls") or []]))
    calls = llm_calls(chosen, t0, extra)
    chat = [] if run_filter else [e for e in _read_jsonl(state / "degraded.jsonl")
                                  if DEGRADED_COMPONENT.get(e.get("component")) == "chat"
                                  and (t0 is None or (e.get("ts") or 0) >= t0)]
    comps = components(chosen, t0, diag, plans, inv, findings, calls, chat)
    return {"filters": {"runs": sorted(runs) if runs else None, "since": since, "exclude_tests": exclude_tests},
            "runs": [r.id for r in chosen], "skipped": dict(sorted(skipped.items())),
            "diagnosis": diag, "planner": plans, "components": comps, "fact_check": fact_check(findings),
            "llm_calls": calls, "investigation_time": inv}


# ------------------------------------------------------------------ rendering
def _f(v, unit: str = "") -> str:
    return "n/a" if v is None else f"{v}{unit}"


def _table(head: list[str], rows: list[list]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"] + \
        ["| " + " | ".join(str(c) for c in r) + " |" for r in rows] + [""]


def render_md(data: dict) -> str:
    d, p, fc, calls, inv, comps = (data["diagnosis"], data["planner"], data["fact_check"], data["llm_calls"],
                                   data["investigation_time"], data["components"])
    f = data["filters"]
    filt = ", ".join(x for x in [f"runs={','.join(f['runs'])}" if f["runs"] else "",
                                 f"since={f['since']}" if f["since"] else "",
                                 "tests excluded" if f["exclude_tests"] else ""] if x) or "none"
    graded = d["CORRECT"] + d["MISSED"]
    fb_total = sum(c["decisions"]["fallback"] for name, c in comps.items() if name != "chat")
    llm_total = sum(c["decisions"][k] for name, c in comps.items() if name != "chat" for k in ("api", "cache", "llm"))
    o, n = inv["old_llm_concluded"], inv["new_llm_concluded"]
    out = ["# chaosctl eval: agent scoreboard", "",
           f"Computed from recorded data only (no LLM calls). {len(data['runs'])} run folder(s); filters: {filt}.", "",
           "## Scoreboard", ""]
    if f["since"] is not None:
        out[4:4] = ["Since filters journal events by timestamp and incidents by start time. Diagnoses and fix "
                    "summaries have no artifact timestamp, so only runs starting at/after the cutoff are scored.", ""]
    out += _table(["Metric", "Value"], [
        ["Agent 2 blind diagnosis accuracy (LLM, runs with impact)",
         f"**{d['CORRECT']}/{graded} CORRECT ({_f(d['accuracy_pct'], '%')})**, {d['MISSED']} missed, "
         f"{d['NO_IMPACT']} no impact (not graded)"],
        ["Agent 2 diagnoses from the LLM", f"{d['llm']}/{d['diagnoses']} ({d['fallback']} template fallback)"],
        ["Agent 1 plans from the LLM (LLM not disabled)",
         f"**{p['llm_plans']}/{p['attempted_llm']} ({_f(p['llm_valid_pct'], '%')})**, "
         f"{p['validator_rejections']} validator rejection(s), {p['self_corrections']} self-correction(s)"],
        ["LLM fallback rate, components with recorded decisions (excludes chat)", f"{fb_total}/{fb_total + llm_total} decisions "
                                              f"({_f(_pct(fb_total, fb_total + llm_total), '%')})"],
        ["Findings fact-check pass rate", f"{fc['passed']}/{fc['experiments']} experiment write-ups "
                                         f"({_f(fc['pass_rate_pct'], '%')}) with no unverified number"],
        ["LLM calls", f"{calls['total']} ({calls['api']} api, {calls['cache']} cache, {calls['failed']} failed); "
                      f"median api latency {_f(calls['latency_median_s'], ' s')}"],
        ["Live investigation, LLM-concluded (median)",
         f"old (no triage) {_f(o['median_s'], ' s')} (n={o['n']}) -> new (triage) {_f(n['median_s'], ' s')} (n={n['n']})"],
    ])
    out += ["## Agent 2: blind diagnosis accuracy", "",
            "Graded with `diagnosis.grade` against the injected fault. Only LLM diagnoses count; template fallbacks "
            "are listed but not scored.", ""]
    out += _table(["Experiment", "Fault", "Target", "Source", "Verdict"],
                  [[r["exp_ref"], r["fault"], r["target"], r["source_class"] if r["llm"] else "FALLBACK (template)",
                    r["verdict"] if r["llm"] else f"({r['verdict']}, not counted)"] for r in d["rows"]])
    out += ["## Agent 1: plan validity", ""]
    bs = p["by_source"]
    out += _table(["Plans", "LLM api", "LLM cache", "LLM (unrecorded)", "Fallback", "LLM disabled",
                   "Validator rejections", "Self-corrections", "Partial drops"],
                  [[p["plans"], bs["api"], bs["cache"], bs["llm"], bs["fallback"], bs["disabled"],
                    p["validator_rejections"], p["self_corrections"], p["partial_drops"]]])
    if p["rejection_reasons"]:
        out += ["Validator rejection reasons (per error):", ""]
        out += _table(["Reason", "Count"], [[k, v] for k, v in p["rejection_reasons"].items()])
    if p["fallback_reasons"]:
        out += _table(["Fallback plan because", "Count"], [[k, v] for k, v in p["fallback_reasons"].items()])
    out += ["## LLM fallback rate and source mix per component", "",
            "Decisions = artifacts the component produced (plan, diagnosis, investigation, report, fix summary). "
            "Calls = journaled LLM calls (a failed call may still be retried on the fallback model).", ""]
    rows = []
    for c in COMPONENTS:
        x = comps[c]
        dd, cm = x["decisions"], x["calls"]
        rows.append([c, x["decided"], dd["api"], dd["cache"], dd["llm"], dd["fallback"], dd["disabled"] + dd["rule"],
                     _f(x["fallback_rate_pct"], "%"), f"{cm['calls']} ({cm['api']}/{cm['cache']}/{cm['failed']})"])
    out += _table(["Component", "Decisions", "api", "cache", "llm (old)", "fallback", "disabled/rule",
                   "Fallback rate", "Calls (api/cache/failed)"], rows)
    reasons = [[c, k, v] for c in COMPONENTS for k, v in comps[c]["fallback_reasons"].items()]
    if reasons:
        out += _table(["Component", "Fallback reason", "Count"], reasons)
    out += [f"Chat: {comps['chat']['note']}.", ""]
    out += ["## Findings report fact-check", ""]
    out += ["Experiment totals cover selected experiment write-ups when exp_ref is recorded. Summary flags are "
            "reported separately. Reporter calls describe the whole report; older reports without experiment refs "
            "are counted as a whole when they cover a selected run.", ""]
    out += _table(["Report", "Source", "Experiments", "Flagged", "Summary flagged", "Unverified numbers"],
                  [[r["report"], r["source"], r["experiments"], r["flagged_experiments"],
                    "yes" if r["summary_flagged"] else "no",
                    "; ".join(f"{k}: {', '.join(v)}" for k, v in r["flagged"].items()) or "-"] for r in fc["rows"]])
    out += ["## LLM calls and latency per run", ""]
    out += _table(["Run", "Calls", "api", "cache", "failed", "api latency total s", "median s", "max s", "Models"],
                  [[r["run"], r["calls"], r["api"], r["cache"], r["failed"], r["latency_total_s"],
                    _f(r["latency_median_s"]), _f(r["latency_max_s"]), ", ".join(r["models"])] for r in calls["rows"]])
    out += _table(["Purpose", "Calls", "api", "cache", "failed", "median api latency s"],
                  [[k, v["calls"], v["api"], v["cache"], v["failed"], _f(v["latency_median_s"])]
                   for k, v in calls["by_purpose"].items()])
    out += ["## Agent 2 live investigation time (ended - started)", ""]
    out += _table(["Group", "n", "median s", "mean s", "min s", "max s"],
                  [[label, s["n"], _f(s["median_s"]), _f(s["mean_s"]), _f(s["min_s"]), _f(s["max_s"])]
                   for label, s in [("old: no triage, all", inv["old_no_triage"]),
                                    ("old: no triage, LLM-concluded", inv["old_llm_concluded"]),
                                    ("new: triage first, all", inv["new_triage"]),
                                    ("new: triage first, LLM-concluded", inv["new_llm_concluded"])]])
    out += _table(["Incident", "Run", "By", "Triage", "Source", "Steps", "Duration s"],
                  [[r["id"], r["run_id"] or "-", r["by"], "yes" if r["triage"] else "no", r["source_class"],
                    r["steps"], r["duration_s"]] for r in inv["rows"]])
    if data["skipped"]:
        out += ["## Skipped", ""]
        out += _table(["Run", "Why"], [[k, v] for k, v in data["skipped"].items()])
    return "\n".join(out).rstrip() + "\n"


def write(data: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "eval.md", out_dir / "eval.json"
    md.write_text(render_md(data))
    js.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return md, js


_RUN_SPLIT = re.compile(r"[,\s]+")


def split_runs(values: list[str] | None) -> list[str] | None:
    out = [r for v in values or [] for r in _RUN_SPLIT.split(v) if r]
    return out or None
