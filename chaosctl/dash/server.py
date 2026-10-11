"""Read-only HTTP API + static page for the dashboard (stdlib only).

GET /api/graph             dependency graph (live from the cluster, else graph.json) + map layout
GET /api/state             campaign state, kill switch, loadgen, phase strip, experiment view, workflows
GET /api/components        per node: derived state, pods, restarts since baseline, own live metric
GET /api/metrics?window=s  1 s points (bounded 15 min buffer)
GET /api/events?since=ts   Kubernetes events + journal events, merged and redacted
GET /api/agent1            Agent 1 decision card + candidates table
GET /api/runs | /api/run/<id>/{summary,series,logs,fates,agent1,diagnosis,fix-summary,report}?exp=
GET /api/stretch-goals | /api/compare?a=&b= | /api/report?run=
GET /api/resilience        newest `chaosctl report` score and ranking (findings.json)
GET /api/experiments/<id>  plan, guard verdict, fates, timeline, Agent 2 diagnosis (id "current" = active or last)
GET /api/stream            Server-Sent Events, one message per collector tick (~1 s)
POST /api/chat             "Ask Agent 2" RAG chat {question, history?, run?} (chat panel; run pins that run's records)

Agents (live mode only; chaosctl/dash/control.py and watcher.py):
GET  /api/control                       Agent 1 proposal/run, kill switch, traffic, Agent 2 watcher state
GET  /api/agent2/incident/<id>          one Agent 2 investigation with every tool call
POST /api/agent1/plan {exclude?}        Agent 1 plans; nothing is injected
POST /api/agent1/approve {proposal_id}  a human approves: the plan runs in its own `chaosctl execute` process
POST /api/agent1/cancel                 discard the proposal
POST /api/kill | /api/kill/reset        kill switch (same as `chaosctl kill`) / clear it (same as `chaosctl recover`)
POST /api/traffic {on}                  steady load from the dashboard, so Agent 2 sees client signals between runs
POST /api/agent2/investigate            ask Agent 2 to investigate now
POST /api/agent2/pause {paused}         stop / resume the automatic trigger

The page never injects anything directly: an approved plan goes through the same campaign graph, runtime guard,
preflight and safety controller as the CLI. Every POST except /api/chat needs the header X-Chaosctl: 1, which a
form or link on another web page cannot send (the server is on 127.0.0.1 only).
"""
from __future__ import annotations

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from chaosctl.dash.hub import Hub
from chaosctl.dash.sources import LiveSources, ReplaySources

STATIC = Path(__file__).with_name("static")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


_cache: dict = {}


def _cached(key: tuple, fn):
    """Finished runs do not change; cache per (endpoint, run, exp, journal size, evidence mtime)."""
    if key not in _cache:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = fn()
    return _cache[key]


