"""Agent 2, report mode: writes the findings report from the recorded evidence (docs/KNOWLEDGE_BASE.md).

Same pattern as the other agents: the LLM writes and judges, deterministic validators check that every
cited id exists, fixes come from the playbook and scores are well-formed, and a fact-check traces every
number in the text back to the measurements. Invalid output is retried with the errors as feedback.
Unlike the blind diagnosis, this runs after the fact and is told what was injected.

Resilience scores are calculated by score.py from recorded measurements. Agent 2 writes the analysis
and recommendations using those fixed scores; report generation makes no score-judgement calls.
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from chaosctl import llm
from chaosctl.agents.diagnosis import PLAYBOOK
from chaosctl.score import CONFIG as SCORE_CONFIG

COMPONENTS = list(SCORE_CONFIG["weights"])
SYSTEM = """SYSTEM UNDER TEST: n8n in queue mode on Kubernetes. n8n-main receives webhooks and enqueues jobs in Redis;
n8n-worker pods run the jobs, call mock-api (4 s delay) and write orders to Postgres. W1 and W2 are WORKFLOWS (not
workers): W1 is synchronous (the client waits for the result), W2 is asynchronous (the client gets 200 OK at once
and the work happens later). The load generator sends synthetic orders and checks Postgres for every request.

ORDER FATES (in-flight orders at injection):
- completed_once: client got success and exactly one order row exists (good)
- lost_silently: client got 200 OK but no order row exists (silent data loss, worst)
- lost_visible: client got an error or timeout and no order row exists (visible loss)
- phantom_success: client got an error but the order WAS saved (inconsistent state)
- duplicated: more than one order row (double execution)
- success_without_row: W1 client got success but no row exists"""
SEVERITIES = ["Critical", "High", "Medium", "Low"]
GENERIC_EVIDENCE = ["trace", "fates", "timeline", "baseline", "agent1_plan", "guard", "agent2_diagnosis", "knowledge"]
TIMEOUT_S = 90
ATTEMPTS = 3            # first answer + up to 2 corrections
SAMPLES = 3             # independent scorings per experiment; the median per component is the final score
SAMPLE_TEMPERATURE = 0.5  # enough randomness that the samples really are independent judgements
RUBRIC = """Score each component 0-100 (higher = more resilient), or null if the facts cannot support a score:
- availability: share of W1 (sync) requests that succeeded after injection
- self_recovery: did the system recover without help, and how fast (SLIs back within ~30 s is excellent; needing
  the watchdog to restart something is poor; a LOCKED harness is 0)
