"""Black-box ground-truth probes (BUILD_PLAN section 6.1). Never raise: 'unreachable' is data."""
from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import dotenv_values

from chaosctl.kube import NAMESPACE

REDIS_HOST, REDIS_PORT = "127.0.0.1", 16379
PG_HOST, PG_PORT = "127.0.0.1", 15432
BULL_PREFIX = os.environ.get("CHAOSCTL_BULL_PREFIX", "bull")
SECRETS = Path(__file__).parents[1] / "state" / "secrets.env"

# graph node -> pod label selector, for pod readiness/restart probes
WORKLOAD_SELECTORS = {
    "n8n-main": "app.kubernetes.io/instance=n8n,app.kubernetes.io/component=main",
    "n8n-worker": "app.kubernetes.io/instance=n8n,app.kubernetes.io/component=worker",
    "redis": "app=redis",
    "postgres": "app=postgres",
    "mock-api": "app=mock-api",
}


class RedisProbe:
    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT, prefix: str = BULL_PREFIX):
        import redis

        self.prefix = prefix
        self.r = redis.Redis(host=host, port=port, socket_timeout=1.0, socket_connect_timeout=1.0,
                             health_check_interval=0)

    def sample(self) -> dict:
        try:
            p = self.r.pipeline(transaction=False)
            p.llen(f"{self.prefix}:jobs:wait")
            p.llen(f"{self.prefix}:jobs:active")
            p.dbsize()
            wait, active, dbsize = p.execute()
            return {"reachable": True, "wait": wait, "active": active, "dbsize": dbsize}
        except Exception as exc:
            self.r.connection_pool.disconnect()  # force a fresh connection next time
            return {"reachable": False, "err": type(exc).__name__}


class PgProbe:
    """Read-only probe user over NodePort; reconnects on every failure."""

    def __init__(self, host: str = PG_HOST, port: int = PG_PORT):
        pw = os.environ.get("PG_PROBE_PASSWORD") or dotenv_values(SECRETS).get("PG_PROBE_PASSWORD", "")
        self.dsn = f"host={host} port={port} dbname=n8n user=probe password={pw} connect_timeout=2"
        self.conn = None

    def _conn(self):
        import psycopg

        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.dsn, autocommit=True)
        return self.conn

    def _query(self, sql: str, params=()):
        try:
            with self._conn().cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception:
            try:
                if self.conn:
                    self.conn.close()
            finally:
                self.conn = None
            return None

    def row_counts(self, request_ids: list[str]) -> dict[str, int] | None:
        """request_id -> number of rows in orders (0 if absent). None if unreachable."""
        if not request_ids:
            return {}
        rows = self._query("SELECT request_id::text, count(*) FROM orders WHERE request_id = ANY(%s::uuid[]) GROUP BY 1",
                           (request_ids,))
        if rows is None:
            return None
        counts = dict(rows)
        return {rid: int(counts.get(rid, 0)) for rid in request_ids}

    def sample(self) -> dict:
        rows = self._query("SELECT count(*), count(DISTINCT request_id) FROM orders")
        if rows is None:
            return {"reachable": False}
        total, distinct = rows[0]
        return {"reachable": True, "orders": total, "duplicates": total - distinct}



