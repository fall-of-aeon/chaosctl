"""Where the dashboard's numbers come from. Two interchangeable sources:

LiveSources    the cluster and the harness files, read-only: loadgen SQLite, Redis and Postgres
               via the Kind NodePorts (no port-forward), the Kubernetes API, Chaos Mesh CR status,
               mock-api GET /admin, run journals under reports/.
ReplaySources  one recorded run under reports/<run-id>/ played back on a virtual clock.

Every method returns data or None/"unreachable"; nothing here raises into the collector.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from bisect import bisect_left
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
REPORTS = ROOT / "reports"
STATE = ROOT / "state"
LOADGEN_DB = STATE / "loadgen.sqlite"
GRAPH_JSON = ROOT / "graph.json"
MOCK_URL = "http://127.0.0.1:18080/admin"   # mock-api NodePort (scripts/kind-config.yaml)


# ------------------------------------------------------------------ files shared by both modes

class JournalCache:
    """Parsed journal.jsonl files, re-read only when the file grows."""

    def __init__(self):
        self._c: dict[Path, tuple[int, list[dict]]] = {}

    def read(self, path: Path) -> list[dict]:
        try:
            size = path.stat().st_size
        except OSError:
            return []
        hit = self._c.get(path)
        if hit and hit[0] == size:
            return hit[1]
        out = []
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        self._c[path] = (size, out)
        return out


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_degraded(path: Path, since: float, until: float) -> list[dict]:
    try:
        lines = path.read_text().splitlines()[-500:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if since <= e.get("ts", 0) <= until + 1:
            out.append(e)
    return out


def find_resilience(reports: Path, only_run: str | None = None) -> dict | None:
    """Score and ranking from the newest `chaosctl report` output (reports/<run>/findings.json or
    reports/*findings.json). only_run: replay looks at the replayed run's own report only."""
    if only_run:
        files = [reports / only_run / "findings.json"]
    else:
        files = list(reports.glob("*findings.json")) + list(reports.glob("r*/findings.json"))
    files = [f for f in files if f.exists()]
    for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True):
        d = read_json(f)
        if d and d.get("ranking") and d.get("overall"):
            return {"path": str(f.relative_to(reports.parent)), "generated": f.stat().st_mtime, "source": d.get("source"),
                    "models": d.get("models") or [], "runs": d.get("runs") or [], "overall": d["overall"],
                    "calibrated_on": d.get("calibrated_on") or [], "ranking": d["ranking"]}
    return None


def run_dirs() -> list[Path]:
    """Experiment runs (r<mmdd-hhmmss>), newest first. Sorted by run id, not file time:
    `chaosctl diagnose` appends to old journals."""
    dirs = [p for p in REPORTS.glob("r*") if (p / "journal.jsonl").exists()]
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def load_catalog() -> dict:
    return yaml.safe_load((ROOT / "chaosctl" / "catalog.yaml").read_text())


def load_stats_at(conn: sqlite3.Connection, t: float) -> dict:
    """Client-side numbers at time t from the loadgen SQLite (same table the harness uses).
    Rates are over the trailing 5 s, p95 over the trailing 10 s."""
    def window(wf: str, start: float, end: float) -> tuple[int, int, float | None]:
        rows = conn.execute("SELECT status, latency_ms FROM requests WHERE workflow=? AND finished_ts>=? AND finished_ts<?",
                            (wf, start, end)).fetchall()
        ok = sorted(r[1] for r in rows if r[0] is not None and 200 <= r[0] < 300 and r[1] is not None)
        p95 = ok[min(len(ok) - 1, int(0.95 * len(ok)))] if ok else None
        return len(rows), sum(1 for r in rows if r[0] is not None and 200 <= r[0] < 300), p95

    sent10 = conn.execute("SELECT count(*) FROM requests WHERE sent_ts>=? AND sent_ts<?", (t - 10, t)).fetchone()[0]
    sent5 = conn.execute("SELECT count(*) FROM requests WHERE sent_ts>=? AND sent_ts<?", (t - 5, t)).fetchone()[0]
    out: dict = {"load_on": sent5 > 0, "rate": round(sent10 / 10, 2)}
    for wf in ("W1", "W2"):
        n, ok, _ = window(wf, t - 5, t)
        _, _, p95 = window(wf, t - 10, t)
        k = wf.lower()
        out[f"{k}_ok"] = round(ok / 5, 2)
        out[f"{k}_err"] = round((n - ok) / 5, 2)
        out[f"{k}_err_rate"] = round((n - ok) / n, 3) if n else None
        out[f"{k}_p95"] = p95
    out["w1_pending"] = conn.execute(
        "SELECT count(*) FROM requests WHERE workflow='W1' AND sent_ts<=? AND sent_ts>? AND (finished_ts IS NULL OR finished_ts>?)",
        (t, t - 90, t)).fetchone()[0]
    return out