def run_api(path: str, arg) -> tuple[dict, int]:
    """Finished-run endpoints (chaosctl/dash/runs.py). Return (body, status)."""
    from chaosctl.dash import runs
    if path == "/api/runs":
        return {"runs": runs.list_runs()}, 200
    if path == "/api/stretch-goals":
        return runs.stretch_goals(), 200
    if path == "/api/report":
        return runs.report(run_id=arg("run", None)), 200
    if path == "/api/compare":
        a, b = arg("a", None), arg("b", None)
        if not a or not b:
            pair = (runs.stretch_goals()["goals"][0].get("pair") or {})
            a, b = a or pair.get("a"), b or pair.get("b")
        if not a or not b:
            return {"error": "give ?a=<run>[/<exp>]&b=<run>[/<exp>]"}, 400
        return _cached(("compare", a, b), lambda: runs.compare(a, b)), 200
    parts = path.split("/")          # /api/run/<id>/<what>
    if len(parts) != 5:
        return {"error": "use /api/run/<run-id>/<summary|series|logs|fates|agent1|diagnosis|fix-summary|report>"}, 404
    run_id, what, exp = parts[3], parts[4], arg("exp", None)
    try:
        r = runs.Run(run_id, exp)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}, 404
    stamp = ((r.dir / "journal.jsonl").stat().st_size, max((f.stat().st_mtime for f in (r.dir / "evidence").glob("*")), default=0)
             if (r.dir / "evidence").exists() else 0)
    if what == "summary":
        s = _cached(("summary", run_id, r.exp_id, stamp), r.summary)
        rep = runs.report(run_id=run_id)
        return {**s, "narration": {**s["narration"], **({"report": model_narr_report(rep)} if rep.get("available") else {})},
                "fix_summary": r.fix_summary, "report_available": rep.get("available")}, 200
    if what == "series":
        return _cached(("series", run_id, r.exp_id, stamp), r.series), 200
    if what == "logs":
        rows = _cached(("logs", run_id, r.exp_id, stamp), r.logs)
        num = lambda k: float(arg(k, "")) if arg(k, "") not in ("", None) else None  # noqa: E731
        return runs.filter_logs(rows, arg("severity", None), arg("source", None), arg("component", None), num("from"), num("to"),
                                arg("q", None), arg("evidence", None), int(arg("page", "0")), min(500, int(arg("page_size", "200")))), 200
    if what == "fates":
        return _cached(("summary", run_id, r.exp_id, stamp), r.summary)["fates"], 200
    if what == "agent1":
        return runs.agent1(r), 200
    if what == "diagnosis":
        return runs.diagnosis(r), 200
    if what == "fix-summary":
        return ({"available": True, **r.fix_summary} if r.fix_summary else
                {"available": False, "command": f"uv run chaosctl fix-summary --run {run_id} --exp {r.exp_id}"}), 200
    if what == "report":
        return runs.report(run_id=run_id), 200
    return {"error": f"unknown endpoint {what!r}"}, 404


def model_narr_report(rep: dict) -> str:
    return f"Resilience score {rep['overall']['score']} / 100: {rep['overall']['verdict']}."


def llm_settings() -> dict:
    """Which LLM settings this process sees (names and model ids only, never the key). Agent 1 once ran with
    MISTRAL_MODEL unset while Agent 2 had it: this line shows which process saw what."""
    import os

    from chaosctl import llm as _llm  # noqa: F401  (loads .env exactly like the agents do)
    env = Path(__file__).resolve().parents[2] / ".env"
    return {"env_file": str(env) if env.exists() else "missing", "MISTRAL_API_KEY": "set" if os.environ.get("MISTRAL_API_KEY") else "missing",
            "MISTRAL_MODEL": os.environ.get("MISTRAL_MODEL") or "missing",
            "MISTRAL_FALLBACK_MODEL": os.environ.get("MISTRAL_FALLBACK_MODEL") or "missing",
            "CHAOSCTL_REQUIRE_LLM": os.environ.get("CHAOSCTL_REQUIRE_LLM") or "0"}


def control_api(path: str, body: dict, control, watcher) -> tuple[dict, int]:
    from chaosctl.dash.control import ControlError
    if control is None or watcher is None:
        return {"error": "controls are only available in live mode"}, 409
    try:
        if path == "/api/agent1/plan":
            return control.plan([str(x) for x in body.get("exclude") or []]), 200
        if path == "/api/agent1/approve":
            return control.approve(str(body.get("proposal_id", ""))), 200
        if path == "/api/agent1/cancel":
            return control.cancel(), 200
        if path == "/api/kill":
            return control.kill(), 200
        if path == "/api/kill/reset":
            return control.reset_kill(), 200
        if path == "/api/traffic":
            return control.traffic(bool(body.get("on"))), 200
        if path == "/api/agent2/investigate":
            rec = watcher.manual()
            return ({"started": rec["id"]} if rec else {"error": "Agent 2 is already investigating"}), (200 if rec else 409)
        if path == "/api/agent2/pause":
            watcher.enabled = not bool(body.get("paused"))
            return watcher.view(), 200
    except ControlError as exc:
        return {"error": str(exc)}, 409
    return {"error": "not found"}, 404