- latency: how far peak W1 p95 rose above baseline relative to the abort limit
- data_integrity: share of in-flight orders that completed exactly once (lost, duplicated, phantom are failures)
- detectability: whether health checks/readiness showed the problem (green health checks during an outage = 0)
You get the formula score as a REFERENCE and earlier experiments you scored as CALIBRATION examples. Judge from
the evidence: follow the reference where it is fair, depart from it where the evidence shows it misjudges the
component, and then say why in the reason. Stay consistent with how you scored similar past experiments.
Weights (the harness computes the weighted total from your component scores): """ + ", ".join(
    f"{k} {v:.0%}" for k, v in SCORE_CONFIG["weights"].items()) + "."


def band(total: int | None) -> str:
    if total is None:
        return "not scored"
    return next(label for lower, label in sorted(SCORE_CONFIG["bands"].items(), key=lambda x: -x[0]) if total >= lower)


class ComponentScore(BaseModel):
    component: str = Field(description="one of " + ", ".join(COMPONENTS))
    score: int | None = Field(description="0-100, or null if the facts cannot support a score")
    reason: str = Field(description="one sentence citing the measured numbers")


class ExperimentReport(BaseModel):
    exp_ref: str
    headline: str = Field(description="one sentence: what happened")
    why_chosen: str
    expected_vs_observed: str = Field(description="which predictions held and which were refuted, and why")
    impact: str = Field(description="customer impact on W1, W2 and orders")
    recovery: str
    root_cause: str = Field(description="root cause; say whether the blind diagnosis was right")
    evidence: list[str] = Field(description="ids from ALLOWED EVIDENCE IDS that support the analysis")


class ScoreJudgement(BaseModel):
    exp_ref: str
    scores: list[ComponentScore]


def weighted_total(scores: dict[str, float | None]) -> float | None:
    """Weighted mean of the non-null component scores (arithmetic only; the judgement is in the components)."""
    present = [(SCORE_CONFIG["weights"][k], v) for k, v in scores.items() if v is not None]
    weight = sum(w for w, _ in present)
    return round(sum(w * v for w, v in present) / weight, 1) if weight else None


def median_judgement(samples: list[ScoreJudgement]) -> dict:
    """Median per component over the independent samples; the reason comes from the sample closest to it."""
    comps = {}
    for name in COMPONENTS:
        vals = [c for j in samples for c in j.scores if c.component == name]
        nums = sorted(c.score for c in vals if c.score is not None)
        if len(nums) * 2 <= len(vals):          # most samples say "no data"
            comps[name] = {"score": None, "reason": next((c.reason for c in vals if c.score is None), ""), "samples": []}
            continue
        mid = nums[len(nums) // 2] if len(nums) % 2 else (nums[len(nums) // 2 - 1] + nums[len(nums) // 2]) / 2
        best = min((c for c in vals if c.score is not None), key=lambda c: abs(c.score - mid))
        comps[name] = {"score": mid, "reason": best.reason, "samples": nums}
    total = weighted_total({k: c["score"] for k, c in comps.items()})
    totals = [weighted_total({c.component: c.score for c in j.scores}) for j in samples]
    totals = [t for t in totals if t is not None]
    return {"total": total, "verdict": band(total), "components": comps, "samples": len(samples),
            "spread": round(max(totals) - min(totals), 1) if totals else None}


class Weakness(BaseModel):
    title: str
    severity: str = Field(description="Critical | High | Medium | Low")
    explanation: str
    evidence: list[str] = Field(description="detector ids")
    seen_in: list[str] = Field(description="experiment refs")
    fixes: list[str] = Field(description="playbook ids")


class FixRecommendation(BaseModel):
    playbook_id: str
    priority: int = Field(description="1 = do first")
    why: str
    addresses: list[str] = Field(description="titles of the weaknesses it addresses")


class FindingsSummary(BaseModel):
    executive_summary: str = Field(description="3-6 sentences for a non-expert")
    environment: str = Field(description="the system under test and its baseline, 2-3 sentences")
    weaknesses: list[Weakness] = Field(description="ranked, most severe first")
    fixes: list[FixRecommendation]
    limitations: str = Field(description="what these experiments did not show")


# ------------------------------------------------------------ facts given to the model

def _trace_stats(bundle: dict) -> dict:
    after = [s for s in bundle.get("trace") or [] if s.get("t", -1) >= 0]
    w1 = [s.get("w1") or {} for s in after]
    q = [s.get("queue") or {} for s in after]
    p95 = [x["p95_ms"] for x in w1 if x.get("p95_ms") is not None]
    main_ready = [(s.get("pods") or {}).get("n8n-main", {}).get("ready") for s in after]
    return {
        "samples_after_injection": len(after),
        "w1_responses_after_injection": sum(x.get("n") or 0 for x in w1),
        "w1_successful_after_injection": sum(x.get("ok") or 0 for x in w1),
        "w1_peak_p95_ms": max(p95) if p95 else None,
        "w1_last_p95_ms": p95[-1] if p95 else None,
        "w2_min_success_rate": min((s.get("w2") or {}).get("success_rate") or 1 for s in after) if after else None,
        "queue_max_waiting": max((x.get("wait") or 0) for x in q) if q else None,
        "queue_last_waiting": (q[-1].get("wait") if q else None),
        "seconds_redis_unreachable": sum(1 for x in q if x.get("reachable") is False),
        "n8n_main_ready_throughout": all(r is not False for r in main_ready),
    }


def experiment_facts(exp: dict, kb) -> dict:
    res, item, b = exp["result"], exp["plan_item"], exp["bundle"]
    know = kb.get(res.get("fault") or item.get("fault") or "unknown")
    diag = exp.get("diagnosis") or {}
    th = res.get("thresholds") or {}
    return {
        "exp_ref": f"{exp['run_id']}/{exp['exp_id']}",
        "fault": {"name": know.fault, "alias": know.alias, "title": know.title, "summary": know.summary,
                  "knowledge_hypothesis": know.hypothesis, "expected_symptoms": know.symptoms.expected,
                  "possible_symptoms": know.symptoms.possible},
        "target": res.get("target"), "status": res.get("status"), "baseline": b.get("baseline"),
        "skip_reason": res.get("skip_reason"), "abort_reason": res.get("abort_reason"),
        "agent1": {"source": exp.get("plan_source"), "reason": item.get("reason"),
                   "hypothesis": item.get("hypothesis"), "cites": item.get("cites")},
        "guard": {"verdict": (exp.get("guard") or {}).get("verdict"),
                  "blast_radius": (exp.get("guard") or {}).get("blast_radius"),
                  "abort_limits": (exp.get("guard") or {}).get("abort")},
        "abort_limit_w1_p95_ms": th.get("w1_p95_ms"), "recovery_deadline_s": th.get("recovery_deadline_s"),
        "timeline_s_after_injection": res.get("timeline"),
        "recovery_time_s": res.get("recovery_time_s"),
        "manual_action_needed": res.get("manual_action_needed"),
        "watchdog_recovered": (res.get("watchdog") or {}).get("recovered"),
        "restarts_delta": res.get("restarts_delta") or b.get("restarts_delta"),
        "in_flight_order_fates": ((res.get("fates") or b.get("fates") or {}).get("summary")),
        "measurements": _trace_stats(b),
        "detectors_fired": [{k: h[k] for k in ("id", "severity", "detail")} for h in exp["detectors"]],
        "agent2_blind_diagnosis": {"source": diag.get("source"), "root_cause": diag.get("root_cause"),
                                   "failure_chain": diag.get("failure_chain"),
                                   "fixes": [f["playbook_id"] for f in diag.get("fixes") or []],
                                   "graded_correct": (exp.get("grade") or {}).get("root_cause_correct")} if diag else None,
    }


def plain_facts(facts: dict) -> list[str]:
    """Key outcomes as plain sentences, so a small model cannot misread the numbers."""
    fs = facts.get("in_flight_order_fates") or {}
    m = facts.get("measurements") or {}
    out = [f"{facts['exp_ref']}: injected {facts['fault']['title']} on {facts['target']}; status {facts['status']}."]
    if fs:
        out.append(f"Of {sum(fs.values())} in-flight orders: {fs.get('completed_once', 0)} completed once, "
                   f"{fs.get('lost_silently', 0)} lost SILENTLY (client told OK, no row), "
                   f"{fs.get('lost_visible', 0)} lost VISIBLY (client got an error, no row), "
                   f"{fs.get('phantom_success', 0)} phantom successes (client got an error, row saved), "
                   f"{fs.get('duplicated', 0)} duplicated.")
    if m.get("w2_min_success_rate") is not None:
        out.append(f"W2 clients' lowest success rate after injection: {m['w2_min_success_rate']:.0%} "
                   "(a 200 OK to W2 only means the job was accepted).")
    if facts.get("recovery_time_s") is not None:
        out.append(f"SLIs back to baseline {facts['recovery_time_s']} s after injection; manual action needed: "
                   f"{facts.get('manual_action_needed')}.")
    elif facts.get("status") != "skipped":
        out.append("SLIs did not return to baseline within the observation window.")
    return out


# claims that must not appear when the matching fate count is 0
_CLAIMS = {"lost_silently": re.compile(r"silent(ly)?\s+(data\s+)?(loss|lost|drop)|lost\s+silently|silently\s+lost", re.I)}


def false_claims(text: str, facts: list[dict]) -> list[str]:
    totals: dict[str, int] = {}
    for f in facts:
        for k, v in (f.get("in_flight_order_fates") or {}).items():
            totals[k] = totals.get(k, 0) + v
    negated = re.compile(r"\b(no|0|zero|without|not|never|none)\b[\w\s,()-]{0,20}$", re.I)
    out = []
    for k, pat in _CLAIMS.items():
        if totals and totals.get(k, 0) == 0 and any(not negated.search(text[max(0, m.start() - 30):m.start()])
                                                     for m in pat.finditer(text)):
            out.append(f"claims {k.replace('_', ' ')} but the count is 0")
    return out


def _allowed_evidence(facts: dict) -> list[str]:
    return sorted({d["id"] for d in facts["detectors_fired"]} | set(GENERIC_EVIDENCE))


# ------------------------------------------------------------ validation and fact-check

_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def _numbers(obj) -> set[float]:
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return {float(n) for n in _NUM.findall(text)}


def grounded_numbers(facts) -> set[float]:
    """Numbers the text may use: every measured number, ms->s conversions, ratios as percentages."""
    base = _numbers(facts)
    out = set(base)
    for x in base:
        out |= {round(x / 1000, 1), round(x / 1000), round(x * 100), round(x * 100, 1), round(x, 1), round(x)}
    ints = [x for x in base if x == int(x) and 0 < x <= 1000]
    for a in ints:
        for b in ints:
            if a <= b:
                out |= {round(100 * a / b), round(100 * a / b, 1), round(b / a, 1)}
    return out


def ungrounded(text: str, allowed: set[float]) -> list[str]:
    bad = []
    for n in _NUM.findall(text):
        v = float(n)
        if v in allowed or any(abs(v - a) <= 0.01 * max(1, abs(a)) for a in allowed):
            continue
        bad.append(n)
    return sorted(set(bad), key=float)


def _text(m: BaseModel) -> str:
    """Prose fields only (ids and scores excluded) for the fact-check."""
    d = m.model_dump()
    skip = {"exp_ref", "evidence", "seen_in", "fixes", "playbook_id", "component", "score", "total", "priority",
            "overall_score", "addresses"}

    def walk(x):
        if isinstance(x, dict):
            return " ".join(walk(v) for k, v in x.items() if k not in skip)
        if isinstance(x, list):
            return " ".join(walk(v) for v in x)
        return x if isinstance(x, str) else ""
    return walk(d)


def validate_experiment(r: ExperimentReport, facts: dict) -> list[str]:
    errs = []
    if r.exp_ref != facts["exp_ref"]:
        errs.append(f"exp_ref must be {facts['exp_ref']!r}")
    bad = [e for e in r.evidence if e not in _allowed_evidence(facts)]
    if bad:
        errs.append(f"unknown evidence ids {bad}; evidence may only contain {_allowed_evidence(facts)}")
    return errs


def validate_judgement(j: ScoreJudgement, facts: dict) -> list[str]:
    errs = []
    if j.exp_ref != facts["exp_ref"]:
        errs.append(f"exp_ref must be {facts['exp_ref']!r}")
    names = [c.component for c in j.scores]
    if sorted(names) != sorted(COMPONENTS):
        errs.append(f"scores must contain each of {COMPONENTS} exactly once")
    if any(c.score is not None and not 0 <= c.score <= 100 for c in j.scores):
        errs.append("component scores must be 0-100 or null")
    if any(len(c.reason.strip()) < 15 for c in j.scores):
        errs.append("every component needs a reason citing the evidence")
    return errs


def validate_summary(s: FindingsSummary, exp_reports: list[ExperimentReport], fired: set[str]) -> list[str]:
    errs, refs = [], {r.exp_ref for r in exp_reports}
    for w in s.weaknesses:
        if w.severity not in SEVERITIES:
            errs.append(f"weakness {w.title!r}: severity must be one of {SEVERITIES}")
        if bad := [e for e in w.evidence if e not in fired]:
            hint = " (playbook ids belong in fixes)" if set(bad) & set(PLAYBOOK) else ""
            errs.append(f"weakness {w.title!r}: evidence {bad} are not detector ids{hint}; use only {sorted(fired)}")
        if bad := [e for e in w.seen_in if e not in refs]:
            errs.append(f"weakness {w.title!r}: unknown experiment refs {bad}")
        if bad := [f for f in w.fixes if f not in PLAYBOOK]:
            errs.append(f"weakness {w.title!r}: fixes not in playbook {bad}; use only {sorted(PLAYBOOK)}")
    ranks = [SEVERITIES.index(w.severity) for w in s.weaknesses if w.severity in SEVERITIES]
    if ranks != sorted(ranks):
        errs.append("weaknesses must be ordered most severe first")
    if bad := [f.playbook_id for f in s.fixes if f.playbook_id not in PLAYBOOK]:
        errs.append(f"fixes not in playbook {bad}; use only {sorted(PLAYBOOK)}")
    return errs


# ------------------------------------------------------------ prompts

def experiment_prompt(facts: dict, feedback: str | None) -> str:
    return f"""You are Agent 2 of a chaos-engineering harness, now writing the findings report for one experiment.
