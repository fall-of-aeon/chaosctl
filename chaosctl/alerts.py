"""Loud, recorded warnings: nothing degrades silently.

Every time a component cannot do its job the normal way (the LLM is not used and a
deterministic fallback answers instead, evidence could not be collected, a safety
thread hit an error), call `warn()`. It
  1. prints a red panel to stderr immediately,
  2. writes a `degraded` event to the journal it is given,
  3. appends the event to state/degraded.jsonl (the dashboard shows these as a banner),
  4. is listed again in the summary printed when the command exits.

`llm_fallback()` is `warn()` for the LLM path. With CHAOSCTL_REQUIRE_LLM=1 (or
`chaosctl --require-llm ...`) it raises LLMRequired instead, so the command fails
rather than continuing on pre-written answers.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# repo-root state/ regardless of the working directory, so the dashboard always finds the alerts
STATE_DIR = Path(os.environ.get("CHAOSCTL_STATE_DIR", Path(__file__).resolve().parents[1] / "state"))
_lock = threading.Lock()
_seen: list[dict] = []   # this process, for the exit summary


class LLMRequired(RuntimeError):
    """The LLM was required (CHAOSCTL_REQUIRE_LLM=1) but could not be used."""


def require_llm() -> bool:
    return os.environ.get("CHAOSCTL_REQUIRE_LLM") == "1"


def _console():
    from rich.console import Console
    return Console(stderr=True, highlight=False)


def warn(component: str, reason: str, journal=None, *, kind: str = "error", detail: dict | None = None) -> dict:
    """kind: 'llm_fallback' | 'error' (harness could not do something) | 'warning'."""
    rec = {"event": "degraded", "kind": kind, "component": component, "reason": str(reason)[:500],
           "ts": time.time(), **({"detail": detail} if detail else {})}
    with _lock:
        _seen.append(rec)
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with (STATE_DIR / "degraded.jsonl").open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError as exc:  # still print; never hide the original problem
            rec["persist_error"] = str(exc)
    if journal is not None:
        try:
            journal.write(rec)
        except Exception as exc:  # noqa: BLE001 - report, do not mask the original warning
            rec["journal_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from rich.panel import Panel
        title = {"llm_fallback": "LLM NOT USED - deterministic fallback", "error": "HARNESS ERROR",
                 "warning": "WARNING"}.get(kind, kind)
        _console().print(Panel(f"[bold]{component}[/bold]: {reason}", title=f"⚠ {title}", border_style="red"))
    except Exception:  # noqa: BLE001 - rich missing/broken: plain stderr
        import sys
        print(f"!!! {kind.upper()} {component}: {reason}", file=sys.stderr)
    return rec


_once: set[str] = set()


def warn_once(key: str, component: str, reason: str, journal=None, **kw) -> dict | None:
    """warn() at most once per key per process (for errors inside 1 s loops)."""
    with _lock:
        if key in _once:
            return None
        _once.add(key)
    return warn(component, reason + " (further identical errors in this command are not repeated)", journal, **kw)


def llm_fallback(component: str, reason: str, journal=None) -> dict:
    rec = warn(component, reason, journal, kind="llm_fallback")
    if require_llm():
        raise LLMRequired(f"{component}: LLM required but not used ({reason})")
    return rec


def seen() -> list[dict]:
    with _lock:
        return list(_seen)


def recent(since_ts: float = 0, limit: int = 50) -> list[dict]:
    p = STATE_DIR / "degraded.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines()[-500:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ts", 0) >= since_ts:
            out.append(e)
    return out[-limit:]


def print_summary() -> None:
    """Called at process exit by the CLI: repeat every degradation so it cannot scroll away unseen."""
    items = seen()
    if not items:
        return
    c = _console()
    c.rule(f"[bold red]⚠ {len(items)} problem(s) in this command - results below are NOT fully normal[/bold red]")
    for e in items:
        c.print(f"[red]•[/red] [{e['kind']}] [bold]{e['component']}[/bold]: {e['reason']}")
    c.rule(style="red")
