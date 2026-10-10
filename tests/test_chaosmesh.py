import copy
import re

import pytest
import yaml

from chaosctl.chaos import chaosmesh as cm


class FakeBatch:
    """BatchV1Api stand-in: jobs as dicts; the cleanup job succeeds at once."""

    def __init__(self, fail=False):
        self.jobs, self.created, self.fail = {}, [], fail

    def create_namespaced_job(self, ns, body):
        body = copy.deepcopy(body)
        body["status"] = {"failed": 3} if self.fail else {"active": 1, "succeeded": 1}
        self.jobs[body["metadata"]["name"]] = body
        self.created.append(body["metadata"]["name"])

    def list_namespaced_job(self, ns, label_selector=""):
        want = dict(kv.split("=") for kv in label_selector.split(","))
        return {"items": [j for j in self.jobs.values() if all(j["metadata"]["labels"].get(k) == v for k, v in want.items())]}

    def read_namespaced_job(self, name, ns):
        if name not in self.jobs:
            e = Exception("nf"); e.status = 404; raise e
        return self.jobs[name]

    def delete_namespaced_job(self, name, ns, propagation_policy=None):
        if name not in self.jobs:
            e = Exception("nf"); e.status = 404; raise e
        self.jobs.pop(name)


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


CAT = cm.load_catalog()


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
    ("worker_cpu_stress", "n8n-worker", "w1_load80", "r1"),  # disabled (known issue), test-only
    ("worker_redis_partition", "redis", "d20", "r1"),      # partition applies to the worker side only
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
    c = cm.ChaosMesh(api=api, catalog=CAT, batch=FakeBatch())
    c.create("pod_failure", "n8n-worker", "d60", "ks1", allow_test_only=True)
    c.create("worker_pod_kill", "n8n-worker", "ungraceful", "r2")
    assert len(c.list_managed()) == 2 and len(c.list_managed("ks1")) == 1
    assert c.delete_all(wait_s=0.1) and c.list_managed() == []


def all_rungs(enabled_only=True):
    for fault, e in CAT["faults"].items():
        if enabled_only and not e.get("enabled"):
            continue
        for node in e["applies_to"]["nodes"]:
            for rung in e["ladder"]:
                yield fault, node, rung


@pytest.mark.parametrize("fault,node,rung", list(all_rungs(enabled_only=False)))
def test_every_catalog_entry_renders_and_passes_checks(fault, node, rung):
    cr = cm.render(CAT, fault, node, rung, "r1", allow_test_only=True)
    assert cr["kind"] == CAT["faults"][fault]["chaos_kind"]
    if cr["kind"] in cm.PLURALS:
        assert cr["spec"]["mode"] == "one"
    if CAT["faults"][fault]["reversible"]:
        assert 0 < cm.duration_s(cr) <= CAT["faults"][fault]["max_duration_s"]
    assert not re.search(r"\{[a-z_]+\}", yaml.safe_dump(cr))   # no unfilled placeholder


def test_partition_target_is_scoped_to_namespace():
    cr = cm.render(CAT, "worker_redis_partition", "n8n-worker", "d20", "r1")
    t = cr["spec"]["target"]
    assert cr["spec"]["action"] == "partition" and t["mode"] == "all"
    assert t["selector"] == {"namespaces": ["n8n-chaos"], "labelSelectors": {"app": "redis"}}
    assert cr["spec"]["selector"]["labelSelectors"]["app.kubernetes.io/component"] == "worker"


def test_http_and_network_specs():
    cr = cm.render(CAT, "mockapi_http_500", "mock-api", "code500_d30", "r1")
    assert cr["kind"] == "HTTPChaos" and cr["spec"]["replace"] == {"code": 500}
    assert cr["spec"]["port"] == 8080 and cr["spec"]["path"] == "/work" and cr["spec"]["duration"] == "30s"
    cr = cm.render(CAT, "redis_latency", "redis", "l500ms", "r1")
    assert cr["spec"]["delay"]["latency"] == "500ms" and cr["spec"]["duration"] == "45s"


def _mutated(fault, edit):
    bad = copy.deepcopy(CAT)
    edit(bad, bad["faults"][fault])
    assert bad != CAT, "mutation did not apply"
    return bad


