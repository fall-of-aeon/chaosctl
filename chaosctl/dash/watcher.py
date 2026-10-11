"""Agent 2, always on: watches the dashboard's live health model and investigates on its own.

Trigger (deterministic, no AI): any service Down or Degraded for TRIGGER_TICKS collector ticks in a row. The
investigation (chaosctl/agents/investigator.py) runs in its own thread and is streamed to the page step by step.
After a conclusion the watcher re-arms only once every service has been healthy for REARM_S seconds, so one
incident gives one investigation.

Blind: the watcher reads service states and the probes, never the run journal, the plan or Chaos Mesh objects.
It does note which run was active (the run id only), so the record can be filed next to that run.

Second job: when a chaos run writes its evidence bundle (reports/<run>/evidence/exp-NN.json) after this dashboard
started, the watcher runs the full evidence diagnosis on it (same as `chaosctl diagnose`, graded afterwards
against the plan) and writes the fix summary, so the Story fills in without a command.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

from chaosctl.dash.sources import REPORTS, STATE, read_json

TRIGGER_TICKS = 3
REARM_S = 30
KEEP = 12
INCIDENTS = STATE / "incidents"
# reasons that come from the harness's own configuration, not from a symptom (would leak an injection)
HIDDEN_REASONS = ("mock error rate",)


def unhealthy(nodes: dict) -> dict[str, list[str]]:
    """{service: [why...]} for every Down/Degraded service, minus reasons that are not symptoms."""
    out = {}
    for name, c in (nodes or {}).items():
        if c.get("kind") == "external" or c.get("state") not in ("Down", "Degraded"):
            continue
        why = [w for w in (c.get("why") or []) if not any(h in w for h in HIDDEN_REASONS)]
        if why or c.get("state") == "Down":
            out[name] = why or [c["state"].lower()]
    return out


def trigger_text(bad: dict[str, list[str]], state_of: dict[str, str]) -> str:
    return "\n".join(f"- {n} is {state_of.get(n, '?')}: {', '.join(w)}" for n, w in bad.items())


class Agent2Watcher(threading.Thread):
    def __init__(self, hub, use_llm: bool = True, investigate=None):
        super().__init__(daemon=True, name="agent2-watcher")
        self.hub = hub
        self.use_llm = use_llm
        self._investigate = investigate          # tests inject a fake
        self.lock = threading.Lock()
        self.streak = 0
        self.armed = True
        self.healthy_since: float | None = time.time()
        self.current: dict | None = None
        self.recent: deque = deque(maxlen=KEEP)
        self.checked_at = None
        self.bad: dict = {}
        self.started_at = time.time()
        self.bundles_seen: set[str] = set()
        self.bundle_job: dict | None = None
        self.enabled = True
        INCIDENTS.mkdir(parents=True, exist_ok=True)
        for p in sorted(INCIDENTS.glob("inc-*.json"))[-KEEP:]:
            rec = read_json(p)
            if rec:
                self.recent.appendleft(rec)

    # ---------------------------------------------------------------- loop
    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:   # never die; say so on the page
                with self.lock:
                    self.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(1)

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        with self.hub.lock:
            nodes = dict((self.hub.components or {}).get("nodes") or {})
        bad = unhealthy(nodes)
        with self.lock:
            self.checked_at, self.bad, self.error = now, bad, None
            busy = self.current is not None and self.current["status"] == "investigating"
            if bad:
                self.streak += 1
                self.healthy_since = None
            else:
                self.streak = 0
                self.healthy_since = self.healthy_since or now
                if not self.armed and not busy and now - self.healthy_since >= REARM_S:
                    self.armed = True
            fire = self.enabled and bad and self.armed and not busy and self.streak >= TRIGGER_TICKS
        if fire:
            self.begin(trigger_text(bad, {n: nodes[n].get("state") for n in bad}), bad, "monitor")
        self._check_bundles()

    # ---------------------------------------------------------------- investigations
    def begin(self, trigger: str, bad: dict, by: str) -> dict | None:
        with self.lock:
            if self.current is not None and self.current["status"] == "investigating":
                return None
            self.armed = False
            active = read_json(STATE / "active_run.json") or {}
            rec = {"id": time.strftime("inc-%m%d-%H%M%S"), "started": time.time(), "by": by, "trigger": trigger,
                   "services": sorted(bad), "status": "investigating", "phase": "starting", "steps": [],
                   "run_id": active.get("run_id")}
            self.current = rec
        threading.Thread(target=self._run_investigation, args=(rec,), daemon=True, name=f"agent2-{rec['id']}").start()
        return rec

    def manual(self) -> dict | None:
        with self.lock:
            bad = dict(self.bad)
            nodes = dict((self.hub.components or {}).get("nodes") or {})
        text = trigger_text(bad, {n: nodes.get(n, {}).get("state") for n in bad}) if bad else \
            "- no service is Down or Degraded right now; an operator asked for a health check"
        return self.begin(text, bad, "operator")

    def _toolkit(self):
        from chaosctl.agents.investigator import ToolKit
        hub, src = self.hub, self.hub.src

        def pods():
            return src.pods(time.time())

        def logs(service, since_s):
            from chaosctl.probes import K8sProbe
            if not src._k8s():
                raise RuntimeError("Kubernetes API unreachable")
            k = K8sProbe(core=src._core)
            words = ("error", "warn", "fail", "lost", "stalled", "space", "panic", "timeout", "refused", "econn", "fatal")
            out = []
            for p in (pods() or {}).get(service, []):
                since = time.time() - since_s
                lines = k.logs(p["name"], 300, since_ts=since, until_ts=time.time())
                if p.get("restarts"):
                    lines = [f"[previous container] {x}" for x in k.logs(p["name"], 100, since_ts=since, until_ts=time.time(),
                                                                         previous=True)] + lines
                out += [f"{p['name']}: {x}" for x in lines if any(w in x.lower() for w in words)]
            return out

        def downstream():
            import httpx
            out = []
            for _ in range(3):
                t0 = time.time()
                try:
                    r = httpx.get("http://127.0.0.1:18080/healthz", timeout=6)
                    out.append({"status": r.status_code, "ms": round((time.time() - t0) * 1000)})
                except Exception as exc:
                    out.append({"error": type(exc).__name__, "ms": round((time.time() - t0) * 1000)})
            return out

        def points():
            with hub.lock:
                return list(hub.points)

        return ToolKit(pods=pods, events=lambda: src.events(time.time()), logs=logs, redis=lambda: src.redis(time.time()),
                       postgres=lambda: src.postgres(time.time()), points=points,
                       baseline=lambda: read_json(STATE / "baseline.json"), graph=lambda: hub.graph, downstream=downstream)

    def _run_investigation(self, rec: dict) -> None:
        from chaosctl.journal import Journal
        journal = Journal(REPORTS / rec["run_id"] / "journal.jsonl") if rec.get("run_id") else Journal(INCIDENTS / "journal.jsonl")
        journal.write({"event": "agent2_investigation_start", "incident": rec["id"], "by": rec["by"], "services": rec["services"]})

        def on_step(u: dict) -> None:
            with self.lock:
                rec["steps"] = [dict(s) for s in u["steps"]]
                rec["phase"] = u["phase"]

        try:
            fn = self._investigate
            if fn is None:
                from chaosctl.agents.investigator import investigate as fn
            out = fn(rec["trigger"], self._toolkit(), journal=journal, use_llm=self.use_llm, on_step=on_step)
            from chaosctl.agents.diagnosis import PLAYBOOK
            for f in out["diagnosis"].get("fixes") or []:   # titles and the exact change come from the playbook
                pb = PLAYBOOK.get(f["playbook_id"], {})
                f.update(title=pb.get("title"), change=pb.get("change"), verify=pb.get("verify"))
            with self.lock:
                rec.update(status="concluded", phase="done", ended=time.time(), steps=out["steps"], source=out["source"],
                           steps_source=out.get("steps_source"), llm_used=out["llm_used"], detectors=out["detectors"],
                           diagnosis=out["diagnosis"])
        except Exception as exc:
            with self.lock:
                rec.update(status="failed", phase="done", ended=time.time(), error=f"{type(exc).__name__}: {str(exc)[:200]}")
        journal.write({"event": "agent2_investigation_end", "incident": rec["id"], "status": rec["status"],
                       "root_cause": (rec.get("diagnosis") or {}).get("root_cause"), "source": rec.get("source")})
        self._save(rec)
        with self.lock:
            self.recent.appendleft(rec)
            self.healthy_since = None if self.bad else time.time()

    def _save(self, rec: dict) -> None:
        body = json.dumps(rec, indent=2, default=str)
        (INCIDENTS / f"{rec['id']}.json").write_text(body)
        if rec.get("run_id") and (REPORTS / rec["run_id"]).is_dir():
            d = REPORTS / rec["run_id"] / "investigations"
            d.mkdir(exist_ok=True)
            (d / f"{rec['id']}.json").write_text(body)

    # ---------------------------------------------------------------- evidence bundles of finished runs
    def _check_bundles(self) -> None:
        if self.bundle_job and self.bundle_job.get("status") == "running":
            return
        for run in sorted((p for p in REPORTS.glob("r*") if p.is_dir()), key=lambda p: p.name, reverse=True)[:3]:
            for ev in sorted((run / "evidence").glob("exp-[0-9][0-9].json")):
                key = f"{run.name}/{ev.stem}"
                if key in self.bundles_seen:
                    continue
                if ev.stat().st_mtime < self.started_at or (ev.parent / f"{ev.stem}.diagnosis.json").exists():
                    self.bundles_seen.add(key)
                    continue
                self.bundles_seen.add(key)
                self.bundle_job = {"run_id": run.name, "exp": ev.stem, "status": "running", "started": time.time()}
                threading.Thread(target=self._diagnose_bundle, args=(run, ev.stem, self.bundle_job), daemon=True).start()
                return

    def _diagnose_bundle(self, run: Path, exp: str, job: dict) -> None:
        from chaosctl import fix_summary, llm
        from chaosctl.agents.diagnosis import diagnose
        from chaosctl.journal import Journal
        try:
            time.sleep(1)   # the runner writes the bundle and then the plan file
            j = Journal(run / "journal.jsonl")
            llm.budget.allow(4)
            out = diagnose(json.loads((run / "evidence" / f"{exp}.json").read_text()), journal=j, use_llm=self.use_llm)
            (run / "evidence" / f"{exp}.diagnosis.json").write_text(json.dumps(out, indent=2))
            fix_summary.write(run, exp, fix_summary.build(out, use_llm=self.use_llm, journal=j))
            job.update(status="done", source=out["source"], root_cause=out["root_cause"], ended=time.time())
        except Exception as exc:
            job.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}", ended=time.time())

    # ---------------------------------------------------------------- view
    def view(self) -> dict:
        with self.lock:
            cur = self.current
            busy = cur is not None and cur["status"] == "investigating"
            if not self.enabled:
                state = "paused"
            elif busy:
                state = "investigating"
            elif self.bad and not self.armed:
                state = "concluded"       # same incident still going; re-arms after REARM_S healthy
            elif self.bad:
                state = "suspicious"
            elif not self.armed:
                state = "re-arming"
            else:
                state = "watching"
            return {"available": True, "state": state, "enabled": self.enabled, "checked_at": self.checked_at,
                    "unhealthy": self.bad, "streak": self.streak, "trigger_ticks": TRIGGER_TICKS, "armed": self.armed,
                    "rearm_in_s": (max(0, round(REARM_S - (time.time() - self.healthy_since))) if self.healthy_since and not self.armed else None),
                    "current": json.loads(json.dumps(cur, default=str)) if cur else None,
                    "recent": [{k: r.get(k) for k in ("id", "started", "ended", "status", "services", "run_id", "by", "source")}
                               | {"root_cause": (r.get("diagnosis") or {}).get("root_cause")} for r in self.recent],
                    "bundle_job": dict(self.bundle_job) if self.bundle_job else None,
                    "error": getattr(self, "error", None)}

    def incident(self, inc_id: str) -> dict | None:
        with self.lock:
            for r in ([self.current] if self.current else []) + list(self.recent):
                if r and r["id"] == inc_id:
                    return json.loads(json.dumps(r, default=str))
        return read_json(INCIDENTS / f"{inc_id}.json")
