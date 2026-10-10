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

    def heartbeat_rows_since(self, since_ts: float) -> int | None:
        rows = self._query("SELECT count(*) FROM orders WHERE workflow='W3' AND completed_at >= to_timestamp(%s)", (since_ts,))
        return None if rows is None else int(rows[0][0])


class K8sProbe:
    def __init__(self, core=None):
        if core is None:
            from kubernetes import client

            from chaosctl.kube import api_client
            core = client.CoreV1Api(api_client())
        self.core = core

    def pods(self) -> dict:
        """graph node -> {ready, pods:[{name, uid, phase, ready, restarts}]}."""
        out = {}
        for node, sel in WORKLOAD_SELECTORS.items():
            try:
                items = self.core.list_namespaced_pod(NAMESPACE, label_selector=sel, _request_timeout=3).items
            except Exception as exc:
                out[node] = {"ready": None, "err": type(exc).__name__, "pods": []}
                continue
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
        except Exception:
            return []
        out = []
        for e in items:
            t = e.last_timestamp or e.event_time or e.first_timestamp
            ts = t.timestamp() if t else 0
            if ts >= since_ts:
                out.append({"ts": ts, "type": e.type, "reason": e.reason, "object": e.involved_object.name,
                            "message": (e.message or "")[:200]})
        return sorted(out, key=lambda x: x["ts"])

    def logs(self, pod: str, lines: int = 50) -> list[str]:
        try:
            text = self.core.read_namespaced_pod_log(pod, NAMESPACE, tail_lines=lines, _request_timeout=5)
        except Exception:
            return []
        return text.splitlines()


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