The experiment is over, so you are told what was injected. Use ONLY the facts below. Every number you write must
come from the facts (you may convert ms to s and counts to percentages). Do not invent causes the facts do not
support, and use the exact fate names: e.g. do not call visible losses "silent".

{SYSTEM}

KEY FACTS IN PLAIN WORDS:
{chr(10).join(plain_facts(facts))}

ALL FACTS:
{json.dumps(facts, indent=1, default=str)}

ALLOWED EVIDENCE IDS: {json.dumps(_allowed_evidence(facts))}

{f"YOUR PREVIOUS ANSWER WAS REJECTED, fix these problems: {feedback}" if feedback else ""}
Return JSON with exp_ref "{facts['exp_ref']}", a one-sentence headline, why_chosen, expected_vs_observed, impact,
recovery, root_cause and evidence. (The resilience score is judged in a separate step.)"""


def score_prompt(facts: dict, reference: dict, calibration: list[dict], feedback: str | None) -> str:
    ref = {k: {"score": c["score"], "evidence": c["why"]} for k, c in (reference.get("components") or {}).items()}
    cal = [{k: e.get(k) for k in ("exp_ref", "fault", "target", "status", "key_facts", "reference", "ai_scores",
                                  "ai_total")} for e in calibration]
    return f"""You are Agent 2 of a chaos-engineering harness, judging the resilience of the system in one experiment.
