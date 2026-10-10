"""Finished runs, read from disk only: reports/<run-id>/ (journal, results, evidence) and the loadgen
SQLite rows inside the run's time window. Everything the Story, Compare and Report tabs show for a
finished run comes from here, so the demo can be replayed without a cluster.

Times in series and logs are seconds relative to injection (T0); wall-clock times are kept for tooltips.
"""
from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from chaosctl.dash import model
from chaosctl.dash import sources

LEAD_S, TAIL_S = 60, 30      # series window: T-60 .. end of observation + 30 s
SEVERITIES = ("error", "warn", "info")


# ------------------------------------------------------------------ catalog / knowledge

@lru_cache(maxsize=1)
def kb() -> dict[str, dict]:
    """fault -> {alias, title} from chaosctl/knowledge (E1, E2 ...); empty if the KB cannot be read."""
    try:
        from chaosctl.fault_kb import load_kb
        k = load_kb()
        return {f: {"alias": e.alias, "title": e.title} for f, e in k.entries.items()}
    except Exception:
        return {}


def catalog() -> dict:
    return sources.load_catalog()


# ------------------------------------------------------------------ run index

def _journal(run_dir: Path) -> list[dict]:
    out = []
    try:
        lines = (run_dir / "journal.jsonl").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _experiments(ev: list[dict]) -> list[tuple[dict, dict | None, list[dict]]]:
    """(experiment_start, experiment_end or None, events of that experiment) in journal order."""
    out = []
    starts = [i for i, e in enumerate(ev) if e["event"] == "experiment_start"]
    for n, i in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(ev)
        chunk = ev[i:stop]
        end = next((e for e in chunk if e["event"] == "experiment_end"), None)
        out.append((ev[i], end, chunk))
    return out


def worker_config(plan_file: dict | None, target: str) -> dict | None:
    """The only configuration the harness records per run: how many worker pods existed at injection."""
    pods = ((plan_file or {}).get("at_inject") or {}).get("target_pods")
    if target == "n8n-worker" and pods:
        return {"worker_replicas": len(pods)}
    return None


def list_runs(reports: Path | None = None) -> list[dict]:
    """One entry per experiment that reached injection or was skipped, newest first."""
    reports = reports or sources.REPORTS
    out = []
    for d in sorted((p for p in reports.glob("r*") if (p / "journal.jsonl").exists()), key=lambda p: p.name, reverse=True):
        ev = _journal(d)
        for start, end, chunk in _experiments(ev):
            plan = start.get("plan") or {}
            exp_id = plan.get("id") or "exp-01"
            fault, target = plan.get("fault"), (plan.get("target") or "").removeprefix("node:")
            evid = sources.read_json(d / "evidence" / f"{exp_id}.json") or {}
            pfile = sources.read_json(d / "evidence" / f"{exp_id}.plan.json")
            inj = next((e for e in chunk if e["event"] == "inject"), None)
            k = kb().get(fault, {})
            status = (end or {}).get("status") or ("skipped" if not inj else "running")
            out.append({"run_id": d.name, "exp_id": exp_id, "ref": f"{d.name}/{exp_id}", "fault": fault,
                        "alias": k.get("alias"), "title": k.get("title") or (fault or "").replace("_", " "),
                        "target": target, "status": status, "started": start["ts"],
                        "t_inject": inj.get("t", inj["ts"]) if inj else None,
                        "recovery_time_s": evid.get("recovery_time_s"), "has_evidence": bool(evid),
                        "diagnosed": (d / "evidence" / f"{exp_id}.diagnosis.json").exists(),
                        "auto_stopped": any(e["event"] == "abort" for e in chunk),
                        "config": worker_config(pfile, target)})
    return out


# ------------------------------------------------------------------ one experiment, everything

