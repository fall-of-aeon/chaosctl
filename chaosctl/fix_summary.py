"""Fix summary: a short, plain-English card for an engineer in a hurry (what broke, why, what to change,
how to verify, how to roll back), written at the end of `chaosctl diagnose`.

Blind like Agent 2: the input is the diagnosis JSON and the playbook entries it chose, never the injected
fault. The LLM only writes the sentences (what_broke, why, one action line per fix). Effort, impact, the
exact change, verify and rollback are always copied from playbook.yaml, and a playbook id the playbook does
not have is rejected, so no fix can come from outside the vetted playbook.
Stored in reports/<run-id>/evidence/fix_summary.json, keyed by experiment id.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from chaosctl import alerts, llm
from chaosctl.agents.diagnosis import PLAYBOOK

MAX_FIXES = 3


class FixLine(BaseModel):
    playbook_id: str
    action: str = Field(description="one plain sentence: what to change, for this incident")


class FixSummaryLLM(BaseModel):
    what_broke: str = Field(description="one sentence")
    why: str = Field(description="one sentence")
    fixes: list[FixLine]


def _playbook_part(pid: str) -> dict:
    pb = PLAYBOOK[pid]
    return {"playbook_id": pid, "title": pb.get("title"), "effort": pb.get("effort"), "impact": pb.get("impact"),
            "change": pb.get("change"), "verify": pb.get("verify"), "rollback": pb.get("rollback")}


def prompt(diag: dict, ids: list[str]) -> str:
    entries = {i: {k: PLAYBOOK[i].get(k) for k in ("title", "change", "effort", "impact")} for i in ids}
    return f"""You write a fix summary for an on-call engineer in a hurry. Plain English, short sentences, no jargon
beyond the component names. You were NOT told what was injected; use only this diagnosis.

DIAGNOSIS:
root cause: {diag.get('root_cause')}
failure chain: {json.dumps(diag.get('failure_chain') or [])}
findings: {json.dumps([f.get('title') for f in diag.get('findings') or []])}

PLAYBOOK FIXES YOU MAY USE (ids; you may only use these, in this order of preference):
{json.dumps(entries, indent=1)}

Return JSON: what_broke (one sentence), why (one sentence), fixes: up to {MAX_FIXES} items, each with playbook_id
(from the list) and action (one sentence: what to change, for this incident)."""


def validate(s: FixSummaryLLM, allowed: list[str]) -> list[str]:
    errs = [f"unknown playbook id {f.playbook_id!r}" for f in s.fixes if f.playbook_id not in PLAYBOOK]
    errs += [f"playbook id {f.playbook_id!r} was not recommended by the diagnosis" for f in s.fixes
             if f.playbook_id in PLAYBOOK and f.playbook_id not in allowed]
    if not s.fixes:
        errs.append("no fixes")
    if not s.what_broke.strip() or not s.why.strip():
        errs.append("empty sentence")
    return errs


def fallback(diag: dict, ids: list[str]) -> dict:
    """Same structure built only from the diagnosis and the playbook (no LLM)."""
    rc = (diag.get("root_cause") or "").strip()
    chain = diag.get("failure_chain") or []
    fixes = [{**_playbook_part(i), "action": PLAYBOOK[i].get("title")} for i in ids[:MAX_FIXES]]
    return {"what_broke": rc.split(". ")[0].rstrip(".") + "." if rc else "Not determined.",
            "why": (chain[0].rstrip(".") + ".") if chain else "See the diagnosis.", "fixes": fixes,
            "source": "deterministic fallback"}


def build(diag: dict, use_llm: bool = True, journal=None, call=None) -> dict:
    ids = [f["playbook_id"] for f in diag.get("fixes") or [] if f.get("playbook_id") in PLAYBOOK]
    if not ids:
        out = fallback(diag, ids)
        out["fallback_reason"] = "the diagnosis recommended no playbook fix"
        return out
    if not use_llm:
        alerts.llm_fallback("Fix summary", "LLM disabled for this command -> playbook-only summary used instead", journal)
        return {**fallback(diag, ids), "fallback_reason": "LLM disabled"}
    meta: dict = {}
    try:
        s = llm.generate_json("fix_summary", prompt(diag, ids), FixSummaryLLM, journal=journal, call=call, meta=meta)
    except llm.LLMUnavailable as exc:
        alerts.llm_fallback("Fix summary", f"{exc} -> playbook-only summary used instead", journal)
        return {**fallback(diag, ids), "fallback_reason": str(exc)}
    errs = validate(s, ids)
    if errs:
        alerts.llm_fallback("Fix summary", f"LLM summary failed validation ({'; '.join(errs)[:200]}) -> playbook-only "
                                           "summary used instead", journal)
        return {**fallback(diag, ids), "fallback_reason": "; ".join(errs)}
    fixes = [{**_playbook_part(f.playbook_id), "action": f.action.strip()} for f in s.fixes[:MAX_FIXES]]
    return {"what_broke": s.what_broke.strip(), "why": s.why.strip(), "fixes": fixes,
            "source": f"llm:{meta.get('source', 'api')}:{meta.get('model', '?')}"}


def write(run_dir: Path, exp_id: str, summary: dict) -> Path:
    p = run_dir / "evidence" / "fix_summary.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    data[exp_id] = summary
    p.write_text(json.dumps(data, indent=2))
    return p
