"""Score memory: Agent 2's resilience judgements on earlier real experiments, used to calibrate new ones.

This is in-context calibration, not model training: every experiment Agent 2 scores is stored here, and the
most similar earlier experiments (same fault first, then most recent) are shown to it as reference examples
the next time it scores. The more real runs are recorded, the more consistent the scoring becomes.
Stored in state/ (gitignored), one JSON object per line, keyed by exp_ref (a re-score replaces the old entry).
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def path() -> Path:
    from chaosctl import llm
    return Path(llm.STATE_DIR) / "score_memory.jsonl"


def load() -> list[dict]:
    p = path()
    out = []
    for line in (p.read_text().splitlines() if p.exists() else []):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def record(entries: list[dict]) -> None:
    """Add or replace entries (by exp_ref)."""
    new = {e["exp_ref"]: {**e, "ts": time.time()} for e in entries}
    kept = [e for e in load() if e["exp_ref"] not in new]
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e, default=str) + "\n" for e in kept + list(new.values())))


def examples(fault: str, exclude_ref: str, k: int = 4) -> list[dict]:
    """Up to k earlier judgements: the same fault first, then other faults, most recent first."""
    pool = [e for e in load() if e["exp_ref"] != exclude_ref]
    pool.sort(key=lambda e: (e.get("fault") != fault, -e.get("ts", 0)))
    return pool[:k]
