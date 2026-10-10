"""Live impact dashboard: `chaosctl dashboard` -> http://127.0.0.1:8787

Samples the same black-box probes as the harness once per second (own process, own
connections) and overlays experiment events from the newest run journal.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from chaosctl.loadgen import LoadStats
from chaosctl.probes import K8sProbe, RedisProbe

ROOT = Path(__file__).parents[1]
LOADGEN_DB = ROOT / "state" / "loadgen.sqlite"
HTML = Path(__file__).with_name("dashboard.html")
HISTORY_S = 300
EVENT_KINDS = {"inject", "abort", "kill_switch_exec", "watchdog_restart", "experiment_end", "experiment_start",
               "chaos_delete_all", "safety_armed", "warmup", "state"}


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="dashboard-sampler")
        self.points: deque = deque(maxlen=HISTORY_S)
        self.pods: dict = {}
        self.lock = threading.Lock()
        self.baseline = None

    def run(self) -> None:
        stats, redis, k8s = LoadStats(LOADGEN_DB), RedisProbe(), K8sProbe()
        while True:
            t = time.time()
            try:
                # 5 s rolling rate: 1 s buckets are too bursty to read
                w1_1s, w2_1s = stats.window("W1", t - 5, t), stats.window("W2", t - 5, t)
                w1_10s = stats.window("W1", t - 10, t)
                q = redis.sample()
                pods = k8s.pods()
                pt = {"t": round(t, 2),
                      "w1_ok": round(w1_1s["ok"] / 5, 2), "w1_err": round((w1_1s["n"] - w1_1s["ok"]) / 5, 2),
                      "w2_ok": round(w2_1s["ok"] / 5, 2), "w2_err": round((w2_1s["n"] - w2_1s["ok"]) / 5, 2),
                      "w1_p95": w1_10s["p95_ms"], "w1_pending": len(stats.pending("W1")),
                      "q_wait": q.get("wait") if q.get("reachable") else None,
                      "q_active": q.get("active") if q.get("reachable") else None,
                      "redis_up": bool(q.get("reachable")), "dbsize": q.get("dbsize"),
                      "unready": sorted(n for n, v in pods.items() if not v["ready"])}
                with self.lock:
                    self.points.append(pt)
                    self.pods = {n: {"ready": v["ready"], "restarts": sum(p["restarts"] for p in v["pods"]),
                                     "pods": [p["name"] for p in v["pods"] if not p["deleting"]]} for n, v in pods.items()}
            except Exception:
                pass
            time.sleep(max(0.0, 1.0 - (time.time() - t)))


def latest_run() -> dict:
    runs = sorted((p for p in (ROOT / "reports").glob("r*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    if not runs:
        return {}
    d = runs[-1]
    events = []
    jp = d / "journal.jsonl"
    if jp.exists():
        for line in jp.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") in EVENT_KINDS:
                keep = {k: e[k] for k in ("event", "ts", "reason", "via", "exp_id", "status", "fates",
                                          "recovery_time_s", "in_flight", "nodes", "plan", "seconds", "to") if k in e}
                if "plan" in keep:
                    p = keep.pop("plan")
                    keep.update(fault=p.get("fault"), target=p.get("target"), reason_text=p.get("reason"),
                                hypothesis=p.get("hypothesis"))
                events.append(keep)
    results = []
    rp = d / "results.json"
    if rp.exists():
        try:
            results = json.loads(rp.read_text())
        except ValueError:
            pass
    diag = None
    dfiles = sorted((d / "evidence").glob("*.diagnosis.json"), key=lambda p: p.stat().st_mtime) if (d / "evidence").exists() else []
    if dfiles:
        try:
            diag = json.loads(dfiles[-1].read_text()) | {"exp_id": dfiles[-1].name.split(".")[0]}
        except ValueError:
            pass
    return {"run_id": d.name, "events": events, "diagnosis": diag,
            "results": [{k: r.get(k) for k in ("exp_id", "fault", "target", "status", "abort_reason", "recovery_time_s",
                                               "timeline", "manual_action_needed", "restarts_delta", "plan_reason",
                                               "hypothesis")} | {"fates": (r.get("fates") or {}).get("summary")}
                        for r in results]}


def agent1_choice() -> dict | None:
    """Newest Agent 1 decision (from a run or from `chaosctl plan`): candidates with scores, chosen plan, verdicts."""
    best = None
    for jp in list((ROOT / "reports").glob("r*/journal.jsonl")) + [ROOT / "reports" / "plans" / "journal.jsonl"]:
        if not jp.exists():
            continue
        scored = None
        for line in jp.read_text().splitlines():
            if '"agent1_' not in line:
                continue
            e = json.loads(line)
            if e["event"] == "agent1_scored":
                scored = e["candidates"]
            elif e["event"] == "agent1_plan" and (best is None or e.get("ts", 0) > best["ts"]):
                best = {"ts": e.get("ts", 0), "source": e["source"], "candidates": scored or [],
                        "plan": e["plan"]["experiments"], "verdicts": e["verdicts"]}
    return best


def resilience() -> dict | None:
    """Score and ranking from the newest `chaosctl report` (reports/<run>/findings.json or reports/findings.json)."""
    files = [f for f in (ROOT / "reports").glob("*findings.json")] + list((ROOT / "reports").glob("r*/findings.json"))
    for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("ranking") and d.get("overall"):
            return {"path": str(f.relative_to(ROOT)), "generated": f.stat().st_mtime, "source": d.get("source"),
                    "models": d.get("models") or [], "runs": d.get("runs") or [], "overall": d["overall"],
                    "calibrated_on": d.get("calibrated_on") or [],
                    "ranking": d["ranking"]}
    return None


def system_map() -> dict:
    from chaosctl.graph import Graph
    g = Graph.build([])
    return {n: {"kind": a["kind"], "spof": a["spof"], "criticality": a["criticality"],
                "blast_radius": g.blast_radius(n)["nodes"]} for n, a in g.nodes.items() if a["kind"] != "obs"}


def serve(port: int = 8787) -> None:
    sampler = Sampler()
    sampler.start()
    baseline_path = ROOT / "state" / "baseline.json"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                with sampler.lock:
                    pts, pods = list(sampler.points), dict(sampler.pods)
                base = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
                body = {"now": time.time(), "points": pts, "pods": pods, "run": latest_run(),
                        "agent1": agent1_choice(), "map": system_map(), "resilience": resilience(),
                        "baseline": {"w1_p95": (base or {}).get("w1", {}).get("p95_ms"),
                                     "w1_success": (base or {}).get("w1", {}).get("success_rate"),
                                     "rate": (base or {}).get("rate")} if base else None}
                self._send(json.dumps(body, default=str).encode(), "application/json")
            else:
                self._send(HTML.read_bytes(), "text/html; charset=utf-8")

    def do_POST(self):
        if not self.path.startswith("/api/chat"):
            self.send_response(404); self.end_headers(); return
        from chaosctl.rag import ask
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            q = str(req.get("question", "")).strip()[:500]
            out = ask(q, req.get("history") or []) if q else {"answer": "Ask a question.", "sources": [], "source": "-"}
        except Exception as exc:
            out = {"answer": f"Chat error: {type(exc).__name__}", "sources": [], "source": "error"}
        self._send(json.dumps(out).encode(), "application/json")

    H.do_POST = do_POST
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"dashboard: http://127.0.0.1:{port}  (Ctrl-C to stop)")
    srv.serve_forever()
