"""Dependency graph: k8s discovery + overlay + derived metrics (BUILD_PLAN section 7).

Edges point from dependent to dependency: A -> B means "A depends on B", so
blast_radius(X) is every node that can reach X. All output is sorted so the same
input always produces the same graph.
"""
from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
OVERLAY_PATH = Path(__file__).with_name("overlay.yaml")

NODE_DEFAULTS: dict[str, Any] = {
    "kind": "app",
    "workload": None,
    "replicas": 1,
    "stateful": False,
    "has_pvc": False,
    "has_readiness_probe": False,
    "has_resource_limits": False,
    "has_pdb": False,
    "injectable": True,
}


def node_id(name: str) -> str:
    return f"node:{name}"


def edge_id(a: str, b: str) -> str:
    return f"edge:{a}->{b}"


# ---------------------------------------------------------------- discovery

def _labels_match(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _hosts_in(value: str) -> set[str]:
    """Hostname-like tokens in an env value: 'redis', 'postgres:5432', 'http://mock-api:8080/x'."""
    value = re.sub(r"^[a-z]+://", "", value.strip())
    return {tok.split(":")[0] for tok in re.split(r"[/,\s]+", value) if tok}


def _env_values(containers: list[dict], configmaps: dict[str, dict]) -> list[str]:
    """Literal env values plus values pulled in from ConfigMaps (configMapKeyRef / envFrom)."""
    vals = []
    for c in containers:
        for e in c.get("env") or []:
            if "value" in e:
                vals.append(str(e["value"]))
            ref = (e.get("valueFrom") or {}).get("configMapKeyRef")
            if ref and ref.get("key") in configmaps.get(ref.get("name"), {}):
                vals.append(str(configmaps[ref["name"]][ref["key"]]))
        for ef in c.get("envFrom") or []:
            ref = ef.get("configMapRef")
            if ref:
                vals.extend(str(v) for v in configmaps.get(ref.get("name"), {}).values())
    return vals


def discover(manifests: list[dict], overlay: dict) -> tuple[dict, list[dict]]:
    """Return (node attrs by graph name, candidate edges) from raw k8s object dicts."""
    ns = overlay.get("namespace")
    by_workload = {(w["kind"], w["name"]): name for name, w in overlay.get("workloads", {}).items()}

    workloads, services, pdbs, configmaps = [], [], [], {}
    for obj in manifests:
        if ns and obj.get("metadata", {}).get("namespace", ns) != ns:
            continue
        kind = obj.get("kind")
        if kind in ("Deployment", "StatefulSet"):
            workloads.append(obj)
        elif kind == "Service":
            services.append(obj)
        elif kind == "PodDisruptionBudget":
            pdbs.append(obj)
        elif kind == "ConfigMap":
            configmaps[obj["metadata"]["name"]] = obj.get("data") or {}

    nodes: dict[str, dict] = {}
    pod_labels: dict[str, dict] = {}
    env_values: dict[str, list[str]] = {}
    for w in workloads:
        key = (w["kind"], w["metadata"]["name"])
        name = by_workload.get(key, w["metadata"]["name"])
        spec = w.get("spec", {})
        tmpl = spec.get("template", {})
        containers = tmpl.get("spec", {}).get("containers", [])
        volumes = tmpl.get("spec", {}).get("volumes", []) or []
        labels = tmpl.get("metadata", {}).get("labels", {})
        pod_labels[name] = labels
        nodes[name] = {
            "workload": w["kind"],
            "replicas": spec.get("replicas", 1),
            "has_pvc": bool(spec.get("volumeClaimTemplates")) or any("persistentVolumeClaim" in v for v in volumes),
            "has_readiness_probe": all("readinessProbe" in c for c in containers) and bool(containers),
            "has_resource_limits": all(c.get("resources", {}).get("limits") for c in containers) and bool(containers),
            "has_pdb": any(_labels_match(p.get("spec", {}).get("selector", {}).get("matchLabels", {}), labels) for p in pdbs),
            "source": "k8s",
        }
        env_values[name] = _env_values(containers, configmaps)

    svc_to_node: dict[str, str] = {}
    for s in services:
        sel = s.get("spec", {}).get("selector", {})
        for name, labels in pod_labels.items():
            if _labels_match(sel, labels):
                svc_to_node[s["metadata"]["name"]] = name

    edges = []
    for name, values in env_values.items():
        targets = {svc_to_node[h] for v in values for h in _hosts_in(v) if h in svc_to_node}
        for t in sorted(targets - {name}):
            edges.append({"from": name, "to": t, "source": "k8s_env"})
    return nodes, edges


def live_manifests(namespace: str, context: str | None = None) -> list[dict]:
    """Fetch Deployments/StatefulSets/Services/PDBs from the cluster as plain dicts."""
    from kubernetes import client, config

    config.load_kube_config(context=context)
    api = client.ApiClient()
    apps, core, policy = client.AppsV1Api(api), client.CoreV1Api(api), client.PolicyV1Api(api)
    out = []
    for kind, items in (
        ("Deployment", apps.list_namespaced_deployment(namespace).items),
        ("StatefulSet", apps.list_namespaced_stateful_set(namespace).items),
        ("Service", core.list_namespaced_service(namespace).items),
        ("PodDisruptionBudget", policy.list_namespaced_pod_disruption_budget(namespace).items),
        ("ConfigMap", core.list_namespaced_config_map(namespace).items),
    ):
        for it in items:
            d = api.sanitize_for_serialization(it)
            d["kind"] = kind
            out.append(d)
    return out


# ------------------------------------------------------------------- graph

class Graph:
    def __init__(self, nodes: dict[str, dict], edges: list[dict], workflows: dict[str, dict]):
        self.nodes = {k: nodes[k] for k in sorted(nodes)}
        self.edges = sorted(edges, key=lambda e: (e["from"], e["to"]))
        self.workflows = {k: workflows[k] for k in sorted(workflows)}
        self._compute()

    # --- construction
    @classmethod
    def build(cls, manifests: list[dict] | None = None, overlay: dict | None = None,
              observed: dict[str, dict] | None = None) -> "Graph":
        overlay = overlay if overlay is not None else yaml.safe_load(OVERLAY_PATH.read_text())
        disc_nodes, disc_edges = discover(manifests or [], overlay)

        nodes: dict[str, dict] = {}
        for name, attrs in disc_nodes.items():
            nodes[name] = {**NODE_DEFAULTS, **attrs}
        for name, attrs in (overlay.get("nodes") or {}).items():
            base = nodes.get(name, {**NODE_DEFAULTS, "source": "overlay"})
            merged = {**base, **attrs}
            merged["source"] = "k8s+overlay" if name in disc_nodes else "overlay"
            if merged["kind"] in ("external", "obs"):
                merged["replicas"] = None
            nodes[name] = merged
        for name, attrs in (observed or {}).items():
            if name in nodes:
                nodes[name].update(attrs)

        edges: dict[tuple[str, str], dict] = {}
        for e in disc_edges:
            edges[(e["from"], e["to"])] = dict(e)
        for e in overlay.get("edges") or []:
            key = (e["from"], e["to"])
            src = "k8s_env+overlay" if key in edges else "overlay"
            edges[key] = {**edges.get(key, {}), **e, "source": src}
        for (a, b), e in edges.items():
            e["id"] = edge_id(a, b)
            for n in (a, b):
                nodes.setdefault(n, {**NODE_DEFAULTS, "source": "edge"})

        return cls(nodes, list(edges.values()), overlay.get("workflows") or {})

    # --- derived metrics
    def dependents(self, x: str) -> list[str]:
        rev: dict[str, list[str]] = {}
        for e in self.edges:
            rev.setdefault(e["to"], []).append(e["from"])
        seen, q = set(), deque([x])
        while q:
            for p in rev.get(q.popleft(), []):
                if p not in seen and p != x:
                    seen.add(p)
                    q.append(p)
        return sorted(seen)

    def blast_radius(self, x: str) -> dict:
        deps = self.dependents(x)
        return {"nodes": [d for d in deps if d != "client"], "client_impact": "client" in deps}

    def workflows_affected(self, x: str) -> dict[str, str]:
        out = {}
        for wid, wf in self.workflows.items():
            path = wf["path"]
            if x not in path:
                continue
            until = wf.get("client_visible_until")
            if until is None or "client" not in path:
                out[wid] = "internal"
            else:
                out[wid] = "client_visible" if path.index(x) <= path.index(until) else "silent"
        return out

    def _compute(self) -> None:
        n_wf = len(self.workflows) or 1
        dep_counts = {n: len(self.blast_radius(n)["nodes"]) for n in self.nodes}
        max_dep = max(dep_counts.values() or [1]) or 1
        for name, a in self.nodes.items():
            on_paths = sorted(w for w, wf in self.workflows.items() if name in wf["path"])
            coverage = len(on_paths) / n_wf
            spof = a.get("replicas") == 1 and bool(on_paths) and a.get("kind") not in ("external", "obs")
            a["on_paths"] = on_paths
            a["path_coverage"] = round(coverage, 3)
            a["spof"] = spof
            a["fan_in"] = sum(1 for e in self.edges if e["to"] == name)
            a["fan_out"] = sum(1 for e in self.edges if e["from"] == name)
            gaps = []
            if spof:
                if not a.get("has_pdb"):
                    gaps.append("no_pdb")
                if not a.get("has_readiness_probe"):
                    gaps.append("no_readiness_probe")
                if a.get("stateful") and not a.get("has_pvc"):
                    gaps.append("stateful_without_pvc")
            a["redundancy_gap"] = gaps
            a["criticality"] = round(0.5 * coverage + 0.3 * spof + 0.2 * dep_counts[name] / max_dep, 3)

    def ranked(self) -> list[str]:
        return sorted(self.nodes, key=lambda n: (-self.nodes[n]["criticality"], n))

    def has_id(self, ref: str) -> bool:
        if ref.startswith("node:"):
            return ref[5:] in self.nodes
        if ref.startswith("edge:"):
            return any(e["id"] == ref for e in self.edges)
        if ref.startswith("wf:"):
            return ref[3:] in self.workflows
        return False

    # --- outputs
    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "nodes": [{"id": node_id(n), "name": n, **a,
                       "blast_radius": self.blast_radius(n),
                       "workflows_affected": self.workflows_affected(n)} for n, a in self.nodes.items()],
            "edges": self.edges,
            "workflows": [{"id": f"wf:{w}", **wf} for w, wf in self.workflows.items()],
            "ranking": self.ranked(),
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")

    def mermaid(self) -> str:
        lines = ["graph LR"]
        for n, a in self.nodes.items():
            if a["kind"] == "obs":
                continue
            label = f"{n}<br/>{a['kind']}" + (" SPOF" if a["spof"] else "")
            lines.append(f'  {n.replace("-", "_")}["{label}"]')
        for e in self.edges:
            arrow = "-.->" if e.get("mode") == "async" else "-->"
            lines.append(f'  {e["from"].replace("-", "_")} {arrow}|{e.get("flow", "")}| {e["to"].replace("-", "_")}')
        for n, a in self.nodes.items():
            if a["spof"]:
                lines.append(f"  style {n.replace('-', '_')} stroke:#c0392b,stroke-width:3px")
        return "\n".join(lines) + "\n"

    def summary(self, top: int = 3) -> str:
        """Compact, stable text for LLM prompts (<= ~60 lines)."""
        lines = ["NODES (id | kind | replicas | spof | criticality | flags):"]
        for n in self.ranked():
            a = self.nodes[n]
            if a["kind"] == "obs":
                continue
            flags = ",".join(f for f in ("stateful", "has_pvc", "has_readiness_probe", "has_pdb") if a.get(f)) or "-"
            inj = "" if a.get("injectable", True) else " not-injectable"
            lines.append(f"  {node_id(n)} | {a['kind']} | {a['replicas']} | {a['spof']} | {a['criticality']} | {flags}{inj}")
        lines.append("EDGES (A depends on B):")
        for e in self.edges:
            extra = f" timeout={e['known_timeout_s']}s" if e.get("known_timeout_s") else ""
            lines.append(f"  {e['id']} | {e.get('flow', '?')} | {e.get('mode', '?')}{extra}")
        lines.append("WORKFLOWS:")
        for w, wf in self.workflows.items():
            lines.append(f"  wf:{w}: {' > '.join(wf['path'])} (client waits until: {wf.get('client_visible_until')})")
        lines.append("TOP SINGLE POINTS OF FAILURE:")
        for n in [n for n in self.ranked() if self.nodes[n]["spof"]][:top]:
            br = self.blast_radius(n)
            lines.append(f"  {node_id(n)}: blast_radius={br['nodes']} client_impact={br['client_impact']} "
                         f"workflows={self.workflows_affected(n)}")
        return "\n".join(lines)

    def candidates(self, catalog: dict) -> list[tuple[str, str, str]]:
        """Every valid (node, fault, rung) for enabled catalog entries."""
        out = []
        for fault, entry in sorted((catalog.get("faults") or {}).items()):
            if not entry.get("enabled"):
                continue
            applies = entry.get("applies_to", {})
            for n, a in self.nodes.items():
                if not a.get("injectable", True):
                    continue
                if n not in applies.get("nodes", []) and a["kind"] not in applies.get("kinds", []):
                    continue
                for rung in sorted(entry.get("ladder", {"default": {}})):
                    out.append((n, fault, rung))
        return out