def make_handler(hub: Hub, control=None, watcher=None):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:  # noqa: D401
            self._send(json.dumps(obj, default=str).encode(), "application/json", status)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            arg = lambda k, d: q.get(k, [d])[0]  # noqa: E731
            try:
                if u.path == "/api/graph":
                    return self._json(hub.api_graph())
                if u.path == "/api/state":
                    return self._json(hub.api_state())
                if u.path == "/api/components":
                    return self._json(hub.api_components())
                if u.path == "/api/metrics":
                    return self._json(hub.api_metrics(min(900.0, max(10.0, float(arg("window", "300"))))))
                if u.path == "/api/events":
                    return self._json(hub.api_events(float(arg("since", "0"))))
                if u.path.startswith("/api/run") or u.path in ("/api/stretch-goals", "/api/compare", "/api/report"):
                    return self._json(*run_api(u.path, arg))
                if u.path == "/api/control":
                    return self._json({"control": control.view() if control else {"available": False},
                                       "agent2": watcher.view() if watcher else {"available": False}})
                if u.path.startswith("/api/agent2/incident/"):
                    rec = watcher.incident(u.path.rsplit("/", 1)[-1]) if watcher else None
                    return self._json(rec or {"error": "no such incident"}, 200 if rec else 404)
                if u.path == "/api/resilience":
                    return self._json(hub.api_resilience())
                if u.path == "/api/agent1":
                    return self._json(hub.api_agent1())
                if u.path.startswith("/api/experiments/"):
                    return self._json(hub.api_experiment(u.path.rsplit("/", 1)[-1], arg("run", None)))
                if u.path == "/api/download":
                    return self._download(arg("path", ""))
                if u.path == "/api/stream":
                    return self._stream()
            except ValueError as exc:
                return self._json({"error": f"bad request: {exc}"}, 400)
            except Exception as exc:  # report, never drop the connection
                return self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 500)
            name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (STATIC / name).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(b"not found", "text/plain", 404)
            self._send(f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))

        def _download(self, rel: str):
            """findings.md / findings.json under reports/ only, as a file download."""
            from chaosctl.dash.sources import REPORTS
            f = (REPORTS.parent / rel).resolve()
            if f.name not in ("findings.md", "findings.json") or REPORTS.resolve() not in f.parents or not f.exists():
                return self._json({"error": "only reports/**/findings.md or findings.json can be downloaded"}, 404)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8" if f.suffix == ".md" else "application/json")
            self.send_header("Content-Disposition", f'attachment; filename="{f.parent.name}-{f.name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            sub = hub.subscribe()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while True:
                    try:
                        payload = sub.get(timeout=5)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")  # collector stalled; the page shows Stale
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(sub)

        def do_POST(self):
            path = urlparse(self.path).path
            if path != "/api/chat":
                if self.headers.get("X-Chaosctl") != "1":
                    return self._json({"error": "missing X-Chaosctl header"}, 403)
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                except ValueError:
                    return self._json({"error": "body must be JSON"}, 400)
                return self._json(*control_api(path, body if isinstance(body, dict) else {}, control, watcher))
            from chaosctl.rag import ask
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                question = str(req.get("question", "")).strip()[:500]
                history = [h for h in (req.get("history") or [])[-6:] if isinstance(h, dict)]
                run = req.get("run") if isinstance(req.get("run"), str) else None
                out = ask(question, history, run) if question else {"answer": "Ask a question.", "sources": [], "source": "-"}
            except Exception as exc:
                out = {"answer": f"Chat error: {type(exc).__name__}", "sources": [], "source": "error"}
            self._json(out)

    return H


def serve(port: int = 8787, replay: str | None = None) -> None:
    src = ReplaySources(replay) if replay else LiveSources()
    hub = Hub(src)
    hub.start()
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):  # browsers closing streams is normal
            import sys
            if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
                super().handle_error(request, client_address)

    control = watcher = None
    if not replay:
        from chaosctl.dash.control import Control
        from chaosctl.dash.watcher import Agent2Watcher
        control, watcher = Control(hub), Agent2Watcher(hub)
        watcher.start()
        hub.extras = lambda: {"control": control.view(), "agent2": watcher.view()}
    srv = Server(("127.0.0.1", port), make_handler(hub, control, watcher))
    settings = llm_settings()
    print("dashboard LLM settings: " + ", ".join(f"{k}={v}" for k, v in settings.items()), flush=True)
    hub.llm_settings = settings
    mode = f"REPLAY of {replay}" if replay else "live"
    print(f"dashboard ({mode}): http://127.0.0.1:{port}  (Ctrl-C to stop)"
          + ("" if replay else "\nAgent 2 is watching; Agent 1 waits for the button on the page."))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