def profile_name(rate: float) -> str:
    from chaosctl.loadgen import PROFILES
    for name, r in PROFILES.items():
        if r and abs(rate - r) <= 0.25 * r:
            return name
    return f"{rate:g} req/s"


# ------------------------------------------------------------------ live

class LiveSources:
    mode = "live"

    def __init__(self):
        from chaosctl.loadgen import connect
        from chaosctl.probes import PgProbe, RedisProbe

        self.journals = JournalCache()
        self._db = connect(LOADGEN_DB) if LOADGEN_DB.exists() else None
        self._redis = RedisProbe()
        self._pg = PgProbe()
        self._core = self._custom = self._apps = None
        self.cluster_error: str | None = None

    def now(self) -> float:
        return time.time()

    # -- kubernetes clients (created lazily, re-created after failures)
    def _k8s(self) -> bool:
        if self._core is not None:
            return True
        try:
            from kubernetes import client

            from chaosctl.kube import api_client
            api = api_client()
            self._core, self._custom, self._apps = client.CoreV1Api(api), client.CustomObjectsApi(api), client.AppsV1Api(api)
            self.cluster_error = None
            return True
        except Exception as exc:
            self.cluster_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return False

    def _k8s_failed(self, exc: Exception) -> None:
        self.cluster_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        self._core = None

    # -- samples
    def loadgen(self, t: float) -> dict | None:
        if self._db is None:
            if not LOADGEN_DB.exists():
                return None
            from chaosctl.loadgen import connect
            self._db = connect(LOADGEN_DB)
        try:
            return load_stats_at(self._db, t)
        except sqlite3.Error:
            return None

    def redis(self, t: float) -> dict:
        return self._redis.sample()

    def postgres(self, t: float) -> dict:
        """Reachability plus the reconciliation gap: W2 requests accepted 30 s to 5 min ago
        that still have no row in orders (BUILD_PLAN 6.1)."""
        ids = []
        if self._db is not None:
            ids = [r[0] for r in self._db.execute(
                "SELECT request_id FROM requests WHERE workflow='W2' AND status BETWEEN 200 AND 299 AND sent_ts BETWEEN ? AND ?",
                (t - 300, t - 30)).fetchall()]
        counts = self._pg.row_counts(ids) if ids else ({} if self._pg.sample().get("reachable") else None)
        if counts is None:
            return {"reachable": False}
        return {"reachable": True, "recon_gap": sum(1 for v in counts.values() if v == 0), "recon_checked": len(ids)}

    def mock(self, t: float) -> dict:
        import httpx
        try:
            r = httpx.get(MOCK_URL, timeout=1.0)
            d = r.json()
            return {"reachable": True, "delay_s": d.get("delay_s"), "error_rate": d.get("error_rate")}
        except Exception:
            return {"reachable": False}

    def pods(self, t: float) -> dict | None:
        """graph node -> list of pods, one API call. None if the cluster is unreachable."""
        from chaosctl.probes import WORKLOAD_SELECTORS
        from chaosctl.kube import NAMESPACE

        if not self._k8s():
            return None
        try:
            items = self._core.list_namespaced_pod(NAMESPACE, _request_timeout=3).items
        except Exception as exc:
            self._k8s_failed(exc)
            return None
        out = {}
        for node, sel in WORKLOAD_SELECTORS.items():
            want = dict(kv.split("=") for kv in sel.split(","))
            pods = []
            for p in items:
                if not all((p.metadata.labels or {}).get(k) == v for k, v in want.items()):
                    continue
                cs = p.status.container_statuses or []
                term = [s.last_state.terminated.reason for s in cs if s.last_state and s.last_state.terminated]
                created = p.metadata.creation_timestamp.timestamp() if p.metadata.creation_timestamp else None
                deleting = p.metadata.deletion_timestamp is not None
                pods.append({"name": p.metadata.name, "uid": p.metadata.uid,
                             "phase": "Terminating" if deleting else p.status.phase,
                             "ready": bool(cs) and all(s.ready for s in cs) and not deleting,
                             "restarts": sum(s.restart_count for s in cs), "created": created,
                             "last_termination": term[0] if term else None})
            out[node] = sorted(pods, key=lambda x: x["name"])
        return out

    def events(self, t: float) -> list[dict] | None:
        from chaosctl.kube import NAMESPACE
        if not self._k8s():
            return None
        try:
            items = self._core.list_namespaced_event(NAMESPACE, _request_timeout=3).items
        except Exception as exc:
            self._k8s_failed(exc)
            return None
        out = []
        for e in items:
            ts = e.last_timestamp or e.event_time or e.first_timestamp
            out.append({"ts": ts.timestamp() if ts else 0, "type": e.type, "reason": e.reason,
                        "object": e.involved_object.name, "message": (e.message or "")[:240]})
        return sorted(out, key=lambda x: x["ts"])

    def chaos(self, t: float) -> list[dict] | None:
        """chaosctl-managed Chaos Mesh resources and the pods they were applied to."""
        from chaosctl.chaos.chaosmesh import ChaosMesh
        if not self._k8s():
            return None
        try:
            crs = ChaosMesh(api=self._custom, catalog={"faults": {}, "targets": {}}).list_managed()
        except Exception:
            return None
        out = []
        for cr in crs:
            st = cr.get("status") or {}
            recs = (st.get("experiment") or {}).get("containerRecords") or []
            out.append({"kind": cr["kind"], "name": cr["metadata"]["name"],
                        "run_id": (cr["metadata"].get("labels") or {}).get("chaosctl/run"),
                        "desired": (st.get("experiment") or {}).get("desiredPhase"),
                        "pods": sorted({r.get("id", "").split("/")[-1] for r in recs if r.get("id")}),
                        "injected": any(r.get("phase") == "Injected" or r.get("injectedCount") for r in recs)})
        return out

    def manifests(self) -> list[dict] | None:
        from chaosctl.graph import live_manifests
        from chaosctl.kube import EXPECTED_CONTEXT, NAMESPACE
        if not self._k8s():
            return None
        try:
            return live_manifests(NAMESPACE, EXPECTED_CONTEXT)
        except Exception as exc:
            self._k8s_failed(exc)
            return None

    def platform(self, items: list[dict]) -> dict:
        """Lane status: pod counts for Chaos Mesh / Prometheus / Grafana, annotation on the namespace."""
        from chaosctl.kube import NAMESPACE
        if not self._k8s():
            return {}
        out = {}
        for it in items:
            try:
                if it["id"] == "namespace":
                    ns = self._core.read_namespace(NAMESPACE, _request_timeout=3)
                    ann = (ns.metadata.annotations or {}).get("chaos-mesh.org/inject")
                    out[it["id"]] = {"status": "exists", "detail": "chaos injection enabled" if ann == "enabled" else "not annotated"}
                elif it.get("selector"):
                    pods = self._core.list_namespaced_pod(it["namespace"], label_selector=it["selector"], _request_timeout=3).items
                    ready = sum(1 for p in pods if p.status.container_statuses and all(s.ready for s in p.status.container_statuses))
                    out[it["id"]] = {"status": "running" if ready else "not running",
                                     "detail": f"{ready}/{len(pods)} pods ready" if pods else "0 pods (scaled to 0 or not installed)"}
            except Exception as exc:
                out[it["id"]] = {"status": "unknown", "detail": type(exc).__name__}
        return out

    # -- harness files
    def journal(self, run_dir: Path, t: float) -> list[dict]:
        return self.journals.read(run_dir / "journal.jsonl")

    def runs(self) -> list[Path]:
        return run_dirs()

    def flags(self) -> dict:
        return {"kill_file": (ROOT / "KILL").exists(), "locked": read_json(STATE / "LOCKED.json"),
                "active_run": read_json(STATE / "active_run.json")}

    def degraded(self, t: float) -> list[dict]:
        """LLM fallbacks and harness errors written by chaosctl/alerts.py, last hour."""
        return read_degraded(STATE / "degraded.jsonl", t - 3600, t)