class Run:
    """All recorded data of one experiment in one run."""

    def __init__(self, run_id: str, exp_id: str | None = None, reports: Path | None = None, db: Path | None = None):
        reports = reports or sources.REPORTS
        self.dir = reports / run_id
        if not (self.dir / "journal.jsonl").exists():
            raise FileNotFoundError(f"run {run_id} not found under {reports}")
        self.run_id = run_id
        self.db_path = db or sources.LOADGEN_DB
        self.journal = _journal(self.dir)
        exps = _experiments(self.journal)
        if not exps:
            raise ValueError(f"run {run_id} has no experiment")
        pick = [x for x in exps if ((x[0].get("plan") or {}).get("id") or "exp-01") == (exp_id or "")] or [exps[0]]
        self.start, self.end, self.events = pick[0]
        self.plan = self.start.get("plan") or {}
        self.exp_id = self.plan.get("id") or "exp-01"
        self.guard = self.start.get("guard") or {}
        ev_dir = self.dir / "evidence"
        self.evidence = sources.read_json(ev_dir / f"{self.exp_id}.json") or {}
        self.plan_file = sources.read_json(ev_dir / f"{self.exp_id}.plan.json") or {}
        self.diagnosis = sources.read_json(ev_dir / f"{self.exp_id}.diagnosis.json")
        fs = sources.read_json(ev_dir / "fix_summary.json") or {}
        self.fix_summary = fs.get(self.exp_id) if isinstance(fs, dict) else None
        results = sources.read_json(self.dir / "results.json") or []
        self.result = next((r for r in results if r.get("exp_id") == self.exp_id), {})
        self.fault = self.plan.get("fault")
        self.target = (self.plan.get("target") or "").removeprefix("node:")
        self.entry = catalog()["faults"].get(self.fault, {})
        inj = next((e for e in self.events if e["event"] == "inject"), None)
        self.t_inject = (self.evidence.get("timeline") or {}).get("t_inject") or (inj.get("t", inj["ts"]) if inj else None)
        self.inject_event = inj or {}

    # -- small helpers
    def rel(self, ts: float | None) -> float | None:
        return round(ts - self.t_inject, 1) if ts is not None and self.t_inject else None

    def _ev(self, kind: str) -> dict | None:
        return next((e for e in self.events if e["event"] == kind), None)

    def window(self) -> tuple[float, float]:
        """[T-60, last observation + 30 s] in wall-clock seconds."""
        t0 = (self.t_inject or self.start["ts"]) - LEAD_S
        trace = self.evidence.get("trace") or []
        last = max([self.t_inject + s["t"] for s in trace] if trace and self.t_inject else [self.start["ts"]])
        end = (self.end or {}).get("ts") or last
        tl = self.evidence.get("timeline") or {}
        rec = self.t_inject + tl["t_slis_recovered"] if self.t_inject and tl.get("t_slis_recovered") else 0
        return t0, max(last, rec, min(end, last + 120)) + TAIL_S

    # -- summary for the Story tab
    def summary(self) -> dict:
        tl = self.evidence.get("timeline") or self.result.get("timeline") or {}
        fates = self.evidence.get("fates") or self.result.get("fates") or {}
        f_rows = model.fate_rows(fates.get("summary") or (self.end or {}).get("fates"), fates.get("rows"))
        abort = self._ev("abort")
        warm = self._ev("warmup")
        armed = self._ev("safety_armed")
        deleted = self._ev("chaos_delete_all")
        k = kb().get(self.fault, {})
        milestones = []
        for key, label in [("baseline_stable", "baseline stable"), ("t_inject", "inject"), ("t_first_symptom", "first symptom"),
                           ("t_pod_ready", "pod ready"), ("t_queue_drained", "queue drained"),
                           ("t_last_inflight_resolved", "last in-flight resolved"), ("t_slis_recovered", "SLIs recovered")]:
            if key == "baseline_stable":
                rel = self.rel(armed["ts"]) if armed else None
            elif key == "t_inject":
                rel = 0.0 if self.t_inject else None
            else:
                rel = tl.get(key) if isinstance(tl.get(key), (int, float)) else None
            note = "no symptom observed" if key == "t_first_symptom" and tl.get("saw_symptom") is False else None
            milestones.append({"key": key, "label": label, "rel_s": rel, "note": note})
        gap_note = None
        if isinstance(tl.get("t_pod_ready"), (int, float)) and isinstance(tl.get("t_slis_recovered"), (int, float)) \
                and tl["t_slis_recovered"] - tl["t_pod_ready"] > 45:
            gap_note = (f"Pod ready at T+{tl['t_pod_ready']:.0f} s but SLIs only recovered at T+{tl['t_slis_recovered']:.0f} s: "
                        f"the harness needs W1 success and p95 within 10 % of baseline for 30 s in a row. "
                        f"See the log lines between those times.")
        peak = self.peak_p95()
        b = self.evidence.get("baseline") or {}
        in_flight = self.inject_event.get("in_flight") or {k2: len(v) for k2, v in
                                                            ((self.plan_file.get("at_inject") or {}).get("in_flight") or {}).items()}
        summary = {
            "run_id": self.run_id, "exp_id": self.exp_id, "ref": f"{self.run_id}/{self.exp_id}", "fault": self.fault,
            "alias": k.get("alias"), "title": k.get("title") or (self.fault or "").replace("_", " "),
            "target": self.target, "rung": self.plan.get("rung"), "variant": model._variant(self.entry, self.plan.get("rung") or ""),
            "chaos_kind": self.entry.get("chaos_kind"), "tier": self.entry.get("tier"), "reversible": self.entry.get("reversible"),
            "status": (self.end or {}).get("status") or self.result.get("status") or ("running" if not self.end else None),
            "t_inject": self.t_inject, "started": self.start["ts"],
            "pod": self.killed_pod(), "in_flight": {**in_flight, "total": sum(in_flight.values())} if in_flight else None,
            "queue_at_inject": self.inject_event.get("queue"),
            "plan": self.plan, "guard": self.guard, "hypothesis": self.plan.get("hypothesis"),
            "abort": self.abort_info(abort), "milestones": milestones, "gap_note": gap_note,
            "recovery_time_s": self.evidence.get("recovery_time_s", self.result.get("recovery_time_s")),
            "manual_action_needed": self.evidence.get("manual_action_needed"),
            "restarts_delta": self.evidence.get("restarts_delta") or self.result.get("restarts_delta"),
            "fates": f_rows, "baseline": {"w1_success": (b.get("w1") or {}).get("success_rate"), "w1_p95_ms": (b.get("w1") or {}).get("p95_ms"),
                                          "w2_rate": round((b.get("w2") or {}).get("n", 0) / b["duration_s"], 2) if b.get("duration_s") else None,
                                          "wait_mean": (b.get("queue") or {}).get("wait_mean"), "duration_s": b.get("duration_s"),
                                          "rate": b.get("rate")},
            "peak_p95": peak, "phases": self.phases(warm, armed, deleted, tl),
            "markers": self.markers(abort, deleted, tl),
            "limits": self.limits(),
            "config": worker_config(self.plan_file, self.target),
            "edge": self._edge(),
        }
        summary["hypothesis_check"] = model.judge_hypothesis(self.plan.get("hypothesis"), summary)
        summary["narration"] = model.narration(summary, self.diagnosis, self.fix_summary)
        return summary

    def _edge(self) -> str | None:
        peer = (self.entry.get("params") or {}).get("peer")
        return f"edge:{self.target}->{peer}" if peer else None

    def killed_pod(self) -> str | None:
        for e in self.evidence.get("events") or []:
            m = re.search(r"apply chaos for [\w-]+/([\w.-]+)", e.get("message", ""))
            if m:
                return m.group(1)
        inj = self.result.get("injection") or {}
        return (inj.get("pods") or [None])[0] if isinstance(inj.get("pods"), list) else None

    def abort_info(self, abort: dict | None) -> dict | None:
        """'AUTO-STOP at T+97 s: W1 p95 24.0 s > 20 s limit' from the journal's abort reason."""
        if not abort:
            return None
        reason = abort.get("reason") or ""
        text = reason
        m = re.match(r"(\w+) ([\d.]+) > ([\d.]+)", reason)
        if m:
            k, v, lim = m.group(1), float(m.group(2)), float(m.group(3))
            if k == "w1_p95_ms":
                text = f"W1 p95 {v / 1000:.1f} s > {lim / 1000:.0f} s limit"
            elif k == "w1_error_rate":
                text = f"W1 errors {v:.0%} > {lim:.0%} limit"
            elif k == "queue_waiting":
                text = f"jobs waiting {v:.0f} > {lim:.0f} limit"
        elif reason.startswith("max_total_s"):
            m2 = re.search(r"\((\d+)s > (\d+)s\)", reason)
            text = f"time cap: ran {m2.group(1)} s > {m2.group(2)} s limit" if m2 else reason
        elif reason.startswith("blast_radius_breach"):
            text = "a service outside the blast radius went unready: " + reason.split(":", 1)[-1].strip()
        elif reason == "kill_switch":
            text = "kill switch triggered"
        return {"reason": reason, "text": text, "rel_s": self.rel(abort["ts"]), "ts": abort["ts"]}

    def phases(self, warm, armed, deleted, tl) -> list[dict]:
        """Background bands: warm-up, fault active, recovering, recovered (seconds relative to injection)."""
        out = []
        if warm and self.t_inject:
            out.append({"kind": "warmup", "label": "warm-up", "from": self.rel(warm["ts"]),
                        "to": self.rel(min(warm["ts"] + warm.get("seconds", 0), self.t_inject))})
        if not self.t_inject:
            return out
        dur = ((self.entry.get("ladder") or {}).get(self.plan.get("rung")) or {}).get("duration")
        fault_end = tl.get("t_pod_ready") if not self.entry.get("reversible") else None
        if dur:
            fault_end = float(dur)
        if deleted:
            fault_end = min(fault_end, self.rel(deleted["ts"])) if fault_end is not None else self.rel(deleted["ts"])
        fault_end = fault_end if fault_end is not None else (self.rel((self.end or {}).get("ts")) or 0)
        out.append({"kind": "fault", "label": f"fault active: {(self.fault or '').replace('_', ' ')}", "from": 0.0,
                    "to": max(fault_end, 1.0), "fault": self.fault})
        rec = tl.get("t_slis_recovered")
        if isinstance(rec, (int, float)) and rec > fault_end:
            out.append({"kind": "recovering", "label": "recovering", "from": fault_end, "to": rec})
            out.append({"kind": "recovered", "label": "recovered", "from": rec, "to": None})
        elif rec is None:
            out.append({"kind": "recovering", "label": "not recovered in window", "from": fault_end, "to": None})
        return out

    def markers(self, abort, deleted, tl) -> list[dict]:
        out = []
        if self.t_inject:
            out.append({"key": "inject", "label": "\U0001F4A5 inject", "rel_s": 0.0})
        if isinstance(tl.get("t_pod_ready"), (int, float)):
            out.append({"key": "pod_ready", "label": "pod ready", "rel_s": tl["t_pod_ready"]})
        if abort:
            out.append({"key": "abort", "label": "AUTO-STOP", "rel_s": self.rel(abort["ts"])})
        for e in self.events:
            if e["event"] in ("kill_switch", "kill_switch_exec"):
                out.append({"key": "kill_switch", "label": "kill switch", "rel_s": self.rel(e["ts"])})
                break
        if deleted:
            out.append({"key": "chaos_removed", "label": "chaos removed", "rel_s": self.rel(deleted["ts"])})
        if isinstance(tl.get("t_slis_recovered"), (int, float)):
            out.append({"key": "slis_recovered", "label": "SLIs recovered", "rel_s": tl["t_slis_recovered"]})
        if self.end:
            out.append({"key": "end", "label": "end", "rel_s": self.rel(self.end["ts"])})
        return sorted(out, key=lambda m: (m["rel_s"] is None, m["rel_s"] or 0))

    def limits(self) -> dict:
        """Abort limits the guard armed for this run; error/latency limits start after the grace period."""
        a = (self.guard.get("abort") or self.result.get("thresholds") or {})
        grace = a.get("error_rate_armed_after_s", 0) or 0
        return {"w1_p95_ms": a.get("w1_p95_ms"), "w1_error_rate": a.get("w1_error_rate") if a.get("error_rate_abort", True) else None,
                "queue_waiting": a.get("queue_waiting"), "max_total_s": a.get("max_total_s"), "armed_after_s": grace}

    # -- per-second series relative to injection
    def series(self) -> dict:
        t0, t1 = self.window()
        base = self.t_inject or self.start["ts"]
        rows = self._load_rows(t0 - 15, t1)
        trace = sorted(self.evidence.get("trace") or [], key=lambda s: s["t"])
        by_sec = {}
        for s in trace:
            by_sec.setdefault(round(s["t"]), s)
        out = {k: [] for k in ("t", "w1_p50", "w1_p95", "w1_ok", "w1_err", "w2_ok", "w2_err", "q_wait", "q_active", "dbsize")}
        gaps = []
        trace_lo = trace[0]["t"] if trace else None
        trace_hi = trace[-1]["t"] if trace else None
        gap_start = None
        sec = int(t0 - base)
        while base + sec <= t1:
            t = base + sec
            out["t"].append(sec)
            for wf in ("W1", "W2"):
                done = [r for r in rows if r[1] == wf and r[3] is not None and t - 5 <= r[3] < t]
                ok = [r for r in done if r[4] is not None and 200 <= r[4] < 300]
                k = wf.lower()
                out[f"{k}_ok"].append(round(len(ok) / 5, 2) if rows else None)
                out[f"{k}_err"].append(round((len(done) - len(ok)) / 5, 2) if rows else None)
            lat = sorted(r[5] for r in rows if r[1] == "W1" and r[3] is not None and t - 10 <= r[3] < t
                         and r[4] is not None and 200 <= r[4] < 300 and r[5] is not None)
            out["w1_p95"].append(lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None)
            out["w1_p50"].append(lat[len(lat) // 2] if lat else None)
            s = by_sec.get(sec)
            q = (s or {}).get("queue") or {}
            reach = q.get("reachable")
            for col, key in (("q_wait", "wait"), ("q_active", "active"), ("dbsize", "dbsize")):
                out[col].append(q.get(key) if reach else None)
            if trace and trace_lo <= sec <= trace_hi and s is not None and reach is False:
                gap_start = sec if gap_start is None else gap_start
            elif gap_start is not None:
                gaps.append({"from": gap_start, "to": sec, "reason": "Redis probe unreachable"})
                gap_start = None
            sec += 1
        if gap_start is not None:
            gaps.append({"from": gap_start, "to": out["t"][-1], "reason": "Redis probe unreachable"})
        if trace:
            if out["t"] and trace_lo - out["t"][0] > 2:
                gaps.append({"from": out["t"][0], "to": round(trace_lo), "reason": "queue not recorded before the safety sampler started",
                             "kind": "not_recorded", "series": ["q_wait", "q_active", "dbsize"]})
            if out["t"] and out["t"][-1] - trace_hi > 2:
                gaps.append({"from": round(trace_hi), "to": out["t"][-1], "reason": "queue not recorded after observation ended",
                             "kind": "not_recorded", "series": ["q_wait", "q_active", "dbsize"]})
        return {"run_id": self.run_id, "exp_id": self.exp_id, "t_inject": self.t_inject, "loadgen_rows": len(rows),
                "cols": out, "gaps": gaps, "pods": self.swimlane(trace), "wall_t0": base}

    def _load_rows(self, t0: float, t1: float) -> list[tuple]:
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            return conn.execute("SELECT request_id, workflow, sent_ts, finished_ts, status, latency_ms, client_error "
                                "FROM requests WHERE sent_ts BETWEEN ? AND ? OR finished_ts BETWEEN ? AND ?",
                                (t0 - 90, t1, t0, t1)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def swimlane(self, trace: list[dict]) -> list[dict]:
        """Readiness over time, one row per service (one per pod for the target), from the recorded trace
        and Kubernetes events. States: Ready, NotReady, Terminating, Pending, CrashLoopBackOff; ↻ = restart."""
        if not trace or not self.t_inject:
            return []
        events = sorted(self.evidence.get("events") or [], key=lambda e: e["ts"])
        nodes = list((trace[0].get("pods") or {}).keys())
        t_end = trace[-1]["t"]
        rows = []
        for node in nodes:
            pts = [(s["t"], (s.get("pods") or {}).get(node, {}).get("ready")) for s in trace]
            node_ev = [e for e in events if (e.get("object") or "").startswith(node)]
            marks = [{"t": self.rel(e["ts"]), "kind": "restart", "label": f"↻ {e['object']}"}
                     for e in node_ev if e["reason"] == "Started" and e["ts"] >= self.t_inject - 1]
            backoff = [self.rel(e["ts"]) for e in node_ev if e["reason"] == "BackOff"]
            if node == self.target and (self.plan_file.get("at_inject") or {}).get("target_pods"):
                rows += self._target_rows(node, events, pts, t_end)
                continue
            segs = _segments([(t, "Ready" if r else ("NotReady" if r is False else None)) for t, r in pts], t_end)
            for b in backoff:  # a back-off turns the not-ready stretch it falls in into CrashLoopBackOff
                for s in segs:
                    if s["state"] == "NotReady" and s["from"] <= b <= s["to"]:
                        s["state"] = "CrashLoopBackOff"
            rows.append({"row": node, "node": node, "segments": segs, "marks": marks})
        return rows

    def _target_rows(self, node: str, events: list[dict], pts: list, t_end: float) -> list[dict]:
        names = list((self.plan_file.get("at_inject") or {}).get("target_pods") or [])
        killed = self.killed_pod()
        created = [(self.rel(e["ts"]), e["message"].split("Created pod: ")[-1]) for e in events
                   if e["reason"] == "SuccessfulCreate" and e["object"].startswith(node) and e["ts"] >= self.t_inject - 1]
        ready_at = (self.evidence.get("timeline") or {}).get("t_pod_ready")
        rows = []
        for n in names:
            if n == killed:
                kill_t = next((self.rel(e["ts"]) for e in events if e["reason"] == "Killing" and e["object"] == n), 0.0)
                segs = [{"from": pts[0][0], "to": kill_t, "state": "Ready"}, {"from": kill_t, "to": kill_t + 1, "state": "Terminating"}]
            else:
                segs = [{"from": pts[0][0], "to": t_end, "state": "Ready"}]
            rows.append({"row": n, "node": node, "segments": segs, "marks": []})
        for t, n in created:
            if n in names:
                continue
            ok = ready_at if isinstance(ready_at, (int, float)) else t_end
            rows.append({"row": n, "node": node, "segments": [{"from": t, "to": ok, "state": "Pending"},
                                                              {"from": ok, "to": t_end, "state": "Ready"}],
                         "marks": [{"t": t, "kind": "created", "label": f"created {n}"}]})
        return rows

    # -- peak latency
    def peak_p95(self) -> dict | None:
        """Highest W1 p95 after injection, from the client-side records (loadgen), else from the safety trace."""
        if not self.t_inject:
            return None
        b = ((self.evidence.get("baseline") or {}).get("w1") or {}).get("p95_ms")
        cols = self.series()["cols"]
        after = [(v, t) for t, v in zip(cols["t"], cols["w1_p95"]) if t >= 0 and v is not None]
        if after:
            v, t = max(after)
            return {"ms": v, "rel_s": t, "ratio": round(v / b, 1) if b else None, "source": "loadgen"}
        s = self.evidence.get("trace") or []
        vals = [((x.get("w1") or {}).get("p95_ms"), x["t"]) for x in s if (x.get("w1") or {}).get("p95_ms")]
        if not vals:
            return None
        v, t = max(vals)
        return {"ms": v, "rel_s": t, "ratio": round(v / b, 1) if b else None, "source": "trace"}

    # -- unified log
    def logs(self) -> list[dict]:
        """Kubernetes events, harness journal, safety controller, loadgen request errors and the pod log lines
        captured in the evidence bundle, merged. Every row has T+ (None if the line carries no timestamp)."""
        from chaosctl.dash.hub import SAFETY_EVENTS, journal_message
        out = []
        for e in self.evidence.get("events") or []:
            out.append(self._row(e["ts"], "warn" if e.get("type") == "Warning" else "info", "k8s", _component(e.get("object")),
                                 f"{e.get('reason')} {e.get('object')}: {e.get('message')}", evidence="ev:events"))
        for e in self.events:
            msg = journal_message(e)
            if msg is None and e["event"] != "degraded":
                continue
            if e["event"] == "degraded":
                msg = f"{model.ALERT_LABEL.get(e.get('kind'), 'WARNING')}: {e.get('component')}: {e.get('reason')}"
            src = "safety" if e["event"] in SAFETY_EVENTS else "harness"
            sev = "error" if e["event"] in ("abort", "kill_switch", "kill_switch_exec", "watchdog_restart") else \
                "warn" if e["event"] == "degraded" else "info"
            out.append(self._row(e["ts"], sev, src, "safety" if src == "safety" else "harness", msg))
        t0, t1 = self.window()
        for r in self._load_rows(t0, t1):
            rid, wf, sent, fin, status, lat, err = r
            if err or (status is not None and status >= 400):
                if fin is None or not (t0 <= fin <= t1):
                    continue
                out.append(self._row(fin, "error", "loadgen", "client",
                                     f"{wf} request failed: {err or 'http_' + str(status)} after {(lat or 0) / 1000:.1f} s",
                                     request_id=rid))
        for pod, lines in (self.evidence.get("logs") or {}).items():
            for line in lines:
                ts = _parse_log_ts(line)
                sev = "error" if re.search(r"(?i)\b(error|fatal|panic|fail)", line) else \
                    "warn" if re.search(r"(?i)\bwarn", line) else "info"
                out.append(self._row(ts, sev, "pod log", _component(pod), f"{pod}: {line}", evidence=f"ev:logs:{pod}"))
        out.sort(key=lambda r: (r["ts"] is None, r["ts"] or 0))
        for i, r in enumerate(out):
            r["id"] = i
        return out

    def _row(self, ts, severity, source, component, message, evidence=None, request_id=None) -> dict:
        return {"ts": ts, "rel_s": self.rel(ts) if ts else None, "severity": severity, "source": source,
                "component": component, "message": model.redact(message), "evidence": evidence, "request_id": request_id}


def _segments(points: list[tuple[float, str | None]], t_end: float) -> list[dict]:
    segs: list[dict] = []
    for t, st in points:
        if st is None:
            continue
        if segs and segs[-1]["state"] == st:
            segs[-1]["to"] = t
        else:
            if segs:
                segs[-1]["to"] = t
            segs.append({"from": t, "to": t, "state": st})
    if segs:
        segs[-1]["to"] = t_end
    return segs


NODES = ("n8n-main", "n8n-worker", "redis", "postgres", "mock-api")


def _component(obj: str | None) -> str:
    for n in NODES:
        if obj and obj.startswith(n):
            return n
    return "chaos" if obj and obj.startswith("chaosctl-") else (obj or "-")


_TS_PATTERNS = [
    (re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*(UTC|Z)?"), "iso"),
    (re.compile(r"(\d{1,2} \w{3} \d{4}) (\d{2}:\d{2}:\d{2}(?:\.\d+)?)"), "redis"),
]


def _parse_log_ts(line: str) -> float | None:
    """Postgres '2026-10-10 10:09:52.488 UTC', ISO with Z, Redis '10 Oct 2026 11:09:40.926' (UTC in our pods)."""
    from datetime import datetime, timezone
    for pat, kind in _TS_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if kind == "iso" else "%d %b %Y %H:%M:%S"
            clock, frac = (m.group(2).split(".") + ["0"])[:2]
            dt = datetime.strptime(f"{m.group(1)} {clock}", fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp() + float("0." + frac)
        except ValueError:
            continue
    return None


def filter_logs(rows: list[dict], severity=None, source=None, component=None, t_from=None, t_to=None, q=None,
                evidence=None, page: int = 0, page_size: int = 200) -> dict:
    """Newest first, paginated, with counts per filter value over the unfiltered set."""
    counts = {"severity": {}, "source": {}, "component": {}}
    for r in rows:
        for k in counts:
            counts[k][r[k]] = counts[k].get(r[k], 0) + 1
    sel = rows
    if severity:
        sel = [r for r in sel if r["severity"] in severity.split(",")]
    if source:
        sel = [r for r in sel if r["source"] in source.split(",")]
    if component:
        sel = [r for r in sel if r["component"] in component.split(",")]
    if t_from is not None:
        sel = [r for r in sel if r["rel_s"] is not None and r["rel_s"] >= t_from]
    if t_to is not None:
        sel = [r for r in sel if r["rel_s"] is not None and r["rel_s"] <= t_to]
    if evidence:
        sel = [r for r in sel if r["evidence"] == evidence or (evidence == "ev:fates" and r["request_id"])]
    if q:
        ql = q.lower()
        sel = [r for r in sel if ql in r["message"].lower()]
    sel = sorted(sel, key=lambda r: (r["ts"] is not None, r["ts"] or 0), reverse=True)
    return {"total": len(sel), "page": page, "page_size": page_size, "counts": counts,
            "rows": sel[page * page_size:(page + 1) * page_size], "summary": error_summary(rows)}


def error_summary(rows: list[dict]) -> dict:
    """Errors per 10 s bucket (T+ axis) and the top 3 error messages grouped by pattern."""
    errs = [r for r in rows if r["severity"] == "error"]
    buckets: dict[int, int] = {}
    for r in errs:
        if r["rel_s"] is not None:
            b = int(r["rel_s"] // 10) * 10
            buckets[b] = buckets.get(b, 0) + 1
    groups: dict[str, int] = {}
    for r in errs:
        msg = r["message"].split(": ", 1)[-1] if r["source"] == "pod log" else r["message"]
        pat = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<id>", msg)
        pat = re.sub(r"\b[\w-]+-[0-9a-f]{8,10}-[a-z0-9]{5}\b", "<pod>", pat)
        pat = re.sub(r"\d+(\.\d+)?", "#", pat)[:120]
        groups[pat] = groups.get(pat, 0) + 1
    top = sorted(groups.items(), key=lambda kv: -kv[1])[:3]
    return {"errors_per_10s": sorted(buckets.items()), "top": [{"pattern": p, "count": c} for p, c in top],
            "errors": len(errs)}


# ------------------------------------------------------------------ Agent 1 (per run)

def agent1(run: Run) -> dict:
    ev = run.journal
    plan_ev = [e for e in ev if e["event"] == "agent1_plan" and e["ts"] <= run.start["ts"] + 1]
    if not plan_ev:
        return {"available": False}
    plan_ev = plan_ev[-1]
    scored = [e for e in ev if e["event"] == "agent1_scored" and e["ts"] <= plan_ev["ts"]]
    t_scored = scored[-1]["ts"] if scored else 0
    llm_ev = [e for e in ev if e["event"] == "llm" and e.get("purpose", "injection_select") == "injection_select"
              and t_scored <= e["ts"] <= plan_ev["ts"]]
    deg = [e for e in ev if e["event"] == "degraded" and t_scored <= e["ts"] <= plan_ev["ts"] + 1]
    history = sources.read_json(sources.STATE / "history.json") or []
    hist_before = [h for h in history if h.get("ts", 0) < run.start["ts"]]
    picked = {(run.plan.get("target"), run.plan.get("fault"), run.plan.get("rung"))}
    rows = model.candidates_table(scored[-1]["candidates"] if scored else [], catalog(), hist_before, picked)
    from chaosctl.agents.injection import WEIGHTS
    for r in rows:   # the four score terms, so the formula is visible as a split bar
        c = next((c for c in (scored[-1]["candidates"] if scored else []) if (c["target"], c["fault"], c.get("rung")) ==
                  (r["target"], r["fault"], r["rung"])), None)
        r["terms"] = {k: round(WEIGHTS[k] * c.get(k, 0) * (-1 if k == "tier_risk" else 1), 3) for k in WEIGHTS} if c else None
        r["alias"] = kb().get(r["fault"], {}).get("alias")
    passed = next((h for h in hist_before if catalog()["faults"].get(h.get("fault"), {}).get("tier") == 1 and h.get("passed")), None)
    src = model.llm_source(plan_ev.get("source"), llm_ev)
    if deg and src["kind"] == "fallback":
        src["why"] = deg[-1].get("reason") or src.get("why")
    src["badge"] = model.fallback_badge(src)
    return {"available": True, "source": src, "chosen": run.plan, "alias": kb().get(run.fault, {}).get("alias"),
            "variant": model._variant(run.entry, run.plan.get("rung") or ""), "verdict": run.guard,
            "tier_rule": model.tier_rule_text(run.entry.get("tier"), passed),
            "guard_notes": [e.get("note") for e in ev if e["event"] == "guard_note" and t_scored <= e["ts"] <= plan_ev["ts"] + 1],
            "candidates": rows, "weights": WEIGHTS}


# ------------------------------------------------------------------ diagnosis (per run)

def diagnosis(run: Run) -> dict:
    d = run.diagnosis
    if not d:
        return {"available": False}
    from chaosctl.agents.diagnosis import PLAYBOOK, grade
    t_end = (run.end or {}).get("ts") or 0
    llm_ev = [e for e in run.journal if e["event"] == "llm" and e.get("purpose") == "diagnosis" and e["ts"] >= t_end - 1]
    src = model.llm_source(d.get("source"), llm_ev, "detector-only template diagnosis")
    src["badge"] = model.fallback_badge(src)
    det = {h["id"]: h for h in d.get("detectors") or []}
    sev_rank = ["Critical", "High", "Medium", "Low"]
    fixes = []
    for f in d.get("fixes") or []:
        pb = PLAYBOOK.get(f["playbook_id"], {})
        sevs = [det[a]["severity"] for a in pb.get("addresses", []) if a in det]
        fixes.append({"id": f["playbook_id"], "title": pb.get("title", f["playbook_id"]), "why": model.redact(f.get("why", "")),
                      "severity": min(sevs, key=sev_rank.index) if sevs else None, "effort": pb.get("effort"),
                      "impact": pb.get("impact"), "runbook": {k: pb.get(k) for k in ("change", "verify", "rollback", "source") if pb.get(k)}})
    g = grade(d, run.plan_file, run.evidence or None) if run.plan_file else None
    return {"available": True, "source": src,
            "thoughts": [{"step": t["step"], "text": model.redact(t["text"]), "cites": model.cites_in(t["text"])}
                         for t in d.get("thoughts") or []],
            "root_cause": model.redact(d.get("root_cause", "")),
            "failure_chain": [model.redact(s) for s in d.get("failure_chain") or []],
            "findings": [{"title": model.redact(f.get("title", "")), "evidence": f.get("evidence") or []} for f in d.get("findings") or []],
            "detectors": d.get("detectors") or [], "fixes": fixes, "grade": g}


# ------------------------------------------------------------------ report + stretch goals + compare

def report(reports: Path | None = None, run_id: str | None = None) -> dict:
    reports = reports or sources.REPORTS
    r = sources.find_resilience(reports, run_id) if run_id else sources.find_resilience(reports)
    if not r:
        return {"available": False, "command": "uv run chaosctl report --all",
                "produces": "findings.md and findings.json: ranked weaknesses, playbook fixes and a 0-100 resilience score"}
    full = sources.read_json(reports.parent / r["path"]) or {}
    return {"available": True, **r, "weaknesses": full.get("weaknesses") or [], "fixes": full.get("fixes") or [],
            "experiments": full.get("experiments") or [], "md_path": r["path"].replace(".json", ".md")}


def stretch_goals(reports: Path | None = None, live_dashboard: bool = True) -> dict:
    """Statuses computed from recorded runs, never hard-coded."""
    runs = [r for r in list_runs(reports) if r["has_evidence"]]
    # 1. re-run after a fix: same fault, different recorded configuration
    pair = None
    by_fault: dict[str, list[dict]] = {}
    for r in runs:
        if r["config"]:
            by_fault.setdefault(r["fault"], []).append(r)
    for fault, rs in by_fault.items():
        rs = sorted(rs, key=lambda r: r["started"])
        for a in rs:
            b = next((b for b in rs if b["started"] > a["started"] and b["config"] != a["config"]
                      and b["recovery_time_s"] is not None), None)
            if b:
                pair = {"a": a["ref"], "b": b["ref"], "fault": fault, "alias": a["alias"],
                        "change": ", ".join(f"{k.replace('_', ' ')} {a['config'][k]} -> {b['config'][k]}" for k in a["config"]
                                            if a["config"].get(k) != b["config"].get(k))}
                break
        if pair:
            break
    redis_kills = [r for r in runs if r["fault"] == "redis_pod_kill"]
    g1 = {"id": "rerun_after_fix", "title": "Re-run experiments after applying a fix",
          "status": "Done" if pair else ("Partial" if len({r['fault'] for r in runs}) < len(runs) else "Planned"),
          "pair": pair, "pending": "E2 (redis pod kill) before and after k8s/redis-hardened.yaml" +
          (f" ({len(redis_kills)} runs so far, all without persistence)" if redis_kills else "")}
    g2 = {"id": "live_dashboard", "title": "Live dashboard or simple UI", "status": "Done" if live_dashboard else "Partial",
          "shows": ["live metrics: latency, throughput, errors, queue (1 s)", "Kubernetes events and pod readiness",
                    "agent decisions: Agent 1 choice, guard limits, Agent 2 reasoning", "findings, fix summary and resilience score"]}
    combined = []
    for d in sorted((p for p in (reports or sources.REPORTS).glob("r*") if (p / "journal.jsonl").exists())):
        for start, end, chunk in _experiments(_journal(d)):
            n = sum(1 for e in chunk if e["event"] in ("chaos_create", "inject"))
            creates = sum(1 for e in chunk if e["event"] == "chaos_create")
            if creates > 1:
                combined.append(f"{d.name}/{(start.get('plan') or {}).get('id')}")
    g3 = {"id": "combined_failures", "title": "Combined failures", "status": "Done" if combined else "Planned",
          "runs": combined, "scenario": "E3 load spike (2-3x load) during the E1 worker kill",
          "needs": "the non-Chaos-Mesh executor described in README Part 5 for the load spike"}
    return {"goals": [g1, g2, g3]}


def compare(a: str, b: str, reports: Path | None = None, db: Path | None = None) -> dict:
    ra, rb = (Run(*_split(x), reports=reports, db=db) for x in (a, b))
    sa, sb = ra.summary(), rb.summary()

    def fates(s):
        return {r["fate"]: r["count"] for r in s["fates"]["rows"]}

    def lost(s):
        f = fates(s)
        return f.get("lost_silently", 0) + f.get("lost_visible", 0) + f.get("duplicated", 0)

    deltas = [
        {"metric": "recovery time", "a": sa["recovery_time_s"], "b": sb["recovery_time_s"], "unit": "s", "better": "lower"},
        {"metric": "peak W1 p95", "a": (sa["peak_p95"] or {}).get("ms"), "b": (sb["peak_p95"] or {}).get("ms"), "unit": "ms", "better": "lower"},
        {"metric": "lost or duplicated orders", "a": lost(sa), "b": lost(sb), "unit": "", "better": "lower"},
        {"metric": "auto-stop", "a": bool(sa["abort"]), "b": bool(sb["abort"]), "unit": "", "better": "lower"},
    ]
    return {"a": {"summary": sa, "series": ra.series()}, "b": {"summary": sb, "series": rb.series()},
            "deltas": deltas, "verdict": model.compare_verdict(sa, sb)}


def _split(ref: str) -> tuple[str, str | None]:
    run, _, exp = ref.partition("/")
    return run, exp or None