Use ONLY the facts below.

{SYSTEM}

KEY FACTS IN PLAIN WORDS:
{chr(10).join(plain_facts(facts))}

ALL FACTS:
{json.dumps(facts, indent=1, default=str)}

REFERENCE (formula score, a heuristic; total {reference.get('total')}):
{json.dumps(ref, indent=1)}

CALIBRATION (your earlier judgements on real experiments; stay consistent with them):
{json.dumps(cal, indent=1, default=str) if cal else "none yet: this is the first scored experiment"}

SCORING RUBRIC:
{RUBRIC}
{f"YOUR PREVIOUS ANSWER WAS REJECTED, fix these problems: {feedback}" if feedback else ""}
Return JSON with exp_ref "{facts['exp_ref']}" and scores: all five components, each with score (0-100 or null) and
a reason that cites the measured numbers (and says why if you depart from the reference)."""


def summary_prompt(facts: list[dict], exp_reports: list[ExperimentReport], fired: set[str],
                   feedback: str | None, scores: dict | None = None) -> str:
    scores = {k: {"total": v["total"], "verdict": v["verdict"],
                  "components": {c: x["score"] for c, x in v["components"].items()}} for k, v in (scores or {}).items()}
    fixes = {k: {"title": v["title"], "change": v["change"], "addresses": v["addresses"], "effort": v["effort"],
                 "impact": v["impact"]} for k, v in PLAYBOOK.items()}
    by_ref = {f["exp_ref"]: f for f in facts}
    per_exp = [{"exp_ref": r.exp_ref, "fault": by_ref[r.exp_ref]["fault"]["title"], "status": by_ref[r.exp_ref]["status"],
                "headline": r.headline, "impact": r.impact, "recovery": r.recovery, "root_cause": r.root_cause,
                "in_flight_order_fates": by_ref[r.exp_ref]["in_flight_order_fates"],
                "recovery_time_s": by_ref[r.exp_ref]["recovery_time_s"],
                "manual_action_needed": by_ref[r.exp_ref]["manual_action_needed"],
                "formula_resilience_score": scores.get(r.exp_ref)} for r in exp_reports]
    detectors = [{"exp_ref": f["exp_ref"], **d} for f in facts for d in f["detectors_fired"]]
    agent2 = [{"exp_ref": f["exp_ref"], "fixes": (f["agent2_blind_diagnosis"] or {}).get("fixes")} for f in facts]
    return f"""You are Agent 2 of a chaos-engineering harness, writing the summary of the findings report across all