# ------------------------------------------------------------------ replay

class ReplaySources:
    """A recorded run played back in real time on a virtual clock, looping. Only recorded data
    is shown: loadgen SQLite rows, the evidence trace (queue, pod readiness), Kubernetes events
    and the journal. What was not recorded (Postgres gap, mock-api settings, platform) is None."""
    mode = "replay"
    LEAD_S, TAIL_S = 45, 25

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.dir = REPORTS / run_id
        if not (self.dir / "journal.jsonl").exists():
            raise FileNotFoundError(f"no journal for run {run_id} under {REPORTS}")
        self.journals = JournalCache()
        self.cluster_error = None
        ev = self.journals.read(self.dir / "journal.jsonl")
        inj = [e for e in ev if e["event"] == "inject"]
        if not inj:
            raise ValueError(f"run {run_id} never injected; nothing to replay")
        self.t_inject = inj[0].get("t", inj[0]["ts"])
        ends = [e["ts"] for e in ev if e["event"] == "experiment_end"]
        exp_id = next((e.get("plan", {}).get("id") for e in ev if e["event"] == "experiment_start"), "exp-01")
        self.evidence = read_json(self.dir / "evidence" / f"{exp_id}.json") or {}
        self.plan = read_json(self.dir / "evidence" / f"{exp_id}.plan.json") or {}
        self.trace = sorted(self.evidence.get("trace") or [], key=lambda s: s["t"])
        self._tt = [self.t_inject + s["t"] for s in self.trace]
        self.start = self.t_inject - self.LEAD_S
        self.end = (max(ends) if ends else ev[-1]["ts"]) + self.TAIL_S
        self.t0 = time.time()
        self._db = sqlite3.connect(f"file:{LOADGEN_DB}?mode=ro", uri=True, check_same_thread=False) if LOADGEN_DB.exists() else None
        self.k8s_events = sorted(self.evidence.get("events") or [], key=lambda e: e["ts"])

    def now(self) -> float:
        span = self.end - self.start
        return self.start + (time.time() - self.t0) % span

    def loop_index(self) -> int:
        return int((time.time() - self.t0) // (self.end - self.start))

    def _trace_at(self, t: float) -> dict | None:
        i = bisect_left(self._tt, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self._tt) and abs(self._tt[j] - t) <= 1.5:
                if best is None or abs(self._tt[j] - t) < abs(self._tt[best] - t):
                    best = j
        return self.trace[best] if best is not None else None

    def loadgen(self, t: float) -> dict | None:
        if self._db is None:
            return None
        try:
            return load_stats_at(self._db, t)
        except sqlite3.Error:
            return None

    def redis(self, t: float) -> dict:
        s = self._trace_at(t)
        return dict(s["queue"]) if s and s.get("queue") else {}

    def postgres(self, t: float) -> dict:
        return {}

    def mock(self, t: float) -> dict:
        return {}

    def pods(self, t: float) -> dict | None:
        """Pod readiness per node from the trace; pod names for the target from the recorded
        at-inject list and Kubernetes events (killed pod, replacement pod)."""
        s = self._trace_at(t)
        if s is None:
            return None
        target = self.plan.get("target")
        out = {}
        for node, v in (s.get("pods") or {}).items():
            out[node] = [{"name": None, "uid": None, "phase": "Running" if v.get("ready") else "NotReady",
                          "ready": bool(v.get("ready")), "restarts": None, "created": None, "last_termination": None,
                          "reconstructed": True}]
        if target in out:
            out[target] = self._target_pods(t, out[target][0]["ready"])
        return out

    def _target_pods(self, t: float, any_ready: bool) -> list[dict]:
        names = list((self.plan.get("at_inject") or {}).get("target_pods") or [])
        killed = self.killed_pod(t)
        created = [(e["ts"], e["message"].split("Created pod: ")[-1]) for e in self.k8s_events
                   if e["reason"] == "SuccessfulCreate" and e["ts"] <= t and e["ts"] >= self.t_inject - 1]
        ready_rel = (self.evidence.get("timeline") or {}).get("t_pod_ready")
        pods = []
        for n in names:
            gone = n == killed and t >= self.t_inject
            pods.append({"name": n, "uid": n, "phase": "Terminating" if gone else "Running", "ready": not gone and any_ready,
                         "restarts": None, "created": None, "last_termination": None, "reconstructed": True})
        for ts, n in created:
            if n in names:
                continue
            ok = ready_rel is not None and t >= self.t_inject + ready_rel
            pods.append({"name": n, "uid": n, "phase": "Running" if ok else "Pending", "ready": ok, "restarts": None,
                         "created": ts, "last_termination": None, "reconstructed": True})
        return pods

    def killed_pod(self, t: float) -> str | None:
        for e in self.k8s_events:
            if e["ts"] > t + 1:
                break
            m = re.search(r"apply chaos for [\w-]+/([\w.-]+)", e.get("message", ""))
            if m:
                return m.group(1)
        return None

    def events(self, t: float) -> list[dict]:
        return [e for e in self.k8s_events if e["ts"] <= t]

    def chaos(self, t: float) -> list[dict]:
        ev = [e for e in self.journals.read(self.dir / "journal.jsonl") if e["ts"] <= t]
        made = [e for e in ev if e["event"] == "chaos_create"]
        gone = {n for e in ev if e["event"] == "chaos_delete_all" for n in e.get("names", [])}
        out = []
        for e in made:
            if e["name"] in gone:
                continue
            pod = self.killed_pod(t)
            out.append({"kind": (e.get("spec") or {}).get("action", ""), "name": e["name"], "run_id": e.get("run_id"),
                        "desired": "Run", "pods": [pod] if pod else [], "injected": pod is not None})
        return out

    def manifests(self) -> None:
        return None

    def platform(self, items: list[dict]) -> dict:
        return {}

    def journal(self, run_dir: Path, t: float) -> list[dict]:
        return [e for e in self.journals.read(run_dir / "journal.jsonl") if e["ts"] <= t]

    def runs(self) -> list[Path]:
        return [self.dir]

    def flags(self) -> dict:
        return {"kill_file": False, "locked": None, "active_run": None}

    def degraded(self, t: float) -> list[dict]:
        return [e for e in self.journal(self.dir, t) if e.get("event") == "degraded"]
