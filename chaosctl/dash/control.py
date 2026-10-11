"""The dashboard's only write path: Agent 1 on a button, a human approval, the kill switch, steady traffic.

    Ask Agent 1  -> plan in a thread (the same injection agent as `chaosctl plan`: score -> Mistral -> validate ->
                    policy guard), journalled in reports/plans/journal.jsonl; the first approved experiment becomes
                    the proposal shown on the page. Nothing is injected.
    Approve      -> `chaosctl execute --proposal ... --run-id ...` in its own process: the exact approved plan runs
                    through the campaign graph under the safety controller (runtime guard, preflight, in-flight
                    gate, abort limits, recovery watchdog). The dashboard does not inject anything itself.
    Kill switch  -> ./KILL (the running safety controller aborts within a tick) + SIGTERM to the run + the same
                    kill_switch() as `chaosctl kill` (delete all managed chaos, wait for every service Ready).
    Traffic      -> steady load from this process so Agent 2 has client-side signals between runs; switched off
                    before an approved run starts (the runner brings its own load and warm-up).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from chaosctl.dash.sources import LOADGEN_DB, REPORTS, ROOT, STATE, read_json

PROPOSALS = STATE / "proposals"
PLAN_JOURNAL = REPORTS / "plans" / "journal.jsonl"


class ControlError(Exception):
    """Refused request; the message is shown on the page."""


def _cli() -> list[str]:
    exe = Path(sys.executable).with_name("chaosctl")
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "chaosctl.cli"]


class Control:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.state = "idle"        # idle | planning | proposed | running | finished | failed
        self.message = None
        self.proposal: dict | None = None
        self.proc: subprocess.Popen | None = None
        self.run: dict | None = None
        self.loadgen = None
        self.kill_job: dict | None = None
        PROPOSALS.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- blockers
    def blockers(self) -> list[str]:
        out = []
        if (ROOT / "KILL").exists():
            out.append("kill switch is tripped (./KILL present): press Reset kill switch first")
        if read_json(STATE / "LOCKED.json"):
            out.append("LOCKED after a failed recovery: check the cluster, then run `chaosctl recover --ack`")
        if self.proc is not None and self.proc.poll() is None:
            out.append(f"run {self.run['run_id']} is still going")
        if not (STATE / "baseline.json").exists():
            out.append("no baseline: run `chaosctl baseline` first")
        if self.hub.src.mode != "live":
            out.append("replay mode: controls are off")
        return out

    # ---------------------------------------------------------------- Agent 1
    def plan(self, exclude: list[str] | None = None) -> dict:
        with self.lock:
            if self.state == "planning":
                raise ControlError("Agent 1 is already planning")
            if self.proc is not None and self.proc.poll() is None:
                raise ControlError(f"run {self.run['run_id']} is still going")
            self.state, self.message, self.proposal = "planning", "Agent 1 is reading the dependency map", None
        threading.Thread(target=self._plan, args=(list(exclude or []),), daemon=True, name="agent1-plan").start()
        return self.view()

    def _plan(self, exclude: list[str]) -> None:
        try:
            from chaosctl.agents.campaign import load_history
            from chaosctl.agents.injection import build_injection_agent
            from chaosctl.chaos.chaosmesh import load_catalog
            from chaosctl.graph import Graph
            from chaosctl.journal import Journal
            from chaosctl.runner import load_policy

            docs = self.hub.src.manifests()
            if not docs:
                raise ControlError(f"cannot read the cluster to build the dependency map ({self.hub.src.cluster_error})")
            g = Graph.build(docs)
            catalog = load_catalog()
            enabled = [f for f, e in catalog["faults"].items() if e.get("enabled", True)]
            only = [f for f in enabled if f not in exclude] if exclude else None
            if only == []:
                raise ControlError("every enabled fault is excluded")
            j = Journal(PLAN_JOURNAL)
            t0 = time.time()
            out = build_injection_agent(j).invoke({"graph": g, "catalog": catalog, "policy": load_policy(),
                                                   "history": load_history(), "use_llm": True, "only": only})
            exps = out["plan"].experiments
            if only:
                exps = [e for e in exps if e.fault in only]
            if not exps:
                raise ControlError("Agent 1 produced no experiment the guard approved: " + "; ".join(out.get("notes") or []))
            item = exps[0]
            verdict = next(v for v in out["verdicts"] if v.exp_id == item.id)
            events = [e for e in j.read() if e["ts"] >= t0 - 0.5 and e["event"] in
                      ("agent1_scored", "agent1_plan", "agent1_fallback", "agent1_invalid", "agent1_guard_reject", "guard_note", "llm")]
            scored = next((e["candidates"] for e in reversed(events) if e["event"] == "agent1_scored"), [])
            prop = {"proposal_id": uuid.uuid4().hex[:10], "ts": time.time(), "source": out["source"],
                    "item": item.model_dump(), "verdict": verdict.model_dump(), "notes": out.get("notes") or [],
                    "plan_size": len(out["plan"].experiments), "excluded": exclude,
                    "candidates": [{k: c.get(k) for k in ("fault", "target", "rung", "score", "tier")} for c in scored[:8]],
                    "planning_events": events}
            (PROPOSALS / f"{prop['proposal_id']}.json").write_text(json.dumps(prop, indent=2, default=str))
            with self.lock:
                self.proposal, self.state, self.message = prop, "proposed", None
        except Exception as exc:
            with self.lock:
                self.state, self.message = "failed", f"Agent 1 planning failed: {type(exc).__name__}: {str(exc)[:240]}"

    def cancel(self) -> dict:
        with self.lock:
            if self.state == "proposed":
                self.state, self.proposal, self.message = "idle", None, "proposal discarded; nothing was injected"
        return self.view()

    def approve(self, proposal_id: str) -> dict:
        with self.lock:
            if self.state != "proposed" or not self.proposal or self.proposal["proposal_id"] != proposal_id:
                raise ControlError("no such proposal waiting for approval (it may have been replaced)")
            problems = self.blockers()
            if problems:
                raise ControlError("; ".join(problems))
            prop = self.proposal
            self._traffic_off_locked()
            from chaosctl.runner import new_run_id
            run_id = new_run_id()
            (REPORTS / run_id).mkdir(parents=True, exist_ok=True)
            log = (REPORTS / run_id / "console.log").open("w")
            cmd = _cli() + ["execute", "--proposal", str(PROPOSALS / f"{proposal_id}.json"), "--run-id", run_id]
            self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         env={**os.environ, "PYTHONUNBUFFERED": "1", "COLUMNS": "160", "NO_COLOR": "1", "TERM": "dumb"}, start_new_session=True)
            self.run = {"run_id": run_id, "pid": self.proc.pid, "started": time.time(), "fault": prop["item"]["fault"],
                        "target": prop["item"]["target"], "rung": prop["item"]["rung"], "proposal_id": proposal_id,
                        "returncode": None}
            self.state, self.message = "running", None
        threading.Thread(target=self._wait, daemon=True, name="agent1-run").start()
        return self.view()

    def _wait(self) -> None:
        rc = self.proc.wait()
        res = read_json(REPORTS / self.run["run_id"] / "results.json") or []
        with self.lock:
            self.run["returncode"], self.run["ended"] = rc, time.time()
            self.run["result"] = {k: res[0].get(k) for k in ("exp_id", "status", "skip_reason", "problems", "abort_reason",
                                                            "recovery_time_s")} if res else None
            self.state = "finished" if rc == 0 else "failed"
            self.message = None if rc == 0 else f"the run exited with code {rc}: see reports/{self.run['run_id']}/console.log"

    def console_tail(self, n: int = 30) -> list[str]:
        if not self.run:
            return []
        p = REPORTS / self.run["run_id"] / "console.log"
        try:
            return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in p.read_text(errors="replace").splitlines()[-n:]]
        except OSError:
            return []

    # ---------------------------------------------------------------- kill switch
    def kill(self) -> dict:
        (ROOT / "KILL").touch()          # the running safety controller sees it within one tick
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except OSError:
                    pass
            self._traffic_off_locked()
            self.kill_job = {"status": "running", "started": time.time(), "result": None}
            job = self.kill_job
        threading.Thread(target=self._kill_switch, args=(job, "dashboard"), daemon=True, name="kill-switch").start()
        return self.view()

    def reset_kill(self) -> dict:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise ControlError("a run is still stopping; wait for it to exit")
            self.kill_job = {"status": "running", "started": time.time(), "result": None, "reset": True}
            job = self.kill_job
        threading.Thread(target=self._kill_switch, args=(job, "recover"), daemon=True, name="kill-reset").start()
        return self.view()

    def _kill_switch(self, job: dict, reason: str) -> None:
        try:
            from chaosctl.runner import kill_switch
            res = kill_switch(reason=reason)
            if job.get("reset"):
                (ROOT / "KILL").unlink(missing_ok=True)
            job.update(status="done", result={k: res.get(k) for k in ("deleted", "delete_s", "all_ready_s", "recovered",
                                                                       "manual_action_needed")}, ended=time.time())
        except Exception as exc:
            job.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}", ended=time.time())

    # ---------------------------------------------------------------- traffic
    def traffic(self, on: bool) -> dict:
        with self.lock:
            if on:
                if self.proc is not None and self.proc.poll() is None:
                    raise ControlError("a run is going: it brings its own load")
                if (ROOT / "KILL").exists():
                    raise ControlError("kill switch is tripped")
                if self.loadgen is None or not self.loadgen.running:
                    from chaosctl.loadgen import LoadGen
                    self.loadgen = LoadGen(LOADGEN_DB)
                    self.loadgen.start()
            else:
                self._traffic_off_locked()
        return self.view()

    def _traffic_off_locked(self) -> None:
        if self.loadgen is not None and self.loadgen.running:
            self.loadgen.stop()

    # ---------------------------------------------------------------- view
    def view(self) -> dict:
        with self.lock:
            prop = {k: v for k, v in self.proposal.items() if k != "planning_events"} if self.proposal else None
            running = self.proc is not None and self.proc.poll() is None
            return {"available": self.hub.src.mode == "live", "state": self.state, "message": self.message,
                    "proposal": prop, "run": dict(self.run) if self.run else None, "running": running,
                    "console": self.console_tail(14) if self.run else [],
                    "traffic": {"on": bool(self.loadgen and self.loadgen.running),
                                "rate": getattr(self.loadgen, "rate", None) if self.loadgen else None},
                    "kill": dict(self.kill_job) if self.kill_job else None, "kill_file": (ROOT / "KILL").exists(),
                    "blockers": self.blockers()}
