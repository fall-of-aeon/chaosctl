"""Executes one experiment end-to-end under the safety controller (BUILD_PLAN 10, 11).

Agents never call this with raw specs: it takes a validated plan + guard verdict and
creates chaos only via the catalog-backed ChaosMesh client.
"""
from __future__ import annotations

import json
import signal
import statistics
import threading
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from chaosctl import safety
from chaosctl.chaos.chaosmesh import ChaosMesh, load_catalog
from chaosctl.journal import Journal
from chaosctl.kube import NAMESPACE
from chaosctl.loadgen import LoadGen, LoadStats
from chaosctl.probes import WORKLOAD_SELECTORS, K8sProbe, PgProbe, RedisProbe, fate_table
from chaosctl.safety import SafetyController, State, Thresholds

ROOT = Path(__file__).parents[1]
POLICY_PATH = Path(__file__).with_name("policy.yaml")
OVERLAY_PATH = Path(__file__).with_name("overlay.yaml")
BASELINE_PATH = ROOT / "state" / "baseline.json"
LOADGEN_DB = ROOT / "state" / "loadgen.sqlite"


def load_policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text())


def workloads() -> dict:
    return yaml.safe_load(OVERLAY_PATH.read_text())["workloads"]


def _apps_api():
    from kubernetes import client

    from chaosctl.kube import api_client
    return client.AppsV1Api(api_client())


def new_run_id() -> str:
    return time.strftime("r%m%d-%H%M%S")


# ------------------------------------------------------------ kill switch

def kill_switch(reason: str, journal: Journal | None = None, watchdog_deadline_s: float = 120) -> dict:
    """Usable from any process: delete all managed chaos, then make sure every workload is Ready."""
    active = safety.load_active_run() or {}
    journal = journal or Journal(ROOT / "reports" / active.get("run_id", "adhoc") / "journal.jsonl")
    journal.write({"event": "kill_switch_exec", "reason": reason, "active_run": active.get("run_id")})
    t0 = time.time()
    chaos = ChaosMesh(journal=journal)
    deleted = chaos.delete_all()
    t_deleted = time.time()
    k8s = K8sProbe()
    apps = _apps_api()
    wl = workloads()
    result = safety.recovery_watchdog(
        k8s.pods, sorted(WORKLOAD_SELECTORS), watchdog_deadline_s,
        lambda n: safety.rollout_restart(apps, n, wl[n]), journal)
    out = {"reason": reason, "deleted": deleted, "delete_s": round(t_deleted - t0, 2),
           "all_ready_s": round(time.time() - t0, 2), **{k: v for k, v in result.items() if k != "ready_at"}}
    journal.write({"event": "kill_switch_done", **out})
    if not result["recovered"]:
        safety.LOCK_FILE.write_text(json.dumps({"reason": reason, "ts": time.time(), "detail": out}))
    return out


# ------------------------------------------------------------ runner

