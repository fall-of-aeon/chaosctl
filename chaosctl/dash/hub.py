"""The collector: samples a source once per second and builds every API response.

All browsers share this one thread (no per-browser cluster polling). Buffers are bounded:
15 minutes of 1 s points and the newest 600 events.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from pathlib import Path

from chaosctl.dash import model
from chaosctl.dash.sources import GRAPH_JSON, REPORTS, ROOT, STATE, find_resilience, load_catalog, profile_name, read_json

HISTORY_POINTS = 900          # 15 min at 1 Hz
EVENT_LIMIT = 600
GRAPH_REFRESH_S = 30
STALE_AFTER_S = 5

STATE_TO_PHASE = {"PLANNED": "preflight", "PREFLIGHT_OK": "warm-up", "WAIT_IN_FLIGHT": "waiting for in-flight work",
                  "INJECTING": "injected", "OBSERVING": "observing", "ABORTING": "auto-stopping",
                  "RECOVERY_WAIT": "recovering, then classifying request fates", "ROLLBACK_FAILED": "rollback failed", "LOCKED": "locked"}
SAFETY_EVENTS = {"abort", "kill_switch", "kill_switch_exec", "kill_switch_done", "safety_armed", "watchdog_restart",
                 "watchdog_restart_error", "abort_handler_error", "safety_tick_error", "chaos_force_cleanup"}


def _fault_label(fault: str | None) -> str:
    return (fault or "").replace("_", " ")


def journal_message(e: dict) -> str | None:
    """One readable line per journal entry (None = not shown in the event log)."""
    ev = e.get("event")
    if ev == "state":
        return f"state {e.get('from')} -> {e.get('to')}"
    if ev == "experiment_start":
        p = e.get("plan") or {}
        return f"{p.get('id')} start: {_fault_label(p.get('fault'))} on {p.get('target')} ({p.get('rung')})"
    if ev == "preflight":
        return "preflight ok" if not e.get("problems") else "preflight refused: " + "; ".join(e["problems"])
    if ev == "warmup":
        return f"warm-up: {e.get('seconds')} s of steady load before injection"
    if ev == "safety_armed":
        th = e.get("thresholds") or {}
        return (f"safety armed: stop if W1 errors > {th.get('w1_error_rate')}, p95 > {th.get('w1_p95_ms')} ms, "
                f"queue > {th.get('queue_waiting')}, or after {th.get('max_total_s')} s")
    if ev == "chaos_create":
        return f"Chaos Mesh resource created: {e.get('name')}"
    if ev == "inject":
        f = e.get("in_flight") or {}
        return f"fault injected with {sum(f.values())} requests in flight (W1 {f.get('W1')}, W2 {f.get('W2')})"
    if ev == "abort":
        return f"auto-stop: {e.get('reason')}"
    if ev in ("kill_switch", "kill_switch_exec"):
        return f"kill switch triggered via {e.get('via') or e.get('reason')}"
    if ev == "kill_switch_done":
        return f"kill switch done: deleted {e.get('deleted')}, all Ready after {e.get('all_ready_s')} s"
    if ev == "watchdog_restart":
        return f"watchdog restarted {', '.join(e.get('nodes') or [])} after {e.get('after_s')} s"
    if ev == "chaos_delete_all":
        return f"chaos resources deleted: {', '.join(e.get('names') or []) or 'none'}"
    if ev == "experiment_end":
        rt = e.get("recovery_time_s")
        return f"{e.get('exp_id')} end: {e.get('status')}" + (f", SLIs recovered after {rt} s" if rt is not None else "")
    if ev == "agent1_plan":
        exps = (e.get("plan") or {}).get("experiments") or []
        first = exps[0] if exps else {}
        return f"Agent 1 plan ({e.get('source')}): {len(exps)} experiments, first {_fault_label(first.get('fault'))} on {first.get('target')}"
    if ev == "llm":
        what = e.get("source") or e.get("result")
        return f"LLM {e.get('purpose')}: {e.get('model') or '-'} -> {what}" + (f" ({e['err'][:80]})" if e.get("err") else "")
    if ev in ("guard_note", "runtime_guard"):
        return f"guard: {e.get('note') or (e.get('verdict') or {}).get('rationale')}"
    if ev == "graph_loaded":
        return f"dependency graph loaded: {e.get('nodes')} nodes, {e.get('edges')} edges"
    if ev in ("chaos_force_cleanup", "abort_handler_error", "safety_tick_error", "watchdog_restart_error"):
        return f"{ev}: {e.get('name') or e.get('err')}"
    return None


class Hub(threading.Thread):
    def __init__(self, src, hints: dict | None = None):
        super().__init__(daemon=True, name="dashboard-hub")
        self.src = src
        self.hints = hints if hints is not None else model.load_hints()
        self.catalog = load_catalog()
        self.lock = threading.Lock()
        self.points: deque = deque(maxlen=HISTORY_POINTS)
        self.events: list[dict] = []
        self.subscribers: list[queue.Queue] = []
        self.versions = {"graph": 0, "agent1": "", "experiment": "", "resilience": ""}
        self.graph: dict | None = None
        self.graph_source = "missing"
        self.graph_built = None
        self.layout: dict | None = None
        self.concurrency: dict[str, int] = {}
        self._slow: dict[str, float] = {}
        self._cache: dict[str, object] = {}
        self.last_bad: dict[str, float] = {}
        self.restart_base: dict[str, int] = {}
        self.restart_last: dict[str, tuple[int, float]] = {}
        self.base_key = None
        self.keys_hist: deque = deque(maxlen=60)
        self.tracks: dict[tuple, dict] = {}
        self.prev_pods: dict | None = None
        self.loop_seen = 0
        self.state: dict = {"mode": src.mode, "collected_at": None}
        self.components: dict = {"nodes": {}, "platform": {}}
        self.started = time.time()

    # ---------------------------------------------------------------- loop
    def run(self) -> None:
        while True:
            t_wall = time.time()
            try:
                self.tick()
            except Exception as exc:  # the collector must not die; the page and the terminal show the error
                msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                with self.lock:
                    self.state = {**self.state, "collector_error": msg, "alerts": model.alert_banner(
                        [{"kind": "error", "component": "dashboard collector", "reason": msg, "ts": time.time()}])}
                try:
                    from chaosctl import alerts
                    alerts.warn_once(f"dash:{type(exc).__name__}", "dashboard collector", msg)
                except Exception:
                    pass
            time.sleep(max(0.05, 1.0 - (time.time() - t_wall)))

    def _every(self, key: str, period: float, t: float, fn):
        if key not in self._cache or t - self._slow.get(key, -1e18) >= period or t < self._slow.get(key, 0):
            self._cache[key] = fn()
            self._slow[key] = t
        return self._cache[key]

    def _reset_for_loop(self) -> None:
        """Replay wrapped around: forget everything derived from the previous pass."""
        self.points.clear()
        self.tracks.clear()
        self.last_bad.clear()
        self.keys_hist.clear()
        self._slow.clear()

    def tick(self) -> None:
        src = self.src
        if src.mode == "replay" and src.loop_index() != self.loop_seen:
            self.loop_seen = src.loop_index()
            self._reset_for_loop()
        t = src.now()
        self._refresh_graph(t)
        lg = src.loadgen(t)
        rd = src.redis(t)
        pg = self._every("pg", 2, t, lambda: src.postgres(t))
        mk = self._every("mock", 5, t, lambda: src.mock(t))
        pods = src.pods(t)
        k8s_events = self._every("events", 3, t, lambda: src.events(t))
        platform = self._every("platform", 15, t, lambda: src.platform((self.layout or {}).get("lane", {}).get("items", [])))
        flags = src.flags()


        ctx = self._run_context(t, flags)
        exp = ctx.get("exp")
        chaos = self._every("chaos", 2, t, lambda: src.chaos(t)) if exp and exp.get("active") else []
        self._restart_baseline(ctx, pods)

        graph = self.graph or {"nodes": [], "edges": [], "workflows": []}
        gnodes = {n["name"]: n for n in graph["nodes"]}
        pod_view = self._pod_view(pods, gnodes, t)
        sample = {"load_on": bool(lg and lg["load_on"]), "w1_err_rate": (lg or {}).get("w1_err_rate"),
                  "w2_err_rate": (lg or {}).get("w2_err_rate"), "w1_p95": (lg or {}).get("w1_p95"),
                  "w2_p95": (lg or {}).get("w2_p95"), "redis": rd, "postgres": pg, "mock": mk,
                  "pods": {n: {"ready_count": v["ready_count"]} for n, v in pod_view.items()}}
        # last Redis key count seen before the previous COLLAPSE_S seconds (the outage gap is skipped)
        before = next((k for ts, k in reversed(self.keys_hist) if t - ts >= model.COLLAPSE_S), None)
        recent = {"restarted": {n: v["restarted_recently"] for n, v in pod_view.items()}, "keys_before": before}
        sigs = model.node_signals(graph, sample, ctx.get("baseline"), recent)
        if rd.get("reachable"):
            self.keys_hist.append((t, rd["dbsize"]))
        states = {}
        for name, sig in sigs.items():
            st, why = model.node_state(sig, self.last_bad.get(name), t)
            if st in ("Down", "Degraded"):
                self.last_bad[name] = t
            states[name] = {"state": st, "why": why}

        if exp:
            self._track(exp, states, pod_view, chaos, t)
            exp = self._exp_view(exp, ctx, graph, t)
        last = ctx.get("last")
        if last and (not exp or (last["run_id"], last["id"]) != (exp["run_id"], exp["id"])):
            last = self._exp_view(last, ctx, graph, t)
        elif last:
            last = None

        point = {"t": round(t, 2), "load_on": sample["load_on"]}
        for k in ("w1_ok", "w1_err", "w1_p95", "w2_ok", "w2_err"):
            point[k] = lg.get(k) if lg and lg["load_on"] else None
        point["w1_pending"] = (lg or {}).get("w1_pending") if lg and lg["load_on"] else None
        point["redis_up"] = rd.get("reachable") if rd else None
        point["q_wait"] = rd.get("wait") if rd.get("reachable") else None
        point["q_active"] = rd.get("active") if rd.get("reachable") else None
        point["dbsize"] = rd.get("dbsize") if rd.get("reachable") else None
        point["recon_gap"] = (pg or {}).get("recon_gap")

        active_target = exp["target"] if exp and exp.get("active") and exp.get("t_inject") else None
        wf = model.workflow_strip(graph, {n: s["state"] for n, s in states.items()}, sample, ctx.get("baseline"), active_target)

        comps = self._components(graph, gnodes, pod_view, states, sample, lg, exp, platform, ctx.get("history") or [])
        events = self._merge_events(k8s_events, ctx)
        state = self._state(t, ctx, exp, last, lg, rd, pg, pods, wf, flags, platform)
        with self.lock:
            self.points.append(point)
            self.events = events
            self.state, self.components = state, comps
            self.prev_pods = pods
            self.versions["experiment"] = f"{(exp or last or {}).get('run_id')}:{(exp or last or {}).get('id')}:{ctx.get('exp_files')}"
            self.versions["agent1"] = ctx.get("agent1_key", "")
            payload = json.dumps({"state": state, "components": comps, "point": point, "versions": dict(self.versions),
                                  "events_tail": events[-30:]}, default=str)
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    # ---------------------------------------------------------------- graph
    def _refresh_graph(self, t: float) -> None:
        if self.graph is not None and (self.src.mode == "replay" or time.time() - (self.graph_built or 0) < GRAPH_REFRESH_S):
            return
        from chaosctl.graph import Graph
        g, source, docs = None, "missing", self.src.manifests()
        if docs:
            try:
                g, source = Graph.build(docs).to_dict(), "live"
            except Exception:
                g = None
            for d in docs:  # worker concurrency is a container arg, e.g. ["worker", "--concurrency=10"]
                for c in ((d.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers", []) or []:
                    for a in c.get("args") or []:
                        if str(a).startswith("--concurrency="):
                            self.concurrency[d["metadata"]["name"]] = int(str(a).split("=")[1])
        if g is None and GRAPH_JSON.exists():
            g, source = read_json(GRAPH_JSON), "file"
        self.graph_built = time.time()
        if g and g != self.graph:
            self.graph = g
            self.layout = model.layout(g, self.hints)
            self.versions["graph"] += 1
        self.graph_source = source if g else "missing"

    # ---------------------------------------------------------------- run context
    def _run_context(self, t: float, flags: dict) -> dict:
        """Current run, active experiment and last finished experiment, from the journals."""
        src = self.src
        runs = src.runs()
        ctx: dict = {"baseline": read_json(STATE / "baseline.json"), "history": read_json(STATE / "history.json") or [],
                     "run_id": None, "events": [], "exp": None, "last": None}
        if src.mode == "replay":
            ctx["baseline"] = (src.evidence or {}).get("baseline") or ctx["baseline"]
        if not runs:
            return ctx
        # The current run is the newest one with an experiment in progress (another run may have
        # started later and been skipped), else the newest run.
        cur, ev = runs[0], src.journal(runs[0], t)
        for d in runs[:5]:
            dev = src.journal(d, t)
            starts = [e for e in dev if e["event"] == "experiment_start"]
            if not starts:
                continue
            ended = next((e for e in dev if e["event"] == "experiment_end" and e["ts"] >= starts[-1]["ts"]), None)
            x = self._exp_base(d, dev, starts[-1], ended)
            if x["active"]:
                cur, ev, ctx["exp"] = d, dev, x
                break
        ctx.update(run_id=cur.name, run_dir=cur, events=ev)
        for d in runs:
            dev = ev if d == cur else src.journal(d, t)
            ends = [e for e in dev if e["event"] == "experiment_end"]
            if ends:
                end = ends[-1]
                s = [e for e in dev if e["event"] == "experiment_start" and e["ts"] <= end["ts"]][-1]
                ctx["last"] = self._exp_base(d, dev, s, end)
                break
        if ctx["exp"] and not ctx["exp"]["active"]:
            ctx["exp"] = None  # finished: shown as "last"
        ev_files = []
        for x in (ctx["exp"], ctx["last"]):
            if x:
                ev_files += [p.stat().st_size for p in (x["dir"] / "evidence").glob(f"{x['id']}*.json")
                             if p.stat().st_mtime <= t + 1 or src.mode == "live"]
        ctx["exp_files"] = sum(ev_files)
        a1 = self._agent1_source(ctx, t)
        ctx["agent1_key"] = f"{a1[0]}:{a1[1]}" if a1 else ""
        return ctx

    def _exp_base(self, run_dir: Path, ev: list[dict], start: dict, end: dict | None) -> dict:
        plan = start.get("plan") or {}
        after = [e for e in ev if e["ts"] >= start["ts"] and (end is None or e["ts"] <= end["ts"] + 0.01)]
        inj = next((e for e in after if e["event"] == "inject"), None)
        states = [e.get("to") for e in after if e["event"] == "state"]
        return {"run_id": run_dir.name, "dir": run_dir, "id": plan.get("id"), "fault": plan.get("fault"),
                "target": (plan.get("target") or "").removeprefix("node:"), "rung": plan.get("rung"),
                "plan": plan, "guard": start.get("guard") or {}, "t_start": start["ts"],
                "t_inject": inj.get("t", inj["ts"]) if inj else None, "in_flight": (inj or {}).get("in_flight"),
                "queue_at_inject": (inj or {}).get("queue"), "t_end": end["ts"] if end else None,
                # DONE without experiment_end: the runner skipped it (preflight, no in-flight work, kill switch)
                "active": end is None and (not states or states[-1] not in ("DONE", "LOCKED")) and not self._abandoned(run_dir),
                "skipped": end is None and bool(states) and states[-1] == "DONE",
                "last_state": states[-1] if states else None, "events": after, "end": end}

    def _abandoned(self, run_dir: Path) -> bool:
        """Live only: a journal silent for 10 minutes belongs to a runner that crashed or was killed."""
        if self.src.mode != "live":
            return False
        try:
            return time.time() - (run_dir / "journal.jsonl").stat().st_mtime > 600
        except OSError:
            return True

    # ---------------------------------------------------------------- pods / restarts
    def _restart_baseline(self, ctx: dict, pods: dict | None) -> None:
        """Restarts are shown as a delta since the start of the active (or newest) experiment,
        or since the dashboard started when there is none."""
        exp = ctx.get("exp") or ctx.get("last")
        key = (exp["run_id"], exp["id"]) if exp and exp.get("active") else ("dashboard", self.started)
        if key != self.base_key and pods is not None:
            self.base_key = key
            self.restart_base = {p["uid"]: p["restarts"] or 0 for ps in pods.values() for p in ps if p.get("uid")}

    def _pod_view(self, pods: dict | None, gnodes: dict, t: float) -> dict:
        out = {}
        if pods is None:
            return out
        for node, ps in pods.items():
            desired = (gnodes.get(node) or {}).get("replicas")
            rows, delta, lifetime, recent = [], 0, 0, False
            for p in ps:
                r = p.get("restarts")
                d = None
                if r is not None and p.get("uid"):
                    base = self.restart_base.get(p["uid"], 0)
                    d = max(0, r - base)
                    delta += d
                    lifetime += r
                    prev = self.restart_last.get(p["uid"])
                    if prev is None or r > prev[0]:
                        self.restart_last[p["uid"]] = (r, t if prev is not None else -1e18)
                    recent = recent or t - self.restart_last[p["uid"]][1] < model.RECOVER_HOLD_S
                rows.append({"name": p.get("name"), "phase": p.get("phase"), "ready": p.get("ready"),
                             "restarts": r, "restarts_delta": d, "age_s": round(t - p["created"]) if p.get("created") else None,
                             "last_termination": p.get("last_termination"), "reconstructed": p.get("reconstructed", False)})
            if ps and ps[0].get("reconstructed") and ps[0].get("name") is None:
                ready_count = (desired or 1) if ps[0]["ready"] else 0   # replay: readiness only, no pod list
                rows = []
            else:
                ready_count = sum(1 for p in ps if p.get("ready"))
            new_pods = [p for p in ps if p.get("uid") and p["uid"] not in self.restart_base]
            out[node] = {"pods": rows, "ready_count": ready_count, "desired": desired,
                         "restarts_delta": delta if any(p.get("restarts") is not None for p in ps) else None,
                         "restarts_lifetime": lifetime if any(p.get("restarts") is not None for p in ps) else None,
                         "replaced": len(new_pods) if self.restart_base else 0, "restarted_recently": recent}
        return out

    # ---------------------------------------------------------------- experiment tracking
    def _track(self, exp: dict, states: dict, pod_view: dict, chaos: list | None, t: float) -> None:
        """Live observations the harness only writes at the end: per-node first symptom,
        replacement pod Ready, the pod the fault actually hit."""
        key = (exp["run_id"], exp["id"])
        tr = self.tracks.setdefault(key, {"first_symptom": {}, "target_pods": None, "t_pod_ready": None, "pod": None,
                                          "dipped": False})
        if len(self.tracks) > 20:
            self.tracks.pop(next(iter(self.tracks)))
        t0 = exp.get("t_inject")
        if t0 is None or t < t0:
            return
        target = exp["target"]
        if tr["target_pods"] is None:
            src_pods = (self.prev_pods or {}).get(target) or []
            tr["target_pods"] = {p.get("uid") or p.get("name") for p in src_pods}
        for node, s in states.items():
            if s["state"] in ("Down", "Degraded") and node not in tr["first_symptom"] and exp.get("active"):
                tr["first_symptom"][node] = round(t - t0, 1)
        pv = pod_view.get(target) or {}
        for c in chaos or []:
            if c.get("pods") and c.get("run_id") in (exp["run_id"], None):
                tr["pod"] = c["pods"][0]
        if self.src.mode == "replay" and not tr["pod"]:
            tr["pod"] = self.src.killed_pod(t)
        if not tr["pod"]:
            for p in (self.prev_pods or {}).get(target) or []:
                if p.get("phase") == "Terminating" and (p.get("uid") or p.get("name")) in tr["target_pods"]:
                    tr["pod"] = p["name"]
        if tr["t_pod_ready"] is None and exp.get("active"):
            entry = self.catalog["faults"].get(exp["fault"], {})
            if entry.get("reversible"):
                if pv.get("desired") and pv.get("ready_count", 0) < pv["desired"]:
                    tr["dipped"] = True
                elif tr["dipped"]:
                    tr["t_pod_ready"] = t
            else:
                cur = (self.prev_pods or {}).get(target) or []
                if any(p.get("ready") and (p.get("uid") or p.get("name")) not in tr["target_pods"] for p in cur):
                    tr["t_pod_ready"] = t

    def _exp_view(self, x: dict, ctx: dict, graph: dict, t: float) -> dict:
        tr = self.tracks.get((x["run_id"], x["id"]), {})
        entry = self.catalog["faults"].get(x["fault"], {})
        peer = (entry.get("params") or {}).get("peer")
        edge = f"edge:{x['target']}->{peer}" if peer else None
        if edge and not any(e["id"] == edge for e in graph["edges"]):
            edge = None
        gn = {n["name"]: n for n in graph["nodes"]}
        br = (gn.get(x["target"]) or {}).get("blast_radius") or {"nodes": [], "client_impact": False}
        evidence = self._file(x["dir"] / "evidence" / f"{x['id']}.json", t) if not x["active"] else None
        res_tl = (evidence or {}).get("timeline") or {}
        status = (x.get("end") or {}).get("status")
        if x["active"]:
            phase = STATE_TO_PHASE.get(x["last_state"], x["last_state"] or "starting")
        elif status == "completed":
            phase = "recovered"
        elif status == "aborted":
            phase = "auto-stopped, recovered" if (evidence or {}).get("manual_action_needed") is False else "auto-stopped"
        else:
            phase = status or "ended"
        armed = next((e["ts"] for e in x["events"] if e["event"] == "safety_armed"), None)
        live = {"t_pod_ready": tr.get("t_pod_ready")}
        if tr.get("first_symptom") and x.get("t_inject"):
            live["t_first_symptom"] = x["t_inject"] + min(tr["first_symptom"].values())
        tl = model.timeline({"t_inject": x.get("t_inject"), "baseline_stable_ts": armed, "live": live,
                             "result_timeline": res_tl, "events": x["events"],
                             "recovery_time_s": (evidence or {}).get("recovery_time_s")})
        fl = x.get("in_flight") or {}
        return {"run_id": x["run_id"], "id": x["id"], "fault": x["fault"], "fault_label": _fault_label(x["fault"]),
                "chaos_kind": entry.get("chaos_kind"), "variant": model._variant(entry, x.get("rung") or ""),
                "rung": x.get("rung"), "tier": entry.get("tier"), "reversible": entry.get("reversible"),
                "target": x["target"], "edge": edge, "active": x["active"], "phase": phase, "state": x["last_state"],
                "t_start": x["t_start"], "t_inject": x.get("t_inject"), "t_end": x.get("t_end"),
                "t_fault_end": self._fault_end(x, entry),
                "pod": tr.get("pod"), "in_flight": {**fl, "total": sum(fl.values())} if fl else None,
                "blast_radius": br["nodes"], "client_impact": br["client_impact"],
                "workflows_affected": (gn.get(x["target"]) or {}).get("workflows_affected") or {},
                "first_symptom": tr.get("first_symptom") or {}, "timeline": tl, "status": status,
                "abort_reason": next((e.get("reason") for e in x["events"] if e["event"] == "abort"), None),
                "recovery_time_s": (evidence or {}).get("recovery_time_s"),
                "hypothesis": x["plan"].get("hypothesis"), "reason": x["plan"].get("reason"),
                "cites": x["plan"].get("cites") or []}

    @staticmethod
    def _fault_end(x: dict, entry: dict) -> float | None:
        """When the fault stopped acting: its CR was deleted, or a timed fault's duration ran out."""
        deleted = next((e["ts"] for e in x["events"] if e["event"] == "chaos_delete_all"), None)
        dur = ((entry.get("ladder") or {}).get(x.get("rung")) or {}).get("duration")
        if x.get("t_inject") and dur:
            return min(deleted or 1e18, x["t_inject"] + float(dur))
        return deleted

    def _file(self, path: Path, t: float):
        """A run file, but in replay only once the virtual clock has passed the moment it was written."""
        if not path.exists():
            return None
        if self.src.mode == "replay":
            end = next((e["ts"] for e in self.src.journal(self.src.dir, t) if e["event"] == "experiment_end"), None)
            if end is None or t < end:
                return None
        return read_json(path)

    # ---------------------------------------------------------------- components
    def _components(self, graph, gnodes, pod_view, states, sample, lg, exp, platform, history) -> dict:
        roles = model.roles(graph)
        nodes = {}
        for name, n in gnodes.items():
            if n.get("kind") == "obs":
                continue
            pv = pod_view.get(name) or {}
            pods_rows = list(pv.get("pods", []))
            if exp and exp.get("active") and exp.get("target") == name and exp.get("pod") \
                    and not any(p.get("name") == exp["pod"] for p in pods_rows):
                # a 0 s grace kill deletes the pod within a second; keep it visible as what the fault hit
                pods_rows.insert(0, {"name": exp["pod"], "phase": "Killed (deleted)", "ready": False, "restarts": None,
                                     "restarts_delta": None, "age_s": None, "last_termination": "Chaos Mesh", "reconstructed": False})
            metric: dict = {}
            r = roles.get(name)
            if n.get("kind") == "external":
                metric = {"load_on": sample["load_on"], "rate": (lg or {}).get("rate"),
                          "profile": profile_name((lg or {}).get("rate") or 0) if sample["load_on"] else "off",
                          "w1_pending": (lg or {}).get("w1_pending")}
            elif r == "queue":
                q = sample["redis"] or {}
                metric = {"reachable": q.get("reachable"), "waiting": q.get("wait"), "active": q.get("active"),
                          "dbsize": q.get("dbsize")} if q else {}
            elif r == "db":
                metric = dict(sample["postgres"] or {})
            elif r == "mock":
                metric = dict(sample["mock"] or {})
            elif r == "consumer":
                metric = {"concurrency": self.concurrency.get(name)}
            elif r == "client_facing":
                metric = {"w1_p95": sample.get("w1_p95") if sample["load_on"] else None,
                          "w1_err_rate": sample.get("w1_err_rate") if sample["load_on"] else None}
            first = (exp or {}).get("first_symptom", {}).get(name) if exp and exp.get("active") else None
            nodes[name] = {"name": name, "kind": n.get("kind"), "state": (states.get(name) or {}).get("state"),
                           "why": (states.get(name) or {}).get("why", []), "replicas": n.get("replicas"), "spof": n.get("spof"),
                           "ready": pv.get("ready_count"), "desired": pv.get("desired"), "pods": pods_rows,
                           "restarts_delta": pv.get("restarts_delta"), "restarts_lifetime": pv.get("restarts_lifetime"),
                           "replaced": pv.get("replaced"), "metric": metric, "first_symptom_s": first,
                           "pods_reconstructed": self.src.mode == "replay",
                           "tested": [{k: h.get(k) for k in ("fault", "run_id", "exp_id", "status", "passed")}
                                      for h in history if h.get("node") == name][-8:]}
        return {"nodes": nodes, "platform": platform or {}, "restart_baseline": self._baseline_label()}

    def _baseline_label(self) -> str:
        k = self.base_key
        if not k:
            return "-"
        return "since dashboard start" if k[0] == "dashboard" else f"since {k[1]} start"

    # ---------------------------------------------------------------- events
    def _merge_events(self, k8s_events: list | None, ctx: dict) -> list[dict]:
        out = []
        for e in k8s_events or []:
            out.append({"ts": e["ts"], "source": "k8s", "kind": e.get("reason"), "object": e.get("object"),
                        "level": "warn" if e.get("type") == "Warning" else "info",
                        "message": model.redact(f"{e.get('reason')} {e.get('object')}: {e.get('message')}")})
        for e in ctx.get("events") or []:
            msg = journal_message(e)
            if msg is None:
                continue
            src = "safety" if e["event"] in SAFETY_EVENTS else "harness"
            out.append({"ts": e["ts"], "source": src, "kind": e["event"], "object": None,
                        "level": "fault" if e["event"] in ("abort", "kill_switch", "kill_switch_exec") else "info",
                        "message": model.redact(msg)})
        for e in self._cache.get("degraded") or []:
            out.append({"ts": e.get("ts", 0), "source": "harness", "kind": "degraded", "object": None, "level": "warn",
                        "message": model.redact(f"{model.ALERT_LABEL.get(e.get('kind'), 'WARNING')}: {e.get('component')}: {e.get('reason')}")})
        out.sort(key=lambda e: e["ts"])
        return out[-EVENT_LIMIT:]

    # ---------------------------------------------------------------- state
    def _state(self, t, ctx, exp, last, lg, rd, pg, pods, wf, flags, platform) -> dict:
        ev = ctx.get("events") or []
        active = bool(exp and exp.get("active"))
        exp_events = [e for e in ev if e["ts"] >= (exp or {}).get("t_start", 1e30)]
        if flags.get("locked"):
            ks = {"status": "triggered", "text": "LOCKED: run chaosctl recover --ack after checking the cluster"}
        elif flags.get("kill_file"):
            ks = {"status": "triggered", "text": "triggered (./KILL present)"}
        elif any(e["event"] in ("kill_switch", "kill_switch_exec") for e in exp_events):
            ks = {"status": "triggered", "text": "triggered during this experiment"}
        elif active and any(e["event"] == "safety_armed" for e in exp_events):
            ks = {"status": "armed", "text": "armed (CLI, ./KILL, Ctrl-C)"}
        elif active:
            ks = {"status": "standby", "text": "arms after warm-up"}
        else:
            ks = {"status": "standby", "text": "not armed: no experiment running"}
        state_name = (exp or {}).get("state") if active else ("LOCKED" if flags.get("locked") else "IDLE")
        lx = exp or last
        diag_done = bool(lx and self._file(REPORTS / lx["run_id"] / "evidence" / f"{lx['id']}.diagnosis.json", t))
        resil = self._every("resilience", 10, t, lambda: self._resilience(t))
        self.versions["resilience"] = f"{(resil or {}).get('path')}:{(resil or {}).get('generated')}"
        reported = bool(resil and ctx.get("run_id") in (resil.get("runs") or []))
        phases = model.phase_strip(ev, t, self.graph is not None, ctx.get("baseline") is not None, diag_done, reported)
        srcs = {
            "cluster": "ok" if pods is not None else ("not recorded" if self.src.mode == "replay" else "unreachable"),
            "cluster_error": None if pods is not None else self.src.cluster_error,
            "redis": "ok" if rd.get("reachable") else ("no data" if not rd else "unreachable"),
            "postgres": "ok" if (pg or {}).get("reachable") else ("no data" if not pg else "unreachable"),
            "loadgen": "on" if lg and lg["load_on"] else ("no data" if lg is None else "off"),
            "graph": self.graph_source,
            "prometheus": (platform or {}).get("prometheus", {}).get("status", "not recorded" if self.src.mode == "replay" else "unknown"),
        }
        b = ctx.get("baseline") or {}
        own = []   # the dashboard's own blind spots, listed with the harness alerts
        if pods is None and self.src.mode == "live":
            own.append({"kind": "warning", "component": "dashboard", "reason": f"Kubernetes API unreachable ({self.src.cluster_error})", "ts": t})
        if self.graph is None:
            own.append({"kind": "warning", "component": "dashboard", "reason": "dependency graph missing (run chaosctl graph)", "ts": t})
        elif self.graph_source == "file" and self.src.mode == "live":
            own.append({"kind": "warning", "component": "dashboard", "reason": "map from graph.json: live cluster graph unavailable", "ts": t})
        alerts = model.alert_banner(self._every("degraded", 5, t, lambda: self.src.degraded(t)) + own)
        return {
            "alerts": alerts, "collector_error": None, "llm_settings": getattr(self, "llm_settings", None),
            "now": t, "collected_at": t, "wall": time.time(), "mode": self.src.mode, "stale_after_s": STALE_AFTER_S,
            "replay": {"run_id": self.src.run_id, "start": self.src.start, "end": self.src.end} if self.src.mode == "replay" else None,
            "sources": srcs, "run_id": ctx.get("run_id"),
            "campaign": {"state": state_name, "active": active, "run_id": (exp or {}).get("run_id") or ctx.get("run_id"),
                         "exp_id": (exp or {}).get("id")},
            "kill_switch": ks,
            "loadgen": {"on": bool(lg and lg["load_on"]), "rate": (lg or {}).get("rate"),
                        "profile": profile_name(lg["rate"]) if lg and lg["load_on"] else "off"},
            "baseline": {"w1_p95_ms": (b.get("w1") or {}).get("p95_ms"), "w1_success": (b.get("w1") or {}).get("success_rate"),
                         "wait_max": (b.get("queue") or {}).get("wait_max"), "rate": b.get("rate"),
                         "captured_at": b.get("captured_at")} if b else None,
            "phases": phases, "experiment": exp, "last_experiment": last, "workflows": wf,
        }

    # ---------------------------------------------------------------- on-demand responses
    def api_graph(self) -> dict:
        with self.lock:
            return {"source": self.graph_source, "built_at": self.graph_built, "version": self.versions["graph"],
                    "graph": self.graph, "layout": self.layout, "roles": model.roles(self.graph) if self.graph else {},
                    "concurrency": self.concurrency}

    def api_state(self) -> dict:
        with self.lock:
            return self.state

    def api_components(self) -> dict:
        with self.lock:
            return self.components

    def api_metrics(self, window_s: float) -> dict:
        with self.lock:
            pts = list(self.points)
            now = self.state.get("now")
        if now is not None:
            pts = [p for p in pts if p["t"] >= now - window_s]
        return {"now": now, "window_s": window_s, "points": pts}

    def api_events(self, since: float) -> dict:
        with self.lock:
            return {"events": [e for e in self.events if e["ts"] > since]}

    def _agent1_source(self, ctx: dict, t: float):
        """Newest Agent 1 decision: the current run's, else the newest `chaosctl plan` output."""
        for d in [ctx.get("run_dir")] + ([REPORTS / "plans"] if self.src.mode == "live" else []):
            if d is None:
                continue
            ev = self.src.journal(d, t)
            if any(e["event"] == "agent1_plan" for e in ev):
                return d, max(e["ts"] for e in ev if e["event"] == "agent1_plan")
        return None

    def api_agent1(self) -> dict:
        t = self.src.now()
        ctx = self._run_context(t, self.src.flags())
        found = self._agent1_source(ctx, t)
        if not found:
            return {"available": False}
        d, _ = found
        ev = self.src.journal(d, t)
        plan_ev = [e for e in ev if e["event"] == "agent1_plan"][-1]
        scored_ev = [e for e in ev if e["event"] == "agent1_scored" and e["ts"] <= plan_ev["ts"]]
        t_scored = scored_ev[-1]["ts"] if scored_ev else 0
        llm_ev = [e for e in ev if e["event"] in ("llm", "agent1_fallback") and e.get("purpose", "injection_select") == "injection_select"
                  and t_scored <= e["ts"] <= plan_ev["ts"]]
        exps = (plan_ev.get("plan") or {}).get("experiments") or []
        verdicts = {v["exp_id"]: v for v in plan_ev.get("verdicts") or []}
        x = ctx.get("exp") or (ctx.get("last") if ctx.get("last") and ctx["last"]["run_id"] == d.name else None)
        chosen = None
        if x:
            chosen = x["plan"]
            verdict = x.get("guard") or verdicts.get(chosen.get("id"))
        elif exps:
            chosen = exps[0]
            verdict = verdicts.get(chosen["id"])
        else:
            verdict = None
        picked = {(c["target"], c["fault"], c["rung"]) for c in ([chosen] if chosen else [])}
        rows = model.candidates_table(scored_ev[-1]["candidates"] if scored_ev else [], self.catalog, ctx.get("history") or [], picked)
        notes = [e.get("note") for e in ev if e["event"] == "guard_note" and t_scored <= e["ts"] <= plan_ev["ts"] + 1]
        return {"available": True, "run_id": d.name, "ts": plan_ev["ts"], "source": model.llm_source(plan_ev.get("source"), llm_ev),
                "chosen": chosen, "chosen_variant": model._variant(self.catalog["faults"].get((chosen or {}).get("fault"), {}),
                                                                   (chosen or {}).get("rung") or ""),
                "verdict": verdict, "guard_notes": notes, "plan_size": len(exps), "candidates": rows}

    def api_experiment(self, exp_id: str, run_id: str | None) -> dict:
        t = self.src.now()
        ctx = self._run_context(t, self.src.flags())
        x = None
        if exp_id == "current" or run_id is None:
            x = ctx.get("exp") or ctx.get("last")
            if x and exp_id not in ("current", x["id"]):
                x = None
        if x is None and run_id:
            d = REPORTS / run_id
            ev = self.src.journal(d, t)
            starts = [e for e in ev if e["event"] == "experiment_start" and (e.get("plan") or {}).get("id") == exp_id]
            if starts:
                end = next((e for e in ev if e["event"] == "experiment_end" and e["ts"] >= starts[-1]["ts"]), None)
                x = self._exp_base(d, ev, starts[-1], end)
        if x is None:
            return {"available": False}
        evid = self._file(x["dir"] / "evidence" / f"{x['id']}.json", t) or {}
        plan_file = self._file(x["dir"] / "evidence" / f"{x['id']}.plan.json", t) or {}
        diag = self._file(x["dir"] / "evidence" / f"{x['id']}.diagnosis.json", t)
        fates = evid.get("fates") or {}
        out = {"available": True, "run_id": x["run_id"], "id": x["id"], "active": x["active"], "plan": x["plan"],
               "guard": x["guard"], "status": (x.get("end") or {}).get("status"),
               "abort_reason": next((e.get("reason") for e in x["events"] if e["event"] == "abort"), None),
               "recovery_time_s": evid.get("recovery_time_s"), "manual_action_needed": evid.get("manual_action_needed"),
               "restarts_delta": evid.get("restarts_delta"), "timeline": evid.get("timeline"),
               "in_flight_at_inject": x.get("in_flight"), "queue_at_inject": x.get("queue_at_inject"),
               "fates": model.fate_rows(fates.get("summary") or (x.get("end") or {}).get("fates"), fates.get("rows")),
               "diagnosis": None, "grade": None}
        if diag:
            from chaosctl.agents.diagnosis import PLAYBOOK, grade
            llm_ev = [e for e in self.src.journal(x["dir"], t) if e["event"] == "llm" and e.get("purpose") == "diagnosis"
                      and e["ts"] >= (x.get("t_end") or 0) - 1]
            sev_rank = ["Critical", "High", "Medium", "Low"]
            det = {h["id"]: h for h in diag.get("detectors") or []}
            fixes = []
            for f in diag.get("fixes") or []:
                pb = PLAYBOOK.get(f["playbook_id"], {})
                sevs = [det[a]["severity"] for a in pb.get("addresses", []) if a in det]
                fixes.append({"id": f["playbook_id"], "title": pb.get("title", f["playbook_id"]), "why": model.redact(f.get("why", "")),
                              "severity": min(sevs, key=sev_rank.index) if sevs else None,
                              "effort": pb.get("effort"), "impact": pb.get("impact"),
                              "runbook": {k: pb.get(k) for k in ("change", "verify", "rollback", "source") if pb.get(k)}})
            out["diagnosis"] = {"source": model.llm_source(diag.get("source"), llm_ev, "detector-only template diagnosis"),
                                "thoughts": [{"step": s["step"], "text": model.redact(s["text"])} for s in diag.get("thoughts") or []],
                                "root_cause": model.redact(diag.get("root_cause", "")),
                                "failure_chain": [model.redact(s) for s in diag.get("failure_chain") or []],
                                "detectors": diag.get("detectors") or [], "fixes": fixes}
            if plan_file:
                out["grade"] = grade(diag, plan_file, evid or None)
        return out

    def _resilience(self, t: float) -> dict | None:
        if self.src.mode == "replay":
            return find_resilience(REPORTS, self.src.run_id) if self._file(self.src.dir / "findings.json", t) else None
        return find_resilience(REPORTS)

    def api_resilience(self) -> dict:
        r = self._resilience(self.src.now())
        return {"available": r is not None, **(r or {})}

    # ---------------------------------------------------------------- SSE
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