@pytest.mark.parametrize("fault,node,rung,edit", [
    # ladder values are spliced into YAML: no injection
    ("redis_latency", "redis", "l100ms", lambda c, e: e["ladder"]["l100ms"].update(latency_ms="1}, mode: all, x: {")),
    # a rung may not override the selector or namespace
    ("redis_latency", "redis", "l100ms", lambda c, e: e["ladder"]["l100ms"].update(namespace="kube-system")),
    # partition peer must be another catalog target
    ("worker_redis_partition", "n8n-worker", "d20", lambda c, e: e["params"].update(peer="n8n-worker")),
    ("worker_redis_partition", "n8n-worker", "d20", lambda c, e: e["params"].update(peer="kube-dns")),
    # network target escaping the namespace
    ("worker_redis_partition", "n8n-worker", "d20", lambda c, e: c["templates"].update(
        network_partition=c["templates"]["network_partition"].replace(
            'namespaces: ["{namespace}"]\n      labelSelectors: {peer_selector}',
            'namespaces: ["kube-system"]\n      labelSelectors: {peer_selector}'))),
    # duration above the cap, or a non-kill fault declared one-shot
    ("redis_latency", "redis", "l100ms", lambda c, e: e["ladder"]["l100ms"].update(duration=600)),
    ("redis_latency", "redis", "l100ms", lambda c, e: e.update(reversible=False)),
    # template needs a value the rung does not give
    ("redis_latency", "redis", "l100ms", lambda c, e: e["ladder"]["l100ms"].pop("jitter_ms")),
    # stress and time caps
    ("worker_memory_stress", "n8n-worker", "m256", lambda c, e: e["ladder"]["m256"].update(size_mb=4096)),
    ("worker_cpu_stress", "n8n-worker", "w1_load80", lambda c, e: e["ladder"]["w1_load80"].update(workers=8)),
    ("worker_clock_skew", "n8n-worker", "plus2m", lambda c, e: e["ladder"]["plus2m"].update(offset="48h")),
])
def test_render_refuses_unsafe_catalog_edits(fault, node, rung, edit):
    with pytest.raises(cm.ChaosRefused):
        cm.render(_mutated(fault, edit), fault, node, rung, "r1", allow_test_only=True)


# ------------------------------------------------------------ job / load-spike drivers

def test_container_kill_names_the_container():
    cr = cm.render(CAT, "worker_container_kill", "n8n-worker", "kill", "r1")
    assert cr["spec"]["action"] == "container-kill" and cr["spec"]["containerNames"] == ["n8n-worker"]