class Runner:
    def __init__(self, run_id: str | None = None, loadgen: LoadGen | None = None):
        self.run_id = run_id or new_run_id()
        self.dir = ROOT / "reports" / self.run_id
        (self.dir / "evidence").mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.dir / "journal.jsonl")
        self.policy = load_policy()
        self.catalog = load_catalog()
        self.chaos = ChaosMesh(catalog=self.catalog, journal=self.journal)
        self.k8s = K8sProbe()
        self.redis = RedisProbe()
        self.pg = PgProbe()
        self.loadgen = loadgen or LoadGen(LOADGEN_DB)
        self._local = threading.local()
        self.baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else None
        self.ctl: SafetyController | None = None

    @property
    def stats(self) -> LoadStats:
        """One SQLite connection per thread (the safety thread samples concurrently)."""
        if not hasattr(self._local, "stats"):
            self._local.stats = LoadStats(LOADGEN_DB)
        return self._local.stats

    # -- sampling (used by the safety thread and by observe)
    def sample(self, window_s: float = 5.0) -> dict:
        t = time.time()
        return {"ts": t, "w1": self.stats.window("W1", t - window_s, t), "w2": self.stats.window("W2", t - window_s, t),
                "queue": self.redis.sample(), "pods": {n: {"ready": v["ready"]} for n, v in self.k8s.pods().items()},
                "w1_pending": len(self.stats.pending("W1"))}

    def _on_abort(self, reason: str) -> None:
        self.loadgen.stop(timeout=0.1)
        self.chaos.delete_all(self.run_id, wait_s=10)

    def start_safety(self) -> SafetyController:
        self.ctl = SafetyController(self.sample, self._on_abort, self.journal, kill_file=ROOT / "KILL")
        self.ctl.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda s, f: self.ctl.kill(f"signal:{signal.Signals(s).name}"))
        return self.ctl

    # -- pre-flight
    def preflight(self, target: str, fault: str) -> list[str]:
        problems = []
        if safety.is_locked():
            problems.append("LOCKED: run `chaosctl recover --ack` after checking the cluster")
        if (ROOT / "KILL").exists():
            problems.append("./KILL sentinel present")
        pods = self.k8s.pods()
        for n, v in pods.items():
            if not v["ready"]:
                problems.append(f"workload {n} not Ready")
        try:
            from kubernetes import client

            from chaosctl.kube import api_client
            daemons = client.CoreV1Api(api_client()).list_namespaced_pod(
                "chaos-mesh", label_selector="app.kubernetes.io/component=chaos-daemon").items
            if not daemons or not all(all(s.ready for s in (p.status.container_statuses or [])) for p in daemons):
                problems.append("chaos-daemon not Ready")
        except Exception as exc:
            problems.append(f"cannot check chaos-daemon: {type(exc).__name__}")
        if self.chaos.list_managed():
            problems.append("existing chaosctl-managed chaos resources")
        if fault not in self.catalog["faults"] or target not in self.catalog["targets"]:
            problems.append("fault/target not in catalog")
        if not self.redis.sample().get("reachable"):
            problems.append("redis probe unreachable")
        if not self.pg.sample().get("reachable"):
            problems.append("postgres probe unreachable")
        if not self.baseline:
            problems.append("no baseline: run `chaosctl baseline` first")
        if self.ctl is not None and not self.ctl.healthy():
            problems.append("safety controller unhealthy")
        self.journal.write({"event": "preflight", "target": target, "fault": fault, "problems": problems})
        return problems

    def baseline_stable(self, seconds: float = 30) -> tuple[bool, dict]:
        t = time.time()
        w1 = self.stats.window("W1", t - seconds, t)
        ok = w1["n"] >= 5 and (w1["error_rate"] or 0) <= 0.05
        return ok, w1

    # -- in-flight gate
    def in_flight_ids(self, lookback_s: float = 60) -> dict[str, list[str]]:
        w1 = self.stats.pending("W1")
        w2_pending = self.stats.pending("W2")
        accepted = self.stats.accepted_since("W2", time.time() - lookback_s)
        counts = self.pg.row_counts(accepted) or {}
        w2 = sorted(set(w2_pending) | {r for r in accepted if counts.get(r, 1) == 0})
        return {"W1": sorted(w1), "W2": w2}

    def wait_in_flight(self, min_in_flight: int, timeout_s: float) -> dict | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if self.ctl and self.ctl.aborted:
                return None
            ids = self.in_flight_ids()
            q = self.redis.sample()
            if len(ids["W1"]) >= min_in_flight and q.get("active", 0) >= 1:
                return {"ids": ids, "queue": q}
            time.sleep(0.25)
        return None

    # -- one experiment
    def run_experiment(self, plan: dict, guard: dict) -> dict:
        exp_id, fault, target, rung = plan["id"], plan["fault"], plan["target"].removeprefix("node:"), plan["rung"]
        entry = self.catalog["faults"][fault]
        ctl = self.ctl or self.start_safety()
        res: dict = {"exp_id": exp_id, "fault": fault, "target": target, "rung": rung, "run_id": self.run_id}
        self.journal.write({"event": "experiment_start", "plan": plan, "guard": guard})
        safety.save_active_run({"run_id": self.run_id, "exp_id": exp_id, "fault": fault, "target": target})

        if ctl.state in (State.DONE,):
            ctl.state = State.IDLE
        ctl.transition(State.PLANNED)

        problems = self.preflight(target, fault)
        if guard.get("verdict") != "approve":
            problems.append(f"guard verdict {guard.get('verdict')}")
        if problems:
            ctl.transition(State.DONE)
            return {**res, "status": "skipped", "skip_reason": "preflight", "problems": problems}
        ctl.transition(State.PREFLIGHT_OK)

        warmup = self.policy.get("warmup_s", 60)  # BUILD_PLAN 11.1: steady load >= 60 s before injection
        if not self.loadgen.running:
            self.loadgen.start()
            self.journal.write({"event": "warmup", "seconds": warmup})
            t0 = time.time()
            while time.time() - t0 < warmup and not ctl.aborted:
                time.sleep(1)
        ok, base_w1 = self.baseline_stable()
        if not ok:
            ctl.transition(State.DONE)
            return {**res, "status": "skipped", "skip_reason": "baseline_unstable", "w1": base_w1}

        a = guard["abort"]
        th = Thresholds(**{k: a[k] for k in ("w1_error_rate", "w1_p95_ms", "queue_waiting", "max_total_s",
                                             "recovery_deadline_s") if k in a},
                        error_rate_armed_after_s=a.get("error_rate_armed_after_s", 0),
                        error_rate_abort=a.get("error_rate_abort", True),
                        allowed_unready=sorted({target, *[n.removeprefix("node:") for n in guard["blast_radius"]]}))
        ctl.arm(th)

        gate = self.policy["rules"]["require_in_flight_before_inject"]
        ctl.transition(State.WAIT_IN_FLIGHT)
        got = self.wait_in_flight(gate["min_in_flight"], gate["wait_timeout_s"])
        if got is None:
            ctl.transition(State.DONE)
            status = "aborted" if ctl.aborted else "skipped"
            return {**res, "status": status, "skip_reason": ctl.abort_reason or "no_in_flight"}

        pods_before = self.k8s.pods()
        target_uids = [p["uid"] for p in pods_before[target]["pods"]]
        restarts_before = {n: sum(p["restarts"] for p in v["pods"]) for n, v in pods_before.items()}
        inflight = got["ids"]
        res["at_inject"] = {"in_flight": inflight, "queue": got["queue"],
                            "target_pods": [p["name"] for p in pods_before[target]["pods"]], "target_uids": target_uids}

        ctl.transition(State.INJECTING)
        t_inject = ctl.t_inject
        cr = self.chaos.create(fault, target, rung, self.run_id, allow_test_only=bool(entry.get("test_only")))
        res["chaos_name"] = cr["metadata"]["name"]
        self.journal.write({"event": "inject", "t": t_inject, "in_flight": {k: len(v) for k, v in inflight.items()},
                            "queue": got["queue"]})
        ctl.transition(State.OBSERVING)

        timeline = self._observe(ctl, target, target_uids, t_inject, th, entry)

        # recovery
        if ctl.state == State.OBSERVING:
            ctl.transition(State.RECOVERY_WAIT)
        else:
            ctl.state = State.RECOVERY_WAIT
        self.chaos.delete_all(self.run_id)
        apps, wl = _apps_api(), workloads()
        wd = safety.recovery_watchdog(self.k8s.pods, sorted(WORKLOAD_SELECTORS), th.recovery_deadline_s,
                                      lambda n: safety.rollout_restart(apps, n, wl[n]), self.journal)
        if not wd["recovered"]:
            ctl.transition(State.ROLLBACK_FAILED)
            ctl.transition(State.LOCKED)
            safety.LOCK_FILE.write_text(json.dumps({"run_id": self.run_id, "exp_id": exp_id, "ts": time.time()}))

        if ctl.aborted and not self.loadgen.running and not (ROOT / "KILL").exists() and ctl.abort_reason != "kill_switch":
            pass  # load stays off after an abort; the campaign decides whether to continue

        # fates: wait for stragglers (stalled-job redelivery can take ~60-90 s)
        fate_wait = self.policy.get("fate_wait_s", 180)
        t_fate_end = t_inject + fate_wait
        all_ids = inflight["W1"] + inflight["W2"]
        while time.time() < t_fate_end and not (ROOT / "KILL").exists():
            reqs = self.stats.requests(all_ids)
            counts = self.pg.row_counts(all_ids) or {}
            if all(r.get("finished_ts") for r in reqs.values()) and all(counts.get(i, 0) >= 1 for i in all_ids):
                break
            time.sleep(2)
        reqs = self.stats.requests(all_ids)
        counts = self.pg.row_counts(all_ids) or {}
        fates = fate_table(reqs, counts, t_inject)
        finished = [r["finished_ts"] for r in reqs.values() if r.get("finished_ts")]
        timeline["t_last_inflight_resolved"] = max(finished) if finished else None

        pods_after = self.k8s.pods()
        restarts_after = {n: sum(p["restarts"] for p in v["pods"]) for n, v in pods_after.items()}
        status = "aborted" if ctl.aborted else "completed"
        res.update({
            "status": status, "abort_reason": ctl.abort_reason, "thresholds": asdict(th),
            "timeline": {k: (round(v - t_inject, 1) if isinstance(v, float) and k != "t_inject" else v)
                         for k, v in timeline.items()},
            "recovery_time_s": round(timeline["t_slis_recovered"] - t_inject, 1) if timeline.get("t_slis_recovered") else None,
            "watchdog": {k: v for k, v in wd.items() if k != "ready_at"},
            "manual_action_needed": wd["manual_action_needed"],
            "restarts_delta": {n: restarts_after.get(n, 0) - restarts_before.get(n, 0) for n in restarts_after},
            "fates": fates,
        })
        self._write_evidence(res, ctl.samples, t_inject)
        ctl.transition(State.DONE)
        ctl.abort_reason = None if status == "completed" else ctl.abort_reason
        safety.clear_active_run()
        self.journal.write({"event": "experiment_end", "exp_id": exp_id, "status": status,
                            "fates": fates["summary"], "recovery_time_s": res["recovery_time_s"]})
        return res

    def _observe(self, ctl: SafetyController, target: str, old_uids: list[str], t_inject: float,
                 th: Thresholds, entry: dict) -> dict:
        b = self.baseline or {}
        b_p95 = b.get("w1", {}).get("p95_ms") or 5000
        b_sr = b.get("w1", {}).get("success_rate") or 1.0
        b_wait = b.get("queue", {}).get("wait_max", 0)
        tl: dict = {"t_inject": t_inject, "t_first_symptom": None, "t_pod_ready": None,
                    "t_slis_recovered": None, "t_queue_drained": None}
        healthy_since = None
        saw_symptom = False
        # stop observing before the safety cap so max_total_s only ever fires on a runaway run
        while time.time() - t_inject < th.max_total_s - 5:
            if ctl.aborted:
                break
            now = time.time()
            s = self.sample(window_s=10)
            q = s["queue"]
            w1 = s["w1"]
            pods = self.k8s.pods()
            target_ready_new = any(p["ready"] and not p["deleting"] and p["uid"] not in old_uids
                                   for p in pods[target]["pods"])
            if entry.get("reversible"):
                target_ready_new = pods[target]["ready"]
            symptom = ((w1["error_rate"] or 0) > 0 or (w1["p95_ms"] or 0) > 1.5 * b_p95
                       or not q.get("reachable") or q.get("wait", 0) > b_wait + 5
                       or any(not v["ready"] for v in pods.values()))
            self._live(now - t_inject, w1, s["w2"], q, pods, target)
            if symptom and tl["t_first_symptom"] is None:
                tl["t_first_symptom"] = now
            saw_symptom = saw_symptom or symptom
            if target_ready_new and tl["t_pod_ready"] is None:
                tl["t_pod_ready"] = now
            if tl["t_pod_ready"] and q.get("reachable") and q.get("wait", 0) <= b_wait + 5 and tl["t_queue_drained"] is None:
                tl["t_queue_drained"] = now
            sli_ok = (tl["t_pod_ready"] is not None and w1["n"] >= 3 and (w1["success_rate"] or 0) >= b_sr - 0.1
                      and (w1["p95_ms"] or 0) <= 1.1 * b_p95 and q.get("reachable") and q.get("wait", 0) <= b_wait + 5)
            if sli_ok:
                healthy_since = healthy_since or now
                if now - healthy_since >= 30:
                    tl["t_slis_recovered"] = healthy_since
                    break
            else:
                healthy_since = None
            time.sleep(1)
        tl["saw_symptom"] = saw_symptom
        return tl

    def _live(self, t: float, w1: dict, w2: dict, q: dict, pods: dict, target: str) -> None:
        """One line per second so the impact is visible while it happens."""
        from rich.console import Console
        con = getattr(self, "_con", None) or Console(highlight=False)
        self._con = con
        err = w1["n"] - w1["ok"]
        p95 = f"{w1['p95_ms'] / 1000:.1f}s" if w1["p95_ms"] else "  -  "
        qtxt = f"wait {q['wait']:>3} active {q['active']:>2}" if q.get("reachable") else "[red]REDIS DOWN       [/red]"
        pod_txt = " ".join(f"[{'green' if v['ready'] else 'red'}]{n}[/]" for n, v in pods.items())
        con.print(f"t+{t:5.1f}s  W1 ok {w1['ok']:>2} [{'red' if err else 'green'}]err {err:>2}[/] p95 {p95:>6} | "
                  f"W2 ok {w2['ok']:>2} | {qtxt} | {pod_txt}")

    def _write_evidence(self, res: dict, samples: list[dict], t_inject: float) -> None:
        """Evidence bundle for the blind diagnosis agent: no plan/chaos spec inside."""
        logs = {}
        for node, v in self.k8s.pods().items():
            for p in v["pods"]:
                lines = [l for l in self.k8s.logs(p["name"], 200) if any(w in l.lower() for w in ("error", "warn", "fail", "redis", "lost", "stalled"))]
                logs[p["name"]] = lines[-50:]
        from chaosctl.llm import redact
        logs = {k: [redact(l) for l in v] for k, v in logs.items()}
        bundle = {
            "exp_id": res["exp_id"], "baseline": self.baseline,
            "trace": [{"t": round(s["ts"] - t_inject, 1), "w1": s.get("w1"), "w2": s.get("w2"), "queue": s.get("queue"),
                       "pods": s.get("pods"), "w1_pending": s.get("w1_pending")} for s in samples if s["ts"] >= t_inject - 30],
            "events": self.k8s.events_since(t_inject - 30), "logs": logs,
            "fates": res["fates"], "timeline": res["timeline"], "restarts_delta": res["restarts_delta"],
            "recovery_time_s": res["recovery_time_s"], "manual_action_needed": res["manual_action_needed"],
            "status": res["status"],
        }
        (self.dir / "evidence" / f"{res['exp_id']}.json").write_text(json.dumps(bundle, indent=2, default=str))
        (self.dir / "evidence" / f"{res['exp_id']}.plan.json").write_text(json.dumps(
            {k: res.get(k) for k in ("fault", "target", "rung", "chaos_name", "at_inject", "thresholds", "abort_reason")},
            indent=2, default=str))


