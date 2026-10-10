"""Deterministic resilience score per experiment (weights and constants in scoring.yaml).

Inputs are only the evidence bundle (reports/<run>/evidence/<exp>.json) and the run result
(results.json entry), so the score never depends on an LLM. Each component explains its inputs,
so every number in the report can be traced back to the evidence.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from chaosctl.agents.diagnosis import blind, detect

CONFIG = yaml.safe_load((Path(__file__).parent / "scoring.yaml").read_text())
DEFAULT_P95_LIMIT_MS = 20000
DEFAULT_RECOVERY_DEADLINE_S = 120


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _c(score: float | None, why: str) -> dict:
    return {"score": None if score is None else round(score, 1), "why": why}


def availability(trace_after: list[dict]) -> dict:
    if not trace_after:
        return _c(None, "no samples after injection")
    n = sum((s.get("w1") or {}).get("n") or 0 for s in trace_after)
    ok = sum((s.get("w1") or {}).get("ok") or 0 for s in trace_after)
    if n == 0:
        return _c(0, "no W1 response at all after injection")
    return _c(100 * ok / n, f"{ok}/{n} W1 responses successful across post-injection samples")


def latency(trace_after: list[dict], base_p95: float | None, limit_ms: float) -> dict:
    peaks = [(s.get("w1") or {}).get("p95_ms") for s in trace_after]
    peaks = [p for p in peaks if p is not None]
    if not peaks or not base_p95:
        return _c(None, "no W1 p95 samples or no baseline")
    peak, lo = max(peaks), CONFIG["latency"]["ok_ratio"] * base_p95
    why = f"peak p95 {peak / 1000:.1f} s vs baseline {base_p95 / 1000:.1f} s, abort limit {limit_ms / 1000:.0f} s"
    if peak <= lo:
        return _c(100, why)
    if peak >= limit_ms or limit_ms <= lo:
        return _c(0, why)
    return _c(100 * (limit_ms - peak) / (limit_ms - lo), why)


def self_recovery(bundle: dict, result: dict, deadline_s: float) -> dict:
    cfg = CONFIG["self_recovery"]
    wd = result.get("watchdog") or {}
    t = bundle.get("recovery_time_s", result.get("recovery_time_s"))
    manual = bundle.get("manual_action_needed", result.get("manual_action_needed"))
    if wd.get("recovered") is False:
        return _c(cfg["locked"], "recovery failed; harness LOCKED")
    if manual:
        return _c(cfg["watchdog_restart"], "watchdog had to restart a workload")
    if t is not None:
        frac = _clamp((t - cfg["fast_s"]) / max(1, deadline_s - cfg["fast_s"]))
        return _c(100 - (100 - cfg["slow_score"]) * frac, f"SLIs back to baseline {t:.0f} s after injection, no help")
    if (bundle.get("timeline") or {}).get("saw_symptom") is False:
        return _c(100, "no symptom observed, nothing to recover from")
    return _c(cfg["slis_never_recovered"], "pods recovered without help, SLIs not back to baseline within the window")


def data_integrity(fates: dict) -> dict:
    summary = (fates or {}).get("summary") or {}
    total = sum(summary.values())
    if not total:
        return _c(None, "no in-flight requests recorded")
    bad = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in summary.items() if k != "completed_once" and v)
    return _c(100 * summary.get("completed_once", 0) / total,
              f"{summary.get('completed_once', 0)}/{total} in-flight orders completed exactly once"
              + (f"; {bad}" if bad else ""))


def detectability(hit_ids: set[str]) -> dict:
    if "detectability_gap" in hit_ids:
        return _c(0, "n8n-main stayed Ready through the incident (detectability_gap)")
    if hit_ids & {"consumers_down", "queue_unreachable"}:
        return _c(100, "health checks reflected the outage")
    return _c(None, "no consumer/queue outage, so health-check visibility was not tested")


def band(total: float) -> str:
    for lower, label in sorted(CONFIG["bands"].items(), key=lambda x: -x[0]):
        if total >= lower:
            return label
    return "failing"


def score_experiment(bundle: dict, result: dict | None = None) -> dict:
    result = result or {}
    status = result.get("status") or bundle.get("status")
    if status == "skipped":
        return {"scored": False, "why": f"skipped ({result.get('skip_reason')})"}
    th = result.get("thresholds") or {}
    after = [s for s in (bundle.get("trace") or []) if s.get("t", -1) >= 0]
    base_p95 = ((bundle.get("baseline") or {}).get("w1") or {}).get("p95_ms")
    hit_ids = {h["id"] for h in detect(blind(bundle))}
    comps = {
        "availability": availability(after),
        "self_recovery": self_recovery(bundle, result, th.get("recovery_deadline_s") or DEFAULT_RECOVERY_DEADLINE_S),
        "latency": latency(after, base_p95, th.get("w1_p95_ms") or DEFAULT_P95_LIMIT_MS),
        "data_integrity": data_integrity(bundle.get("fates") or result.get("fates")),
        "detectability": detectability(hit_ids),
    }
    w = CONFIG["weights"]
    present = {k: c["score"] for k, c in comps.items() if c["score"] is not None}
    coverage = sum(w[k] for k in present)
    if not coverage:
        return {"scored": False, "why": "no measurable component", "components": comps}
    total = sum(w[k] * v for k, v in present.items()) / coverage
    return {"scored": True, "total": round(total, 1), "band": band(total), "coverage": round(coverage, 2),
            "components": comps, "weights": w}


def score_overall(scores: list[dict]) -> dict | None:
    totals = [s["total"] for s in scores if s.get("scored")]
    if not totals:
        return None
    mean = sum(totals) / len(totals)
    return {"mean": round(mean, 1), "worst": min(totals), "band": band(mean), "experiments": len(totals)}
