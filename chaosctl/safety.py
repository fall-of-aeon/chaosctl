"""Deterministic safety: state machine, 1 Hz abort monitor, kill switch, recovery watchdog.

No LLM is ever consulted here. The controller runs in its own thread with its own
probes and k8s client so a hung campaign cannot block an abort.
"""
from __future__ import annotations

import enum
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from chaosctl.journal import Journal, NullJournal

STATE_DIR = Path("state")
KILL_FILE = Path("KILL")
ACTIVE_RUN = STATE_DIR / "active_run.json"
LOCK_FILE = STATE_DIR / "LOCKED.json"


class State(str, enum.Enum):
    IDLE = "IDLE"
    PLANNED = "PLANNED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    WAIT_IN_FLIGHT = "WAIT_IN_FLIGHT"
    INJECTING = "INJECTING"
    OBSERVING = "OBSERVING"
    ABORTING = "ABORTING"
    RECOVERY_WAIT = "RECOVERY_WAIT"
    DONE = "DONE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    LOCKED = "LOCKED"


TRANSITIONS = {
    State.IDLE: {State.PLANNED},
    State.PLANNED: {State.PREFLIGHT_OK, State.DONE},
    State.PREFLIGHT_OK: {State.WAIT_IN_FLIGHT, State.DONE},
    State.WAIT_IN_FLIGHT: {State.INJECTING, State.DONE},
    State.INJECTING: {State.OBSERVING},
    State.OBSERVING: {State.RECOVERY_WAIT},
    State.ABORTING: {State.RECOVERY_WAIT},
    State.RECOVERY_WAIT: {State.DONE, State.ROLLBACK_FAILED},
    State.ROLLBACK_FAILED: {State.LOCKED},
    State.DONE: {State.IDLE, State.PLANNED},
    State.LOCKED: set(),
}
ABORTABLE = {State.WAIT_IN_FLIGHT, State.INJECTING, State.OBSERVING}


@dataclass
class Thresholds:
    w1_error_rate: float = 0.5
    w1_p95_ms: float = 20000
    queue_waiting: int = 500
    max_total_s: float = 180
    recovery_deadline_s: float = 120
    error_rate_armed_after_s: float = 0      # seconds after inject before error/latency aborts arm
    error_rate_abort: bool = True            # tier 3: outage by design, error-rate abort off
    window_s: float = 5.0
    allowed_unready: list[str] = field(default_factory=list)  # target + blast radius

    def tightened(self, other: "Thresholds") -> "Thresholds":
        """Combine with defaults: never looser than self (the policy default)."""
        return Thresholds(
            w1_error_rate=min(self.w1_error_rate, other.w1_error_rate),
            w1_p95_ms=min(self.w1_p95_ms, other.w1_p95_ms),
            queue_waiting=min(self.queue_waiting, other.queue_waiting),
            max_total_s=min(self.max_total_s, other.max_total_s),
            recovery_deadline_s=min(self.recovery_deadline_s, other.recovery_deadline_s),
            error_rate_armed_after_s=min(self.error_rate_armed_after_s, other.error_rate_armed_after_s)
            if self.error_rate_abort else other.error_rate_armed_after_s,
            error_rate_abort=self.error_rate_abort or other.error_rate_abort,
            window_s=self.window_s,
            allowed_unready=sorted(set(self.allowed_unready) & set(other.allowed_unready))
            if self.allowed_unready else sorted(other.allowed_unready),
        )


def is_locked() -> dict | None:
    return json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else None


def save_active_run(info: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    ACTIVE_RUN.write_text(json.dumps(info, indent=2, default=str))


def load_active_run() -> dict | None:
    return json.loads(ACTIVE_RUN.read_text()) if ACTIVE_RUN.exists() else None


def clear_active_run() -> None:
    ACTIVE_RUN.unlink(missing_ok=True)


# ------------------------------------------------------------- watchdog

def rollout_restart(apps, node: str, workload: dict) -> None:
    body = {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}}}}
    from chaosctl.kube import NAMESPACE
    if workload["kind"] == "StatefulSet":
        apps.patch_namespaced_stateful_set(workload["name"], NAMESPACE, body)
    else:
        apps.patch_namespaced_deployment(workload["name"], NAMESPACE, body)


def recovery_watchdog(pods_fn: Callable[[], dict], nodes: list[str], deadline_s: float,
                      restart_fn: Callable[[str], None], journal: Journal,
                      restart_grace_s: float = 90, poll_s: float = 1.0) -> dict:
    """Wait until every node is Ready. If the deadline passes, restart the stragglers
    (manual_action_needed) and wait restart_grace_s more. Returns a result dict."""
    t0 = time.monotonic()
    first_ready: dict[str, float] = {}

    def unready() -> list[str]:
        pods = pods_fn()
        bad = []
        for n in nodes:
            if pods.get(n, {}).get("ready"):
                first_ready.setdefault(n, time.time())
            else:
                bad.append(n)
        return bad

    bad = unready()
    while bad and time.monotonic() - t0 < deadline_s:
        time.sleep(poll_s)
        bad = unready()
    if not bad:
        return {"recovered": True, "manual_action_needed": False, "ready_at": first_ready}

    journal.write({"event": "watchdog_restart", "nodes": bad, "after_s": round(time.monotonic() - t0, 1)})
    for n in bad:
        try:
            restart_fn(n)
        except Exception as exc:
            journal.write({"event": "watchdog_restart_error", "node": n, "err": str(exc)[:200]})
    t1 = time.monotonic()
    bad = unready()
    while bad and time.monotonic() - t1 < restart_grace_s:
        time.sleep(poll_s)
        bad = unready()
    return {"recovered": not bad, "manual_action_needed": True, "restarted": True,
            "still_unready": bad, "ready_at": first_ready}