class K8sProbe:
    def __init__(self, core=None):
        if core is None:
            from kubernetes import client

            from chaosctl.kube import api_client
            core = client.CoreV1Api(api_client())
        self.core = core
        self.errors: list[str] = []   # collection failures; the runner copies these into the evidence bundle

    def _err(self, what: str, exc: Exception) -> None:
        self.errors.append(f"{what}: {type(exc).__name__}: {str(exc)[:120]}")

    def pods(self) -> dict:
        """graph node -> {ready, pods:[{name, uid, phase, ready, restarts}]}."""
        out = {}
        try:  # one API call for all workloads (keeps load off the API server)
            all_pods = self.core.list_namespaced_pod(NAMESPACE, _request_timeout=3).items
        except Exception as exc:
            self._err("list pods", exc)
            return {node: {"ready": None, "err": type(exc).__name__, "pods": []} for node in WORKLOAD_SELECTORS}
        for node, sel in WORKLOAD_SELECTORS.items():
            want = dict(kv.split("=") for kv in sel.split(","))
            items = [p for p in all_pods if all((p.metadata.labels or {}).get(k) == v for k, v in want.items())]
            pods = []
            for p in items:
                statuses = p.status.container_statuses or []
                pods.append({
                    "name": p.metadata.name, "uid": p.metadata.uid, "phase": p.status.phase,
                    "deleting": p.metadata.deletion_timestamp is not None,
                    "ready": bool(statuses) and all(s.ready for s in statuses),
                    "restarts": sum(s.restart_count for s in statuses),
                })
            out[node] = {"ready": any(p["ready"] and not p["deleting"] for p in pods), "pods": pods}
        return out

    def events_since(self, since_ts: float) -> list[dict]:
        try:
            items = self.core.list_namespaced_event(NAMESPACE, _request_timeout=3).items
        except Exception as exc:
            self._err("list events", exc)
            return []
        out = []
        for e in items:
            t = e.last_timestamp or e.event_time or e.first_timestamp
            ts = t.timestamp() if t else 0
            if ts >= since_ts:
                out.append({"ts": ts, "type": e.type, "reason": e.reason, "object": e.involved_object.name,
                            "message": (e.message or "")[:200]})
        return sorted(out, key=lambda x: x["ts"])

    def exec(self, pod: str, command: list[str], container: str | None = None) -> str:
        """Run a read-only command in a pod (preflight checks). Raises on failure."""
        from kubernetes.stream import stream
        kw = {"container": container} if container else {}
        return stream(self.core.connect_get_namespaced_pod_exec, pod, NAMESPACE, command=command,
                      stderr=True, stdin=False, stdout=True, tty=False, _request_timeout=10, **kw)

    def logs(self, pod: str, lines: int = 50, since_ts: float | None = None, until_ts: float | None = None,
             previous: bool = False) -> list[str]:
        """Pod log lines; with since_ts/until_ts only lines logged inside that window (by the kubelet's
        timestamp), so evidence never carries errors from before the experiment."""
        from datetime import datetime
        window = since_ts is not None or until_ts is not None
        try:
            kw = ({"since_seconds": max(1, int(time.time() - since_ts) + 5)} if since_ts is not None
                  else {} if window else {"tail_lines": lines})
            text = self.core.read_namespaced_pod_log(pod, NAMESPACE, timestamps=window, previous=previous,
                                                     _request_timeout=15, **kw)
        except Exception as exc:
            if not previous:   # no previous container is normal; anything else is a collection failure
                self._err(f"logs {pod}", exc)
            return []
        if not window:
            return text.splitlines()
        out = []
        for line in text.splitlines():
            stamp, _, msg = line.partition(" ")
            try:  # 2026-10-11T01:30:22.123456789Z -> keep microseconds
                ts = datetime.fromisoformat(stamp.rstrip("Z")[:26] + "+00:00").timestamp()
            except ValueError:
                continue
            if (since_ts is None or ts >= since_ts) and (until_ts is None or ts <= until_ts):
                out.append(msg)
        return out[-lines:]


# ---------------------------------------------------------------- fate table

FATES = ("completed_once", "lost_silently", "duplicated", "phantom_success", "lost_visible", "success_without_row")


def classify_fate(workflow: str, client_ok: bool, rows: int) -> str:
    """Fate of one in-flight request (BUILD_PLAN 11.1). A request that never got a
    response by the end of the fate wait counts as a client error/timeout."""
    if rows > 1:
        return "duplicated"
    if client_ok:
        if rows == 1:
            return "completed_once"
        return "lost_silently" if workflow == "W2" else "success_without_row"
    return "phantom_success" if rows == 1 else "lost_visible"


def fate_table(requests: dict[str, dict], counts: dict[str, int], t_inject: float) -> dict:
    rows = []
    for rid, req in sorted(requests.items()):
        ok = req.get("status") is not None and 200 <= req["status"] < 300
        n = counts.get(rid, 0)
        rows.append({"request_id": rid, "workflow": req["workflow"], "client": "success" if ok else (req.get("client_error") or "no_response"),
                     "rows": n, "fate": classify_fate(req["workflow"], ok, n),
                     "late": bool(ok and req.get("finished_ts") and req["finished_ts"] > t_inject + 30)})
    summary = {f: sum(1 for r in rows if r["fate"] == f) for f in FATES}
    return {"rows": rows, "summary": summary}


def now() -> float:
    return time.time()