# ------------------------------------------------------------ baseline

def capture_baseline(duration_s: float = 120, rate: float | None = None) -> dict:
    lg = LoadGen(LOADGEN_DB, rate=rate)
    stats, redis_p = LoadStats(LOADGEN_DB), RedisProbe()
    lg.start()
    t0 = time.time()
    waits, actives = [], []
    try:
        while time.time() - t0 < duration_s:
            q = redis_p.sample()
            if q.get("reachable"):
                waits.append(q["wait"]); actives.append(q["active"])
            time.sleep(1)
    finally:
        lg.stop()
    t1 = time.time()
    start = t0 + min(20, duration_s / 4)  # skip warm-up
    w1, w2 = stats.window("W1", start, t1), stats.window("W2", start, t1)
    half = len(waits) // 2
    trend = (statistics.mean(waits[half:]) - statistics.mean(waits[:half])) if half else 0
    out = {"captured_at": t1, "duration_s": duration_s, "rate": lg.rate, "w1": w1, "w2": w2,
           "queue": {"wait_mean": round(statistics.mean(waits), 2) if waits else None,
                     "wait_max": max(waits) if waits else None,
                     "wait_trend": round(trend, 2),
                     "active_mean": round(statistics.mean(actives), 2) if actives else None}}
    BASELINE_PATH.parent.mkdir(exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(out, indent=2))
    return out
