"""Read-only HTTP API + static page for the dashboard (stdlib only).

GET /api/graph             dependency graph (live from the cluster, else graph.json) + map layout
GET /api/state             campaign state, kill switch, loadgen, phase strip, experiment view, workflows
GET /api/components        per node: derived state, pods, restarts since baseline, own live metric
GET /api/metrics?window=s  1 s points (bounded 15 min buffer)
GET /api/events?since=ts   Kubernetes events + journal events, merged and redacted
GET /api/agent1            Agent 1 decision card + candidates table
GET /api/experiments/<id>  plan, guard verdict, fates, timeline, Agent 2 diagnosis (id "current" = active or last)
GET /api/stream            Server-Sent Events, one message per collector tick (~1 s)
POST /api/chat             unchanged RAG chat for the CLI/README (not used by the page)

Nothing here changes the cluster or the harness: there is no inject or kill endpoint.
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


def make_handler(hub: Hub):
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

        def _json(self, obj, status: int = 200) -> None:
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
                if u.path == "/api/agent1":
                    return self._json(hub.api_agent1())
                if u.path.startswith("/api/experiments/"):
                    return self._json(hub.api_experiment(u.path.rsplit("/", 1)[-1], arg("run", None)))
                if u.path == "/api/stream":
                    return self._stream()
            except ValueError as exc:
                return self._json({"error": f"bad request: {exc}"}, 400)
            except Exception as exc:  # report, never drop the connection
                return self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 500)
            name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (STATIC / name).resolve()
            if f.parent != STATIC.resolve() or not f.exists():
                return self._send(b"not found", "text/plain", 404)
            self._send(f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))

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
            if urlparse(self.path).path != "/api/chat":
                return self._send(b"not found", "text/plain", 404)
            from chaosctl.rag import ask
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                question = str(req.get("question", "")).strip()[:500]
                out = ask(question, req.get("history") or []) if question else {"answer": "Ask a question.", "sources": [], "source": "-"}
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

    srv = Server(("127.0.0.1", port), make_handler(hub))
    mode = f"REPLAY of {replay}" if replay else "live"
    print(f"dashboard ({mode}): http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