experiments below. Use ONLY these facts; every number must come from them. Use the exact fate names (a visible
loss is not a silent loss) and do not contradict the recorded recovery.

{SYSTEM}

KEY FACTS IN PLAIN WORDS:
{chr(10).join(line for f in facts for line in plain_facts(f))}

EXPERIMENT ANALYSES (already written by you):
{json.dumps(per_exp, indent=1)}

DETECTORS THAT FIRED (id, severity, detail, per experiment):
{json.dumps(detectors, indent=1)}

FIXES AGENT 2 RECOMMENDED DURING THE BLIND DIAGNOSES:
{json.dumps(agent2)}

BASELINE: {json.dumps(facts[0].get('baseline') if facts else None, default=str)}

PLAYBOOK (the ONLY fixes you may recommend):
{json.dumps(fixes, indent=1)}
{f"YOUR PREVIOUS ANSWER WAS REJECTED, fix these problems: {feedback}" if feedback else ""}
Return JSON: executive_summary, environment, weaknesses (merge related detectors into customer-facing weaknesses,
most severe first; evidence = detector ids from {sorted(fired)}; seen_in = experiment refs; fixes = playbook ids),
fixes (playbook ids ranked by priority, 1 = do first, with why and the weakness titles they address; recommend a
fix only if its "change" actually addresses the observed cause, e.g. Redis persistence only helps when Redis lost
its state),
and limitations. Quote the resilience scores as given; do not change them."""


# ------------------------------------------------------------ run

def _ask(purpose: str, prompt_fn, model_cls, validate, fact_check, journal, call):
    """Hard errors (validate) must be fixed; untraceable numbers (fact_check) get one retry, then are flagged."""
    feedback, kept = None, None
    for attempt in range(ATTEMPTS):
        out = llm.generate_json(purpose, prompt_fn(feedback), model_cls, journal=journal, call=call,
                                timeout_s=TIMEOUT_S)
        if errs := validate(out):
            feedback = "; ".join(errs)
            continue
        bad = fact_check(out)
        if not bad or attempt == ATTEMPTS - 1:
            return out, bad
        if kept is None or len(bad) < len(kept[1]):
            kept = (out, bad)
        feedback = f"fact-check failed: {bad}; use only measured numbers and the exact fate counts"
    if kept:
        return kept
    raise llm.LLMUnavailable(f"{purpose} invalid after {ATTEMPTS} attempts: {feedback}"[:300])


def judge_scores(facts: dict, reference: dict, journal=None, call=None, samples: int = SAMPLES) -> dict:
    """Agent 2's resilience score: SAMPLES independent judgements (different seeds), median per component."""
    from chaosctl import score_memory
    calibration = score_memory.examples(facts["fault"]["name"], facts["exp_ref"])
    js = []
    for k in range(samples):
        feedback = None
        for _ in range(ATTEMPTS):
            j = llm.generate_json("report_score", score_prompt(facts, reference, calibration, feedback), ScoreJudgement,
                                  journal=journal, call=call, timeout_s=TIMEOUT_S, seed=7 + k,
                                  temperature=SAMPLE_TEMPERATURE)
            if not (errs := validate_judgement(j, facts)):
                js.append(j)
                break
            feedback = "; ".join(errs)
    if not js:
        raise llm.LLMUnavailable(f"report_score invalid after {ATTEMPTS} attempts: {feedback}"[:300])
    out = median_judgement(js)
    out["calibrated_on"] = [e["exp_ref"] for e in calibration]
    return out


