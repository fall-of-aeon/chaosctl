"""Pure derivations for the dashboard: no I/O, so every rule here is unit-tested.

Everything the page shows is computed here from recorded or sampled data: node state,
map layout, workflow strip, timeline, Agent 1 candidates, fate table, phase strip.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from chaosctl.llm import redact as _llm_redact

LAYOUT_PATH = Path(__file__).with_name("layout.yaml")

# ------------------------------------------------------------------ redaction

_EXTRA_SECRETS = [
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(postgres(?:ql)?|redis|https?)://([^:@/\s]+):([^@/\s]+)@"),
    re.compile(r"(?i)\b(N8N_ENCRYPTION_KEY|DB_POSTGRESDB_PASSWORD|PG_PROBE_PASSWORD|[A-Z_]*API_KEY)\b\S*"),
]


def redact(text: str) -> str:
    """BUILD_PLAN 9.1: never show passwords, tokens or the n8n encryption key."""
    text = _llm_redact(str(text))
    text = _EXTRA_SECRETS[0].sub(r"\1 <redacted>", text)
    text = _EXTRA_SECRETS[1].sub(r"\1://\2:<redacted>@", text)
    text = _EXTRA_SECRETS[2].sub(r"\1=<redacted>", text)
    return text


# ------------------------------------------------------------------ node state
#
# One status per node, derived only from measured signals. Order matters:
#   1. Unknown     no pod data for the node (cluster unreachable) and no probe answered.
#   2. Down        no Ready pod, or the node's own probe cannot reach it
#                  (Redis PING/LLEN, Postgres SELECT, mock-api GET /admin).
#   3. Degraded    reachable but at least one bad signal:
#                  - fewer Ready pods than desired replicas,
#                  - a container restarted in the last RECOVER_HOLD_S seconds,
#                  - client-facing node: W1 error rate > ERROR_RATE or W1 p95 > LATENCY_RATIO x baseline,
#                  - queue consumer: jobs waiting > baseline max + QUEUE_GROWTH,
#                  - queue: key count (DBSIZE) collapsed: more than halved, by 10+ keys, within
#                    COLLAPSE_S seconds (state lost; a draining backlog shrinks far slower),
#                  - mock dependency: an error rate is configured.
#   4. Recovering  no bad signal now, but there was one less than RECOVER_HOLD_S seconds ago
#                  (the same hold the runner uses before calling SLIs recovered: policy.yaml recovery_hold_s).
#   5. Healthy     otherwise.

ERROR_RATE = 0.10
LATENCY_RATIO = 2.0
QUEUE_GROWTH = 5
RECOVER_HOLD_S = (yaml.safe_load((Path(__file__).parents[1] / "policy.yaml").read_text()) or {}).get("recovery_hold_s", 30)
COLLAPSE_S = 3
STATES = ("Healthy", "Degraded", "Down", "Recovering", "Unknown")


def node_state(sig: dict, last_bad_ts: float | None, now: float) -> tuple[str, list[str]]:
    """sig keys (all optional): ready, desired, reachable, probe, restarted_recently, error_rate,
    latency_ratio, queue_excess, keys_from, keys_to, injected_error_rate."""
    if sig.get("ready") is None and sig.get("reachable") is None:
        return "Unknown", ["no data"]
    down = []
    if sig.get("ready") == 0:
        down.append("no Ready pod")
    if sig.get("reachable") is False:
        down.append(f"{sig.get('probe', 'probe')} unreachable")
    if down:
        return "Down", down
    bad = []
    ready, desired = sig.get("ready"), sig.get("desired")
    if ready is not None and desired and ready < desired:
        bad.append(f"{ready} of {desired} pods Ready")
    if sig.get("restarted_recently"):
        bad.append("container restarted")
    if (sig.get("error_rate") or 0) > ERROR_RATE:
        bad.append(f"W1 errors {sig['error_rate']:.0%}")
    if (sig.get("latency_ratio") or 0) > LATENCY_RATIO:
        bad.append(f"W1 p95 {sig['latency_ratio']:.1f}x baseline")
    if (sig.get("queue_excess") or 0) > QUEUE_GROWTH:
        bad.append(f"{sig['queue_excess']} more jobs waiting than baseline")
    if sig.get("keys_from") is not None and sig.get("keys_to") is not None:
        bad.append(f"Redis keys fell {sig['keys_from']} -> {sig['keys_to']}")
    if (sig.get("injected_error_rate") or 0) > 0:
        bad.append(f"mock error rate {sig['injected_error_rate']:.0%}")
    if bad:
        return "Degraded", bad
    if last_bad_ts is not None and now - last_bad_ts < RECOVER_HOLD_S:
        return "Recovering", [f"signals normal for {now - last_bad_ts:.0f} s of {RECOVER_HOLD_S} s"]
    return "Healthy", []


def roles(graph: dict) -> dict[str, str]:
    """Which node plays which part, read from the graph (not hardcoded names):
    client_facing = what external clients call; consumer = whoever dequeues; queue/db/mock by kind."""
    nodes = {n["name"]: n for n in graph["nodes"]}
    out: dict[str, str] = {}
    for e in graph["edges"]:
        if nodes.get(e["from"], {}).get("kind") == "external":
            out[e["to"]] = "client_facing"
        if e.get("flow") == "dequeue":
            out[e["from"]] = "consumer"
    for name, n in nodes.items():
        if n.get("kind") in ("queue", "db", "mock") and name not in out:
            out[name] = n["kind"]
    return out


def node_signals(graph: dict, sample: dict, baseline: dict | None, recent: dict) -> dict[str, dict]:
    """Build node_state() inputs for every non-external, non-obs node from one sample.
    recent: {"restarted": {node: bool}, "keys_before": DBSIZE about COLLAPSE_S seconds ago or None}."""
    b = baseline or {}
    b_p95 = (b.get("w1") or {}).get("p95_ms")
    b_wait = (b.get("queue") or {}).get("wait_max") or 0
    role = roles(graph)
    pods = sample.get("pods") or {}
    q = sample.get("redis") or {}
    out = {}
    for n in graph["nodes"]:
        name = n["name"]
        if n.get("kind") in ("external", "obs"):
            continue
        p = pods.get(name)
        sig: dict[str, Any] = {}
        if p is not None and p.get("ready_count") is not None:
            sig["ready"] = p["ready_count"]
            sig["desired"] = n.get("replicas")
            sig["restarted_recently"] = bool((recent.get("restarted") or {}).get(name))
        r = role.get(name)
        if r == "client_facing" and sample.get("load_on"):
            sig["error_rate"] = sample.get("w1_err_rate")
            if sample.get("w1_p95") and b_p95:
                sig["latency_ratio"] = sample["w1_p95"] / b_p95
        elif r == "consumer" and q.get("reachable"):
            sig["queue_excess"] = max(0, q["wait"] - b_wait)
        elif r == "queue":
            sig["probe"] = "Redis probe"
            sig["reachable"] = q.get("reachable") if q else None
            before = recent.get("keys_before")
            if q.get("reachable") and before and before - q["dbsize"] >= 10 and q["dbsize"] < before / 2:
                sig["keys_from"], sig["keys_to"] = before, q["dbsize"]
        elif r == "db":
            pg = sample.get("postgres") or {}
            sig["probe"] = "Postgres probe"
            sig["reachable"] = pg.get("reachable") if pg else None
        elif r == "mock":
            m = sample.get("mock") or {}
            sig["probe"] = "mock-api admin probe"
            sig["reachable"] = m.get("reachable") if m else None
            sig["injected_error_rate"] = m.get("error_rate")
        out[name] = sig
    return out


# ------------------------------------------------------------------ layout

ZONE_BY_KIND = {"external": "clients", "app": "app", "queue": "data", "db": "data", "mock": "external", "obs": "platform"}
NODE_W, NODE_H = 176, 128
ZONE_PAD, COL_GAP, ZONE_GAP = 12, 24, 40
TOP, ROW_STEP = 44, 200
LANE_H = 56


def load_hints(path: Path = LAYOUT_PATH) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def layout(graph: dict, hints: dict | None = None) -> dict:
    """Deterministic left-to-right layout in fixed zones. Same graph -> same coordinates.
    Nodes without a hint go to the zone of their kind, next free row."""
    hints = hints if hints is not None else load_hints()
    zone_defs = hints.get("zones") or [{"id": z, "label": z} for z in ("clients", "app", "data", "external")]
    nh = hints.get("nodes") or {}
    place: dict[str, dict] = {}
    used: dict[str, set] = {}
    lane_nodes = []
    for n in sorted(graph["nodes"], key=lambda n: n["name"]):
        name, h = n["name"], nh.get(n["name"], {})
        zone = h.get("zone") or ZONE_BY_KIND.get(n.get("kind"), "app")
        if zone == "platform":
            lane_nodes.append(name)
            continue
        if zone not in {z["id"] for z in zone_defs}:
            zone_defs = [*zone_defs, {"id": zone, "label": zone}]
        place[name] = {"zone": zone, "row": h.get("row"), "col": h.get("col", 0), "roles": h.get("roles") or []}
    for name, p in place.items():  # hinted rows first, then fill free rows
        if p["row"] is not None:
            used.setdefault(p["zone"], set()).add((p["row"], p["col"]))
    for name, p in place.items():
        if p["row"] is None:
            r = 0
            while (r, p["col"]) in used.setdefault(p["zone"], set()):
                r += 1
            p["row"] = r
            used[p["zone"]].add((r, p["col"]))

    zones, x = [], 0
    for z in zone_defs:
        cols = max([p["col"] for p in place.values() if p["zone"] == z["id"]], default=0) + 1
        w = 2 * ZONE_PAD + cols * NODE_W + (cols - 1) * COL_GAP
        zones.append({"id": z["id"], "label": z["label"], "x": x, "w": w, "cols": cols})
        x += w + ZONE_GAP
    width = x - ZONE_GAP
    zx = {z["id"]: z for z in zones}
    rows = max([p["row"] for p in place.values()], default=0) + 1
    map_h = TOP + rows * ROW_STEP - (ROW_STEP - NODE_H) + 24
    nodes = {}
    for name, p in place.items():
        z = zx[p["zone"]]
        nx = z["x"] + ZONE_PAD + p["col"] * (NODE_W + COL_GAP)
        ny = TOP + p["row"] * ROW_STEP
        nodes[name] = {"x": nx, "y": ny, "w": NODE_W, "h": NODE_H, "zone": p["zone"], "row": p["row"], "roles": p["roles"]}
    columns = sorted({n["x"] for n in nodes.values()})
    for n in nodes.values():
        n["ci"] = columns.index(n["x"])  # column rank, left to right
    for z in zones:
        z.update(y=0, h=map_h)

    lane_y = map_h + 16
    lane = []
    items = [{**it} for it in (hints.get("platform") or [])]
    covered = {it.get("graph_node") for it in items}
    items += [{"id": n, "label": n, "role": "observer", "graph_node": n} for n in lane_nodes if n not in covered]
    slot = width / max(1, len(items))
    for i, it in enumerate(items):
        lane.append({**it, "x": round(i * slot + 8), "y": lane_y + 8, "w": round(slot - 16), "h": LANE_H - 16})
    drawn = [e for e in graph["edges"] if e["from"] in nodes and e["to"] in nodes]
    shapes = {e["id"]: _shape(nodes[e["from"]], nodes[e["to"]]) for e in drawn}
    ports = _ports(drawn, nodes, shapes)
    edges = [_route(e, nodes[e["from"]], nodes[e["to"]], shapes[e["id"]], ports) for e in drawn]
    return {"width": width, "height": lane_y + LANE_H, "map_height": map_h, "zones": zones, "nodes": nodes,
            "edges": edges, "lane": {"y": lane_y, "h": LANE_H, "items": lane}}


# Edge routing, in words: an edge between nodes in the same column is a straight vertical line.
# An edge to the next column is an S-curve from the right side of A to the left side of B.
# An edge that jumps over a column and changes row enters B from the top or bottom, so it runs
# around the node in between instead of through it. Several edges on one side of a node get
# separate ports, ordered by where they end, so they do not cross each other.

def _cy(n: dict) -> float:
    return n["y"] + n["h"] / 2


def _shape(a: dict, b: dict) -> str:
    if a["ci"] == b["ci"]:
        return "vertical"
    if b["ci"] - a["ci"] > 1 and b["row"] != a["row"]:
        return "top" if b["row"] > a["row"] else "bottom"
    return "side"


def _entry_y(b: dict, shape: str) -> float:
    return {"top": b["y"], "bottom": b["y"] + b["h"]}.get(shape, _cy(b))


def _spread(n: dict, keys: list[tuple[float, str]]) -> dict[str, float]:
    keys = sorted(keys)
    step = min(28, (n["h"] - 24) / max(1, len(keys) - 1)) if len(keys) > 1 else 0
    start = _cy(n) - step * (len(keys) - 1) / 2
    return {eid: start + i * step for i, (_, eid) in enumerate(keys)}


def _ports(edges: list[dict], nodes: dict, shapes: dict) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for name, n in nodes.items():
        leaving = [(_entry_y(nodes[e["to"]], shapes[e["id"]]), e["id"]) for e in edges
                   if e["from"] == name and shapes[e["id"]] != "vertical"]
        entering = [(_cy(nodes[e["from"]]), e["id"]) for e in edges
                    if e["to"] == name and shapes[e["id"]] == "side"]
        out.update({("out", k): v for k, v in _spread(n, leaving).items()})
        out.update({("in", k): v for k, v in _spread(n, entering).items()})
    return out


def _route(e: dict, a: dict, b: dict, shape: str, ports: dict) -> dict:
    if shape == "vertical":
        down = b["y"] > a["y"]
        x1 = a["x"] + a["w"] / 2
        y1 = a["y"] + (a["h"] if down else 0)
        y2 = b["y"] + (0 if down else b["h"])
        d = f"M{x1:.0f},{y1:.0f} L{x1:.0f},{y2:.0f}"
        mid = (x1, (y1 + y2) / 2)
    else:
        x1, y1 = a["x"] + a["w"], ports[("out", e["id"])]
        if shape in ("top", "bottom"):
            x2, y2 = b["x"] + b["w"] / 2, _entry_y(b, shape)
            bend = 40 if y2 > y1 else -40
            d = f"M{x1:.0f},{y1:.0f} L{x2 - 40:.0f},{y1:.0f} Q{x2:.0f},{y1:.0f} {x2:.0f},{y1 + bend:.0f} L{x2:.0f},{y2:.0f}"
            mid = ((x1 + x2 - 40) / 2, y1)
        else:
            x2, y2 = b["x"], ports[("in", e["id"])]
            c = (x2 - x1) * 0.5
            d = f"M{x1:.0f},{y1:.0f} C{x1 + c:.0f},{y1:.0f} {x2 - c:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
            mid = ((x1 + x2) / 2, (y1 + y2) / 2)
    label = e.get("flow", "")
    if e.get("logical") and e.get("via"):
        label += f" (via {e['via']})"
    return {"id": e["id"], "from": e["from"], "to": e["to"], "d": d, "mid": [round(mid[0]), round(mid[1])],
            "label": label, "logical": bool(e.get("logical")), "shape": shape}


# ------------------------------------------------------------------ workflows

def workflow_strip(graph: dict, states: dict[str, str], sample: dict, baseline: dict | None,
                   active_target: str | None) -> list[dict]:
    """Plain-language status per workflow. Measured client results first; a broken node after the
    point where the client stops waiting makes an otherwise green workflow 'silent'."""
    b_p95 = ((baseline or {}).get("w1") or {}).get("p95_ms")
    nodes = {n["name"]: n for n in graph["nodes"]}
    out = []
    for wf in graph["workflows"]:
        wid = wf["id"].removeprefix("wf:")
        path = wf["path"]
        until = wf.get("client_visible_until")
        has_client = any(nodes.get(p, {}).get("kind") == "external" for p in path)
        visible = path[: path.index(until) + 1] if until in path else path
        hidden = path[path.index(until) + 1:] if until in path else []
        bad = lambda ps: [p for p in ps if states.get(p) in ("Down", "Degraded")]  # noqa: E731
        err = sample.get(f"{wid.lower()}_err_rate")
        p95 = sample.get(f"{wid.lower()}_p95")
        expected = None
        if active_target and active_target in path and has_client:
            expected = "client_visible" if active_target in visible else "silent"
        if not has_client:
            text, level = ("internal only: affected" if bad(path) else "internal only"), ("warn" if bad(path) else "ok")
        elif not sample.get("load_on"):
            text, level = "no traffic (load generator off)", "none"
        elif err is None:
            text, level = "no completed requests yet", "none"
        elif err > ERROR_RATE:
            text, level = f"clients see errors ({err:.0%})", "fault"
        elif wid == "W1" and p95 and b_p95 and p95 > LATENCY_RATIO * b_p95:
            text, level = f"clients see slow responses (p95 {p95 / 1000:.1f} s)", "warn"
        elif bad(hidden):
            text, level = f"silent: clients still get 200 while {', '.join(bad(hidden))} is impaired", "warn"
        elif bad(visible):
            text, level = f"at risk: {', '.join(bad(visible))} impaired, clients not failing yet", "warn"
        else:
            text, level = "normal", "ok"
        out.append({"id": wf["id"], "name": wid, "mode": "sync" if until == path[-1] else ("async" if has_client else "internal"),
                    "text": text, "level": level, "expected": expected})
    return out


# ------------------------------------------------------------------ timeline

TIMELINE_KEYS = [  # BUILD_PLAN 11.1 names
    ("baseline_stable", "baseline stable"), ("t_inject", "inject"), ("t_first_symptom", "first symptom"),
    ("t_pod_ready", "pod ready"), ("t_slis_recovered", "SLIs recovered"), ("t_queue_drained", "queue drained"),
    ("t_last_inflight_resolved", "last in-flight resolved"),
]


def timeline(exp: dict) -> dict:
    """exp: {t_inject, baseline_stable_ts, live: {key: abs ts}, result_timeline: {key: s after inject},
    events: [{event, ts, ...}]}. Recorded results win over live observations."""
    t0 = exp.get("t_inject")
    res = exp.get("result_timeline") or {}
    live = exp.get("live") or {}
    steps = []
    for key, label in TIMELINE_KEYS:
        ts, source = None, None
        if key == "baseline_stable":
            ts, source = exp.get("baseline_stable_ts"), "journal"
        elif key == "t_inject":
            ts, source = t0, "journal"
        elif t0 is not None and isinstance(res.get(key), (int, float)):
            ts, source = t0 + res[key], "evidence"
        elif live.get(key) is not None:
            ts, source = live[key], "dashboard"
        none_seen = key == "t_first_symptom" and res and res.get("saw_symptom") is False
        steps.append({"key": key, "label": label, "ts": ts, "rel_s": round(ts - t0, 1) if ts and t0 else None,
                      "source": source, "note": "no symptom observed" if none_seen else None})
    marks = []
    for e in exp.get("events") or []:
        if e["event"] in ("abort", "kill_switch", "kill_switch_exec", "watchdog_restart"):
            label = {"abort": "auto-stop", "kill_switch": "kill switch", "kill_switch_exec": "kill switch",
                     "watchdog_restart": "watchdog restart"}[e["event"]]
            marks.append({"label": label, "ts": e["ts"], "rel_s": round(e["ts"] - t0, 1) if t0 else None,
                          "detail": redact(e.get("reason") or e.get("via") or ", ".join(e.get("nodes") or []))})
    return {"steps": steps, "marks": marks, "recovery_time_s": exp.get("recovery_time_s")}


# ------------------------------------------------------------------ Agent 1

def _variant(entry: dict, rung: str) -> str:
    params = {**(entry.get("params") or {}), **((entry.get("ladder") or {}).get(rung) or {})}
    words = []
    for k, v in params.items():
        if k in ("port", "path", "peer"):
            continue
        unit = {"duration": " s", "delay_s": " s", "latency_ms": " ms", "jitter_ms": " ms jitter", "grace_period": " s grace",
                "size_mb": " MB"}.get(k, "")
        name = {"duration": "for", "delay_s": "+", "latency_ms": "", "jitter_ms": "", "grace_period": "",
                "code": "HTTP", "size_mb": "", "offset": "offset", "workers": "workers", "load": "load %"}.get(k, k)
        words.append(f"{name} {v}{unit}".strip() if name not in ("+",) else f"+{v}{unit}")
    if entry.get("params", {}).get("peer"):
        words.insert(0, f"to {entry['params']['peer']}")
    return ", ".join(words)


def candidates_table(scored: list[dict], catalog: dict, history: list[dict], chosen: set[tuple]) -> list[dict]:
    """One row per (target, fault, rung) with the variant spelled out; disabled catalog entries listed as
    'Not enabled'; tier 3 without a passed tier 1 'Blocked by policy'. Allowed first, then by score."""
    passed_t1 = any(catalog["faults"].get(h.get("fault"), {}).get("tier") == 1 and h.get("passed") for h in history)
    tested = {(f"node:{h.get('node')}", h.get("fault")) for h in history if h.get("status") in ("completed", "aborted")}
    rows: dict[tuple, dict] = {}
    for c in scored:
        key = (c["target"], c["fault"], c.get("rung") or "default")
        entry = catalog["faults"].get(c["fault"], {})
        status = "Allowed"
        if entry.get("tier") == 3 and not passed_t1:
            status = "Blocked by policy"
        row = rows.setdefault(key, {
            "target": c["target"], "fault": c["fault"], "rung": key[2], "variant": _variant(entry, key[2]),
            "tier": c["tier"], "score": c["score"], "tested": (c["target"], c["fault"]) in tested or c.get("untested") == 0,
            "status": status, "status_why": "tier 3 needs a passed tier 1 experiment" if status != "Allowed" else "",
            "chosen": key in chosen, "count": 0})
        row["count"] += 1
    for fault, entry in sorted((catalog.get("faults") or {}).items()):
        if entry.get("enabled"):
            continue
        for node in (entry.get("applies_to") or {}).get("nodes", []):
            for rung in sorted(entry.get("ladder") or {"default": {}}):
                key = (f"node:{node}", fault, rung)
                rows.setdefault(key, {"target": key[0], "fault": fault, "rung": rung, "variant": _variant(entry, rung),
                                      "tier": entry.get("tier"), "score": None, "tested": False, "status": "Not enabled",
                                      "status_why": "test only" if entry.get("test_only") else "disabled in catalog",
                                      "chosen": False, "count": 1})
    order = {"Allowed": 0, "Blocked by policy": 1, "Not enabled": 2}
    return sorted(rows.values(), key=lambda r: (order[r["status"]], -(r["score"] or 0), r["target"], r["fault"], r["rung"]))


def provider_of(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    for key, name in (("gemini", "Gemini"), ("ministral", "Mistral"), ("mistral", "Mistral"), ("magistral", "Mistral"),
                      ("gpt", "OpenAI"), ("claude", "Anthropic"), ("llama", "Llama")):
        if key in m:
            return name
    return model


def llm_source(source: str | None, llm_events: list[dict], fallback_text: str = "rule-based ranking") -> dict:
    """Plain-language origin of an agent's output from what llm.py journaled.
    source is "llm", "llm:api:<model>", "llm:cache:<model>" or "fallback:<reason>"."""
    used = [e for e in llm_events if e.get("source") in ("api", "cache")]
    last = used[-1] if used else None
    parts = (source or "").split(":", 2)
    if parts[0] == "llm" and len(parts) == 3:
        last = {"source": parts[1], "model": parts[2]}
    if source and source.startswith("llm") and last:
        kind = "cached" if last["source"] == "cache" else "llm"
        prov = provider_of(last.get("model"))
        text = f"LLM ({prov}, {last.get('model')})" + (", cached answer" if kind == "cached" else "")
        return {"kind": kind, "provider": prov, "model": last.get("model"), "text": text}
    why = (source or "").removeprefix("fallback:") or "unknown"
    errs = [e for e in llm_events if e.get("result") in ("error", "no_api_key", "invalid_json")]
    if errs and errs[-1].get("result") == "no_api_key":
        why = "no API key configured"
    elif "llm_disabled" in why:
        why = "LLM disabled for this run"
    elif errs:
        why = redact(errs[-1].get("err") or why)[:120]
    attempted = sorted({e.get("model") for e in llm_events if e.get("model")})
    return {"kind": "fallback", "provider": provider_of(attempted[-1]) if attempted else None,
            "model": ", ".join(attempted) or None, "text": f"{fallback_text} because the LLM was unavailable",
            "why": why}


# ------------------------------------------------------------------ fates

FATE_MEANING = {
    "completed_once": ("completed once", "client told the right result, work done once", False),
    "lost_silently": ("lost silently", "client got 200, no order was written", True),
    "lost_visible": ("lost, client saw error", "client got an error and no order was written", True),
    "phantom_success": ("phantom success", "client got an error, but the order was written", True),
    "duplicated": ("duplicated", "the order was written more than once", True),
    "success_without_row": ("success without row", "sync client got 200 but no order row exists", True),
}


def fate_rows(summary: dict | None, rows: list[dict] | None = None) -> dict:
    summary = summary or {}
    total = sum(summary.values())
    ids: dict[str, list[str]] = {}
    for r in rows or []:
        ids.setdefault(r["fate"], []).append(f"{r['workflow']} {r['request_id']}")
    out = []
    for key, (label, meaning, bad) in FATE_MEANING.items():
        n = int(summary.get(key, 0))
        out.append({"fate": key, "label": label, "count": n, "share": round(n / total, 3) if total else 0.0,
                    "meaning": meaning, "bad": bad and n > 0, "muted": n == 0, "request_ids": sorted(ids.get(key, []))})
    return {"total": total, "rows": out}


# ------------------------------------------------------------------ phase strip

PHASES = ["Map", "Baseline", "Agent 1 decides", "Inject", "Impact and auto-stop", "Recover", "Agent 2 diagnoses",
          "Report and score"]


def phase_strip(events: list[dict], now: float, have_graph: bool, have_baseline: bool, diagnosed: bool,
                reported: bool = False) -> dict:
    """Progress indicator driven by the run journal. Status per step: done / now / todo."""
    ev = {e["event"] for e in events}
    states = [e.get("to") for e in events if e["event"] == "state"]
    last_state = states[-1] if states else None
    warm = next((e for e in reversed(events) if e["event"] == "warmup"), None)
    warming = bool(warm and now - warm["ts"] < warm.get("seconds", 0))
    done = [have_graph or "graph_loaded" in ev,
            have_baseline or bool(warm and not warming) or "safety_armed" in ev,
            "agent1_plan" in ev,
            "inject" in ev,
            last_state in ("RECOVERY_WAIT", "DONE") and "inject" in ev,
            "experiment_end" in ev and "inject" in ev,
            diagnosed,
            reported]   # the run is in the newest `chaosctl report` (findings.json)
    cur = None
    if warming:
        cur = 1
    elif "agent1_scored" in ev and "agent1_plan" not in ev:
        cur = 2
    elif last_state in ("PREFLIGHT_OK", "WAIT_IN_FLIGHT", "INJECTING"):
        cur = 3
    elif last_state in ("OBSERVING", "ABORTING"):
        cur = 4
    elif last_state == "RECOVERY_WAIT" or (last_state == "DONE" and "experiment_end" not in ev and "inject" in ev):
        cur = 5
    elif "experiment_end" in ev and not diagnosed and any(e["event"] == "llm" and e.get("purpose") == "diagnosis"
                                                          and e["ts"] > _last_ts(events, "experiment_end") for e in events):
        cur = 6
    captions = {1: "steady load before injection", 2: "scoring candidates and asking the LLM",
                3: "waiting for in-flight work, then one fault", 4: "watching impact; safety limits armed",
                5: "fault removed; waiting for Ready pods and request fates", 6: "blind diagnosis from evidence only"}
    steps = []
    for i, name in enumerate(PHASES):
        status = "now" if i == cur else ("done" if done[i] else "todo")
        steps.append({"name": name, "status": status, "caption": captions.get(i, "") if i == cur else ""})
    return {"current": cur, "steps": steps}


def _last_ts(events: list[dict], kind: str) -> float:
    return max((e["ts"] for e in events if e["event"] == kind), default=0)


# ------------------------------------------------------------------ degraded banner

ALERT_LABEL = {"llm_fallback": "LLM NOT USED", "error": "HARNESS ERROR", "warning": "WARNING"}


def alert_banner(items: list[dict]) -> list[dict]:
    """Group alerts.py records (and the dashboard's own problems) for the banner, newest first.
    Nothing degraded is silent: every fallback and error in the last hour is listed."""
    groups: dict[tuple, dict] = {}
    for e in items:
        key = (e.get("kind"), e.get("component"), e.get("reason"))
        g = groups.setdefault(key, {"kind": e.get("kind") or "warning", "label": ALERT_LABEL.get(e.get("kind"), "WARNING"),
                                    "component": e.get("component"), "reason": redact(e.get("reason") or ""),
                                    "count": 0, "last_ts": 0})
        g["count"] += 1
        g["last_ts"] = max(g["last_ts"], e.get("ts") or 0)
    return sorted(groups.values(), key=lambda g: -g["last_ts"])


# ------------------------------------------------------------------ Story tab: words

def fallback_badge(src: dict) -> str:
    """Readable reason a deterministic fallback was used (shown on the page, never hidden)."""
    if src.get("kind") != "fallback":
        return src.get("text", "")
    why = str(src.get("why") or "")
    table = [("MISTRAL_MODEL not set", "Mistral model not configured (MISTRAL_MODEL not set)"),
             ("no API key", "no Mistral API key"), ("MISTRAL_API_KEY", "no Mistral API key"),
             ("429", "Mistral rate limit reached"), ("RESOURCE_EXHAUSTED", "LLM quota exhausted"),
             ("imeout", "Mistral timed out"), ("invalid", "LLM answer failed validation"),
             ("budget", "LLM call budget used up"), ("disabled", "LLM switched off for this run")]
    for key, text in table:
        if key in why:
            return f"Rule-based fallback: {text}"
    return f"Rule-based fallback: {why or 'Mistral unavailable'}"


def tier_rule_text(tier: int | None, passed: dict | None) -> str:
    if tier == 3:
        return ("Tier 3 (outage of a single point of failure) is only allowed after a tier 1 experiment passed"
                + (f": {passed.get('run_id')} {passed.get('fault', '').replace('_', ' ')} passed." if passed else "; none had."))
    if tier == 2:
        return "Tier 2 (degrades a shared dependency): reversible, capped duration."
    return "Tier 1 (one replaceable or client-tolerated component): always allowed first."


def cites_in(text: str) -> list[str]:
    """Evidence ids cited inside Agent 2's text, e.g. (ev:events), ev:logs:redis-6c47..."""
    return sorted(set(re.findall(r"\bev:[\w:.\-]+[\w]", text or "")))


_VERBS = {
    "pod_kill": "killed the {t} pod", "pod_failure": "made the {t} pod fail", "network_delay": "slowed network traffic to {t}",
    "network_partition": "cut {t} off from {peer}", "http_replace_code": "made {t} answer with HTTP errors",
    "http_abort": "made {t} drop requests", "http_delay": "delayed {t}'s answers",
}


def fault_phrase(s: dict) -> str:
    """'killed the redis pod', from the catalog template of the fault."""
    from chaosctl.dash.sources import load_catalog
    entry = load_catalog()["faults"].get(s.get("fault"), {})
    peer = (entry.get("params") or {}).get("peer", "")
    tmpl = entry.get("template") or ""
    if "container" in (s.get("fault") or ""):
        return f"killed a container in {s['target']}"
    if s.get("fault") == "traffic_spike":
        return f"sent a traffic spike to {s['target']}"
    if tmpl in _VERBS:
        return _VERBS[tmpl].format(t=s["target"], peer=peer)
    return f"injected {(s.get('fault') or 'a fault').replace('_', ' ')} into {s['target']}"


def narration(s: dict, diag: dict | None, fix: dict | None, report: dict | None = None) -> dict:
    """One plain sentence per act, built only from recorded numbers (read aloud in the demo)."""
    b = s.get("baseline") or {}
    out = {}
    if b.get("w1_success") is not None:
        out["start"] = (f"The system was healthy: {b['w1_success']:.0%} of sync orders succeeded, p95 "
                        f"{(b.get('w1_p95_ms') or 0) / 1000:.1f} s, about {b.get('wait_mean') or 0:.0f} jobs waiting.")
    else:
        out["start"] = "No baseline was recorded for this run."
    n = (s.get("in_flight") or {}).get("total")
    out["inject"] = f"Agent 1 {fault_phrase(s)}" + (f" while {n} orders were in flight." if n else ".")
    fr = {r["fate"]: r["count"] for r in (s.get("fates") or {}).get("rows", [])}
    total = (s.get("fates") or {}).get("total") or 0
    lost = fr.get("lost_silently", 0) + fr.get("lost_visible", 0)
    if s.get("abort"):
        out["impact"] = f"The safety controller stopped it at T+{s['abort']['rel_s']:.0f} s: {s['abort']['text']}."
    elif s.get("status") == "skipped":
        out["impact"] = "Nothing was injected: the pre-flight checks refused."
    elif s.get("recovery_time_s") is None:
        out["impact"] = "The service did not get back to baseline within the observation window."
    elif total and not lost and not fr.get("duplicated"):
        out["impact"] = f"All {total} in-flight orders completed exactly once; service was back to normal in {s['recovery_time_s']} s."
    else:
        out["impact"] = (f"{lost} of {total} in-flight orders were lost" + (f", {fr['duplicated']} duplicated" if fr.get("duplicated") else "")
                         + f"; service was back to normal in {s['recovery_time_s']} s.")
    if diag:
        rc = (diag.get("root_cause") or "").replace("**", "").replace("`", "").split(". ")[0].rstrip(".")
        out["diagnose"] = f"Agent 2, not told what was injected, concluded: {rc}."
    else:
        out["diagnose"] = "Agent 2 has not diagnosed this run yet."
    if fix and fix.get("fixes"):
        out["fix"] = f"Fix: {str(fix['fixes'][0].get('action') or '').rstrip('. ')}."
    else:
        out["fix"] = "No fix summary yet."
    if report and report.get("available"):
        out["report"] = f"Resilience score {report['overall']['score']} / 100: {report['overall']['verdict']}."
    else:
        out["report"] = "No resilience report yet."
    return out


# Claims a hypothesis can make, each with the measurement that settles it. A claim is only judged when the
# hypothesis text mentions it; a negated claim ("no job is lost") is judged the other way round.
_CLAIMS = [
    ("lost", r"\blost\b|\blose\b", lambda m: m["lost"] > 0, "orders lost", "no orders lost"),
    ("duplicates", r"duplicat|run twice|re-?run", lambda m: m["dup"] > 0, "orders duplicated", "no orders duplicated"),
    ("w1_fail", r"W1 .*(fail|error|hang)|requests? (fail|get errors)", lambda m: m["w1_err"] > 0,
     "sync clients saw errors", "sync clients saw no errors"),
    ("w2_200", r"W2 .*200|keeps? returning 200|still returns? 200", lambda m: m["w2_ok"],
     "async clients still got 200", "async clients did not get 200"),
    ("latency", r"p95 rises|latency rise|slow", lambda m: m["slow"], "latency rose (peak p95 >= 1.5x baseline)",
     "latency stayed normal"),
    ("restart", r"restart", lambda m: m["restarts"] > 0, "a process restarted", "no process restarted"),
]


def judge_hypothesis(text: str | None, s: dict) -> dict:
    """confirmed / refuted / partly / not judged, from the fate table, restarts and peak latency.
    Each claim is negated only by a 'no/not/without/never' in its own clause (text between , ; . and the claim)."""
    if not text or s.get("status") in (None, "running", "skipped"):
        return {"verdict": "not judged", "claims": []}
    fr = {r["fate"]: r["count"] for r in (s.get("fates") or {}).get("rows", [])}
    peak = s.get("peak_p95") or {}
    m = {"lost": fr.get("lost_silently", 0) + fr.get("lost_visible", 0), "dup": fr.get("duplicated", 0),
         "w1_err": fr.get("lost_visible", 0) + fr.get("phantom_success", 0),
         "w2_ok": fr.get("lost_silently", 0) > 0 or (s.get("fates") or {}).get("total", 0) > 0,
         "slow": (peak.get("ratio") or 0) >= 1.5,
         "restarts": sum(v for v in (s.get("restarts_delta") or {}).values() if v and v > 0)}
    claims = []
    for key, pat, test, pos, neg in _CLAIMS:
        hit = re.search(pat, text, re.I)
        if not hit:
            continue
        clause = re.split(r"[,;.]", text[:hit.start()])[-1].lower()
        negated = bool(re.search(r"\b(no|not|without|never)\b", clause))
        hedged = bool(re.search(r"\b(may|might|could|possibly)\b", clause))
        observed = bool(test(m))
        claims.append({"claim": neg if negated else pos, "held": observed != negated, "hedged": hedged})
    if not claims:
        return {"verdict": "not judged", "claims": []}
    firm = [c for c in claims if not c["hedged"]] or claims
    held = sum(c["held"] for c in firm)
    verdict = "confirmed" if held == len(firm) else "refuted" if held == 0 else "partly"
    return {"verdict": verdict, "claims": claims}


def compare_verdict(a: dict, b: dict) -> str:
    """One line, e.g. 'After: recovery 2.4x faster, no backlog, 1 order lost.'"""
    parts = []
    ra, rb = a.get("recovery_time_s"), b.get("recovery_time_s")
    if ra and rb:
        parts.append(f"recovery {ra / rb:.1f}x faster" if rb < ra else f"recovery {rb / ra:.1f}x slower")
    elif rb and not ra:
        parts.append(f"recovered in {rb} s (before: never)")
    elif ra and not rb:
        parts.append(f"did not recover (before: {ra} s)")
    if bool(a.get("abort")) != bool(b.get("abort")):
        parts.append("no auto-stop" if a.get("abort") else "auto-stopped")

    def lost(s):
        fr = {r["fate"]: r["count"] for r in (s.get("fates") or {}).get("rows", [])}
        return fr.get("lost_silently", 0) + fr.get("lost_visible", 0) + fr.get("duplicated", 0)
    lb = lost(b)
    parts.append("no orders lost" if lb == 0 else f"{lb} order{'s' if lb != 1 else ''} lost or duplicated")
    return "After: " + ", ".join(parts) + "."
