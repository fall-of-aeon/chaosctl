from pathlib import Path

import yaml

from chaosctl.graph import Graph, discover

FIX = Path(__file__).parent / "fixtures" / "manifests"
OVERLAY = yaml.safe_load((Path(__file__).parents[1] / "chaosctl" / "overlay.yaml").read_text())


def manifests():
    docs = []
    for f in sorted(FIX.glob("*.yaml")):
        docs += [d for d in yaml.safe_load_all(f.read_text()) if d]
    return docs


def build():
    return Graph.build(manifests(), OVERLAY)


def test_discovery_finds_env_edges():
    _, edges = discover(manifests(), OVERLAY)
    pairs = {(e["from"], e["to"]) for e in edges}
    for app in ("n8n-main", "n8n-worker"):
        assert (app, "redis") in pairs
        assert (app, "postgres") in pairs


def test_discovery_reads_workload_attrs():
    nodes, _ = discover(manifests(), OVERLAY)
    assert nodes["n8n-worker"]["replicas"] == 1
    assert nodes["postgres"]["has_pvc"] is True
    assert nodes["redis"]["has_pvc"] is False
    assert nodes["n8n-main"]["has_pdb"] is False  # baseline disables the chart PDB


def test_overlay_wins_and_merge_is_deterministic():
    g1, g2 = build(), build()
    assert g1.to_dict() == g2.to_dict()
    e = next(e for e in g1.edges if e["id"] == "edge:n8n-main->redis")
    assert e["source"] == "k8s_env+overlay" and e["flow"] == "enqueue" and e["known_timeout_s"] == 10
    custom = {**OVERLAY, "edges": OVERLAY["edges"] + [{"from": "n8n-main", "to": "redis", "flow": "override"}]}
    g3 = Graph.build(manifests(), custom)
    assert next(e for e in g3.edges if e["id"] == "edge:n8n-main->redis")["flow"] == "override"


def test_blast_radius_and_workflows():
    g = build()
    assert g.blast_radius("redis") == {"nodes": ["n8n-main", "n8n-worker"], "client_impact": True}
    assert set(g.workflows_affected("redis")) == {"W1", "W2", "W3"}
    assert g.blast_radius("n8n-worker") == {"nodes": ["n8n-main"], "client_impact": True}
    assert g.workflows_affected("n8n-worker") == {"W1": "client_visible", "W2": "silent", "W3": "internal"}
    assert {"n8n-main", "n8n-worker"} <= set(g.blast_radius("postgres")["nodes"])


def test_spof_and_criticality_ranking():
    g = build()
    for n in ("n8n-main", "n8n-worker", "redis", "postgres", "mock-api"):
        assert g.nodes[n]["spof"] is True
    assert g.nodes["client"]["spof"] is False
    top2 = set(g.ranked()[:2])
    assert top2 == {"redis", "postgres"}


def test_ids_and_summary_stable():
    g = build()
    assert g.has_id("node:redis") and g.has_id("edge:n8n-worker->redis") and g.has_id("wf:W2")
    assert not g.has_id("node:kafka") and not g.has_id("edge:redis->n8n-main")
    s = g.summary()
    assert s == build().summary()
    assert len(s.splitlines()) <= 60
    assert "edge:n8n-main->redis" in s