def write_report(exps: list[dict], kb, journal=None, call=None) -> dict:
    """AI-written report content. Raises llm.LLMUnavailable if the model is unavailable or stays invalid.
    Each exp needs the report.load_run() keys plus "detectors", "grade" and "score" (the formula reference)."""
    facts = [experiment_facts(e, kb) for e in exps]
    refs = {f["exp_ref"]: (e.get("score") or {}) for f, e in zip(facts, exps)}
    exp_reports, warnings, scores = [], {}, {}
    for f in facts:
        ref = refs[f["exp_ref"]]
        if ref.get("scored"):
            scores[f["exp_ref"]] = {"total": ref["total"], "verdict": ref["band"],
                                    "components": ref["components"]}
    for f in facts:
        allowed = grounded_numbers(f)
        r, bad = _ask("report_experiment", lambda fb, f=f: experiment_prompt(f, fb), ExperimentReport,
                      lambda r, f=f: validate_experiment(r, f),
                      lambda r, a=allowed, f=f: ungrounded(_text(r), a) + false_claims(_text(r), [f]),
                      journal, call)
        if bad:
            warnings[f["exp_ref"]] = bad
        exp_reports.append(r)
    fired = {d["id"] for f in facts for d in f["detectors_fired"]}
    allowed = set().union(*(grounded_numbers(f) for f in facts)) if facts else set()
    allowed |= _numbers([[v["total"]] + [c["score"] for c in v["components"].values()] for v in scores.values()])
    summary, bad = _ask("report_summary", lambda fb: summary_prompt(facts, exp_reports, fired, fb, scores),
                        FindingsSummary,
                        lambda s: validate_summary(s, exp_reports, fired),
                        lambda s: ungrounded(_text(s), allowed) + false_claims(_text(s), facts),
                        journal, call)
    if bad:
        warnings["summary"] = bad
    return {"experiments": exp_reports, "scores": scores, "summary": summary, "unverified_numbers": warnings,
            "facts": facts}
