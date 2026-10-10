import copy
from pathlib import Path

import pytest

from chaosctl.chaos import chaosmesh as cm


class FakeApi:
    def __init__(self):
        self.objs = {}

    def create_namespaced_custom_object(self, g, v, ns, plural, body):
        self.objs[(plural, body["metadata"]["name"])] = copy.deepcopy(body)

    def list_namespaced_custom_object(self, g, v, ns, plural, label_selector=""):
        want = dict(kv.split("=") for kv in label_selector.split(","))
        return {"items": [o for (p, _), o in self.objs.items() if p == plural
                          and all(o["metadata"]["labels"].get(k) == v for k, v in want.items())]}

    def get_namespaced_custom_object(self, g, v, ns, plural, name):
        if (plural, name) not in self.objs:
            e = Exception("nf"); e.status = 404; raise e
        return self.objs[(plural, name)]

    def delete_namespaced_custom_object(self, g, v, ns, plural, name):
        self.objs.pop((plural, name), None)

    def patch_namespaced_custom_object(self, g, v, ns, plural, name, body):
        pass


CAT = cm.load_catalog(Path(__file__).parent / "fixtures" / "catalog.yaml")  # frozen; live catalog may change


def test_live_catalog_is_safe():
    """Whatever faults the live catalog holds, every enabled/test-only Chaos Mesh rung must pass the safety checks."""
    live = cm.load_catalog()
    for name, f in live["faults"].items():
        if not (f.get("enabled") or f.get("test_only")) or "template" not in f:
            continue
        for target in f["applies_to"]["nodes"]:
            for rung in f["ladder"]:
                cr = cm.render(live, name, target, rung, "r1", allow_test_only=True)
                assert cr["metadata"]["namespace"] == "n8n-chaos", (name, target, rung)


def test_render_worker_kill():
    cr = cm.render(CAT, "worker_pod_kill", "n8n-worker", "ungraceful", "r1")
    assert cr["kind"] == "PodChaos" and cr["spec"]["action"] == "pod-kill"
    assert cr["spec"]["gracePeriod"] == 0 and cr["spec"]["mode"] == "one"
    assert cr["metadata"]["namespace"] == "n8n-chaos"
    assert cr["spec"]["selector"]["namespaces"] == ["n8n-chaos"]
    assert cr["spec"]["selector"]["labelSelectors"]["app.kubernetes.io/component"] == "worker"
    assert cr["metadata"]["labels"]["chaosctl"] == "managed"
    assert cr["metadata"]["labels"]["chaosctl/run"] == "r1"


@pytest.mark.parametrize("args", [
    ("nope", "redis", "ungraceful", "r1"),                 # unknown fault
    ("worker_pod_kill", "redis", "ungraceful", "r1"),      # wrong target
    ("worker_pod_kill", "n8n-worker", "graceful", "r1"),   # rung not in ladder
    ("worker_graceful_kill", "n8n-worker", "graceful", "r1"),  # disabled
    ("pod_failure", "n8n-worker", "d60", "r1"),            # test-only without flag
    ("worker_pod_kill", "n8n-worker", "ungraceful", "BAD ID"),
])
def test_render_refuses(args):
    with pytest.raises(cm.ChaosRefused):
        cm.render(CAT, *args)


def test_refuses_namespace_escape_in_template():
    bad = copy.deepcopy(CAT)
    bad["templates"]["pod_kill"] = bad["templates"]["pod_kill"].replace('namespaces: ["{namespace}"]', 'namespaces: ["kube-system"]')
    with pytest.raises(cm.ChaosRefused):
        cm.render(bad, "worker_pod_kill", "n8n-worker", "ungraceful", "r1")
    bad2 = copy.deepcopy(CAT)
    bad2["templates"]["pod_kill"] = bad2["templates"]["pod_kill"].replace("mode: one", "mode: all")
    with pytest.raises(cm.ChaosRefused):
        cm.render(bad2, "worker_pod_kill", "n8n-worker", "ungraceful", "r1")


def test_pod_failure_test_variant_has_capped_duration():
    cr = cm.render(CAT, "pod_failure", "n8n-worker", "d60", "ks1", allow_test_only=True)
    assert cr["spec"]["action"] == "pod-failure" and cr["spec"]["duration"] == "60s"


def test_create_list_delete_roundtrip():
    api = FakeApi()
    c = cm.ChaosMesh(api=api, catalog=CAT)
    c.create("pod_failure", "n8n-worker", "d60", "ks1", allow_test_only=True)
    c.create("worker_pod_kill", "n8n-worker", "ungraceful", "r2")
    assert len(c.list_managed()) == 2 and len(c.list_managed("ks1")) == 1
    assert c.delete_all(wait_s=0.1) and c.list_managed() == []