def test_jobs_render_with_labels_deadline_and_allowed_image():
    cr = cm.render(CAT, "postgres_overload", "postgres", "hold85", "r1")
    pod = cr["spec"]["template"]
    assert cr["kind"] == "Job" and cm.duration_s(cr) == 45 and cr["spec"]["backoffLimit"] == 0
    assert pod["metadata"]["labels"]["chaosctl"] == "managed"            # pods findable for cleanup
    assert "-c 85" in pod["spec"]["containers"][0]["args"][0]
    cr = cm.render(CAT, "postgres_disk_full", "postgres", "full", "r1")
    args = cr["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "REFUSED" in args and "1048576" in args                      # in-pod guard against a big disk
    assert cr["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "data-postgres-0"


def test_load_spike_render_and_cap():
    cr = cm.render(CAT, "traffic_spike", "n8n-main", "x5", "r1")
    assert cr["kind"] == "LoadSpike" and cr["spec"]["rate_rps"] == 10 and cm.duration_s(cr) == 60
    with pytest.raises(cm.ChaosRefused):
        cm.ChaosMesh(api=FakeApi(), catalog=CAT, batch=FakeBatch()).create("traffic_spike", "n8n-main", "x3", "r1")


def _job_edit(path_fn):
    def edit(c, e):
        cr_text = c["templates"][e["template"]]
        c["templates"][e["template"]] = path_fn(cr_text)
    return edit


@pytest.mark.parametrize("fault,rung,edit", [
    ("postgres_overload", "cpu4", lambda c, e: e["params"].update(image="evil/miner:latest")),
    ("postgres_overload", "cpu4", lambda c, e: e["ladder"]["cpu4"].update(clients=500)),
    ("postgres_overload", "cpu4", _job_edit(lambda t: t.replace("automountServiceAccountToken: false",
                                                                 "automountServiceAccountToken: true"))),
    ("postgres_overload", "cpu4", _job_edit(lambda t: t.replace("restartPolicy: Never",
                                                                 "restartPolicy: Never\n      hostNetwork: true"))),
    ("postgres_overload", "cpu4", _job_edit(lambda t: t.replace("imagePullPolicy: IfNotPresent",
                                                                 "imagePullPolicy: IfNotPresent\n          securityContext: {privileged: true}"))),
    ("postgres_overload", "cpu4", lambda c, e: e["ladder"]["cpu4"].update(duration=3600)),
    ("postgres_disk_full", "full", lambda c, e: e["params"].update(pvc="some-other-pvc")),
    ("postgres_disk_full", "full", _job_edit(lambda t: t.replace("persistentVolumeClaim: {claimName: \"{pvc}\"}",
                                                                  "hostPath: {path: /}"))),
    ("traffic_spike", "x3", lambda c, e: e["ladder"]["x3"].update(rate_rps=500)),
])
def test_job_and_spike_refusals(fault, rung, edit):
    target = CAT["faults"][fault]["applies_to"]["nodes"][0]
    with pytest.raises(cm.ChaosRefused):
        cm.render(_mutated(fault, edit), fault, target, rung, "r1")


def test_jobs_are_listed_deleted_and_ballast_cleaned():
    api, batch = FakeApi(), FakeBatch()
    c = cm.ChaosMesh(api=api, catalog=CAT, batch=batch)
    c.create("postgres_disk_full", "postgres", "full", "r9")
    c.create("redis_latency", "redis", "l100ms", "r9")
    assert {x["kind"] for x in c.list_managed("r9")} == {"Job", "NetworkChaos"}
    c.delete_all("r9", wait_s=0.1)
    assert c.list_managed() == [] and batch.jobs == {}
    assert any(n.startswith("chaosctl-cleanup-") for n in batch.created)   # ballast cleanup job ran


def test_failed_ballast_cleanup_is_reported(monkeypatch):
    from chaosctl import alerts
    monkeypatch.setattr(cm.time, "sleep", lambda _s: None)
    c = cm.ChaosMesh(api=FakeApi(), catalog=CAT, batch=FakeBatch(fail=True))
    assert c.remove_ballast("postgres_disk_full", timeout_s=0.5) is False
    assert any(e["component"] == "disk ballast cleanup" for e in alerts.seen())


# ------------------------------------------------------------ did the fault actually inject?

class StatusApi(FakeApi):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def get_namespaced_custom_object(self, g, v, ns, plural, name):
        return {**super().get_namespaced_custom_object(g, v, ns, plural, name), "status": self.status}


@pytest.mark.parametrize("status,ok", [
    ({"conditions": [{"type": "AllInjected", "status": "True"}],
      "experiment": {"containerRecords": [{"id": "n8n-chaos/redis-1", "phase": "Injected"}]}}, True),
    ({"conditions": [{"type": "AllInjected", "status": "False"}],
      "experiment": {"containerRecords": [{"id": "n8n-chaos/pg-0", "phase": "Not Injected",
                                           "events": [{"type": "Failed", "message": "toda update RPC failed"}]}]}}, False),
])
def test_verify_injected_chaos(status, ok, monkeypatch):
    monkeypatch.setattr(cm.time, "sleep", lambda _s: None)
    api = StatusApi(status)
    c = cm.ChaosMesh(api=api, catalog=CAT, batch=FakeBatch())
    cr = c.create("redis_latency", "redis", "l100ms", "r1")
    got, detail = c.verify_injected(cr, timeout_s=0.05)
    assert got is ok
    if not ok:
        assert "toda" in detail


def test_verify_injected_job_failure(monkeypatch):
    monkeypatch.setattr(cm.time, "sleep", lambda _s: None)
    c = cm.ChaosMesh(api=FakeApi(), catalog=CAT, batch=FakeBatch(fail=True))
    cr = c.create("postgres_overload", "postgres", "cpu4", "r1")
    got, detail = c.verify_injected(cr, timeout_s=0.05)
    assert got is False and "failed" in detail


def test_disk_fill_injection_needs_the_jobs_own_report(monkeypatch):
    monkeypatch.setattr(cm.time, "sleep", lambda _s: None)
    c = cm.ChaosMesh(api=FakeApi(), catalog=CAT, batch=FakeBatch())
    cr = c.create("postgres_disk_full", "postgres", "full", "r1")
    monkeypatch.setattr(c, "_job_log", lambda name: "1+0 records in")
    ok, detail = c.verify_injected(cr, timeout_s=0.05)
    assert ok is False and "disk now" in detail          # running is not enough
    monkeypatch.setattr(c, "_job_log", lambda name: "disk now 100% used, 0 KB free")
    ok, detail = c.verify_injected(cr, timeout_s=0.05)
    assert ok is True and "100% used" in detail