# ------------------------------------------------------------- controller

class SafetyController(threading.Thread):
    """1 Hz abort monitor. Calls on_abort(reason) exactly once on the first breach."""

    def __init__(self, sample_fn: Callable[[], dict], on_abort: Callable[[str], None],
                 journal: Journal | None = None, kill_file: Path = KILL_FILE, tick_s: float = 1.0):
        super().__init__(name="safety", daemon=True)
        self.sample_fn = sample_fn
        self.on_abort = on_abort
        self.journal = journal or NullJournal()
        self.kill_file = kill_file
        self.tick_s = tick_s
        self.state = State.IDLE
        self.thresholds = Thresholds()
        self.t_inject: float | None = None
        self.abort_reason: str | None = None
        self.samples: list[dict] = []
        self.heartbeat = time.monotonic()
        self._kill = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- state machine
    def transition(self, new: State) -> None:
        with self._lock:
            if new not in TRANSITIONS[self.state] and not (new == State.ABORTING and self.state in ABORTABLE):
                raise RuntimeError(f"illegal transition {self.state.value} -> {new.value}")
            self.journal.write({"event": "state", "from": self.state.value, "to": new.value})
            self.state = new
            if new == State.INJECTING:
                self.t_inject = time.time()

    def arm(self, thresholds: Thresholds) -> None:
        self.thresholds = thresholds
        self.journal.write({"event": "safety_armed", "thresholds": asdict(thresholds)})

    # -- kill switch entry points
    def kill(self, why: str) -> None:
        self.journal.write({"event": "kill_switch", "via": why})
        self._kill.set()

    def healthy(self) -> bool:
        return self.is_alive() and time.monotonic() - self.heartbeat < 5 * self.tick_s

    def stop(self) -> None:
        self._stop.set()

    @property
    def aborted(self) -> bool:
        return self.abort_reason is not None

    # -- loop
    def run(self) -> None:
        while not self._stop.is_set():
            self.heartbeat = time.monotonic()
            try:
                self._tick()
            except Exception as exc:  # the monitor itself must not die
                self.journal.write({"event": "safety_tick_error", "err": f"{type(exc).__name__}: {exc}"[:200]})
            self._stop.wait(self.tick_s)

    def _tick(self) -> None:
        if self.kill_file.exists():
            self._kill.set()
        if self._kill.is_set():
            self._abort("kill_switch")
            return
        if self.state not in ABORTABLE or self.aborted:
            return
        s = self.sample_fn()
        s["ts"] = time.time()
        s["state"] = self.state.value
        self.samples.append(s)
        reason = self.check(s)
        if reason:
            self._abort(reason)

    def check(self, s: dict) -> str | None:
        th = self.thresholds
        since = (s["ts"] - self.t_inject) if self.t_inject else None
        if since is not None and since > th.max_total_s:
            return f"max_total_s exceeded ({since:.0f}s > {th.max_total_s:.0f}s)"
        q = s.get("queue") or {}
        if q.get("reachable") and q.get("wait", 0) > th.queue_waiting:
            return f"queue_waiting {q['wait']} > {th.queue_waiting}"
        for node, info in (s.get("pods") or {}).items():
            if info.get("ready") is False and node not in th.allowed_unready:
                return f"blast_radius_breach: {node} unready (outside {th.allowed_unready})"
        armed = since is None or since >= th.error_rate_armed_after_s
        w1 = s.get("w1") or {}
        if th.error_rate_abort and armed and w1.get("n", 0) >= 3:
            if (w1.get("error_rate") or 0) > th.w1_error_rate:
                return f"w1_error_rate {w1['error_rate']:.2f} > {th.w1_error_rate}"
        if armed and (w1.get("p95_ms") or 0) > th.w1_p95_ms:
            return f"w1_p95_ms {w1['p95_ms']:.0f} > {th.w1_p95_ms:.0f}"
        return None

    def _abort(self, reason: str) -> None:
        with self._lock:
            if self.aborted:
                return
            self.abort_reason = reason
            prev = self.state
            if self.state in ABORTABLE:
                self.state = State.ABORTING
        self.journal.write({"event": "abort", "reason": reason, "from_state": prev.value})
        try:
            self.on_abort(reason)
        except Exception as exc:
            self.journal.write({"event": "abort_handler_error", "err": str(exc)[:200]})
