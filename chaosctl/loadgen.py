"""Steady load against n8n production webhooks via NodePort; every request recorded in SQLite.

Runs its own asyncio loop in a background thread so the harness can keep observing.
"""
from __future__ import annotations

import asyncio
import itertools
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

N8N_URL = "http://127.0.0.1:15678"
WEBHOOK_PATHS = {"W1": "/webhook/chaos-w1", "W2": "/webhook/chaos-w2"}
CLIENT_TIMEOUT_S = 60.0
PROFILES = {"off": 0.0, "steady": 2.0, "spike": 6.0}  # 3 req/s oversubscribed one worker (T4 calibration)

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY, workflow TEXT, sent_ts REAL, finished_ts REAL,
  status INTEGER, latency_ms REAL, client_error TEXT
);
CREATE INDEX IF NOT EXISTS requests_sent ON requests (sent_ts);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


class LoadGen:
    def __init__(self, db_path: str | Path, rate: float | None = None, base_url: str = N8N_URL,
                 mix: tuple[str, ...] = ("W1", "W2")):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url
        self.rate = PROFILES["steady"] if rate is None else rate
        self.mix = mix
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write = connect(self.db_path)
        self._wlock = threading.Lock()

    # ---- control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="loadgen", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def set_profile(self, profile: str) -> None:
        self.rate = PROFILES[profile]

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # ---- workers
    async def _main(self) -> None:
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=CLIENT_TIMEOUT_S, limits=limits) as client:
            tasks: set[asyncio.Task] = set()
            wf_cycle = itertools.cycle(self.mix)
            next_t = time.monotonic()
            while not self._stop.is_set():
                if self.rate <= 0:
                    await asyncio.sleep(0.2)
                    next_t = time.monotonic()
                    continue
                t = asyncio.create_task(self._one(client, next(wf_cycle)))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
                next_t += 1.0 / self.rate
                await asyncio.sleep(max(0.0, next_t - time.monotonic()))
            # stop issuing; let in-flight requests finish (bounded by client timeout)
            if tasks:
                await asyncio.wait(tasks, timeout=CLIENT_TIMEOUT_S + 5)

    async def _one(self, client: httpx.AsyncClient, wf: str) -> None:
        rid = str(uuid.uuid4())
        sent = time.time()
        with self._wlock:
            self._write.execute("INSERT INTO requests (request_id, workflow, sent_ts) VALUES (?,?,?)", (rid, wf, sent))
            self._write.commit()
        status, err = None, None
        try:
            r = await client.post(WEBHOOK_PATHS[wf], json={
                "request_id": rid, "workflow": wf,
                "received_at": datetime.fromtimestamp(sent, timezone.utc).isoformat()})
            status = r.status_code
            if status >= 400:
                err = f"http_{status}"
        except httpx.TimeoutException:
            err = "timeout"
        except httpx.HTTPError as exc:
            err = type(exc).__name__
        done = time.time()
        with self._wlock:
            self._write.execute(
                "UPDATE requests SET finished_ts=?, status=?, latency_ms=?, client_error=? WHERE request_id=?",
                (done, status, round((done - sent) * 1000, 1), err, rid))
            self._write.commit()


# ---- read side (any thread)

class LoadStats:
    def __init__(self, db_path: str | Path):
        self.conn = connect(db_path)

    def pending(self, workflow: str) -> list[str]:
        """Requests sent with no response yet."""
        rows = self.conn.execute(
            "SELECT request_id FROM requests WHERE workflow=? AND finished_ts IS NULL", (workflow,)).fetchall()
        return [r[0] for r in rows]

    def accepted_since(self, workflow: str, since_ts: float) -> list[str]:
        rows = self.conn.execute(
            "SELECT request_id FROM requests WHERE workflow=? AND status BETWEEN 200 AND 299 AND sent_ts>=?",
            (workflow, since_ts)).fetchall()
        return [r[0] for r in rows]

    def window(self, workflow: str, start_ts: float, end_ts: float) -> dict:
        """Success rate and latency stats for requests *finished* in [start, end)."""
        rows = self.conn.execute(
            "SELECT status, latency_ms, client_error FROM requests WHERE workflow=? AND finished_ts>=? AND finished_ts<?",
            (workflow, start_ts, end_ts)).fetchall()
        n = len(rows)
        ok = [r for r in rows if r[0] is not None and 200 <= r[0] < 300]
        lat = sorted(r[1] for r in ok if r[1] is not None)
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None
        p50 = lat[len(lat) // 2] if lat else None
        return {"n": n, "ok": len(ok), "success_rate": (len(ok) / n) if n else None,
                "error_rate": (1 - len(ok) / n) if n else None, "p50_ms": p50, "p95_ms": p95}

    def requests(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = f"SELECT request_id, workflow, sent_ts, finished_ts, status, latency_ms, client_error FROM requests WHERE request_id IN ({','.join('?' * len(chunk))})"
            for r in self.conn.execute(q, chunk):
                out[r[0]] = dict(zip(("request_id", "workflow", "sent_ts", "finished_ts", "status", "latency_ms", "client_error"), r))
        return out
