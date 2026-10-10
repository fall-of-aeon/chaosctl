"""Create/delete/pause fault resources, only from catalog templates, only in n8n-chaos.

Drivers: Chaos Mesh CRs (most faults), Kubernetes Jobs from allow-listed images (pgbench
load, disk ballast) and LoadSpike (rendered and checked here, executed by the runner's
load generator). Every failure to clean up is reported through chaosctl.alerts."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import yaml

from chaosctl import alerts
from chaosctl.journal import Journal, NullJournal
from chaosctl.kube import NAMESPACE

GROUP, VERSION = "chaos-mesh.org", "v1alpha1"
PLURALS = {"PodChaos": "podchaos", "NetworkChaos": "networkchaos", "StressChaos": "stresschaos",
           "HTTPChaos": "httpchaos", "TimeChaos": "timechaos"}
JOB_KINDS = {"Job"}
RUNNER_KINDS = {"LoadSpike"}          # rendered + checked here, executed by the runner
ALLOWED_KINDS = set(PLURALS) | JOB_KINDS | RUNNER_KINDS
MANAGED = {"chaosctl": "managed"}
BALLAST = "chaosctl-ballast"
PAUSE_ANNOTATION = "experiment.chaos-mesh.org/pause"
CATALOG_PATH = Path(__file__).parents[1] / "catalog.yaml"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9./_+:-]{1,64}")   # ladder values are spliced into YAML
_SIZE_MB = re.compile(r"(\d+)MB")
_OFFSET = re.compile(r"(-?)(\d+)(s|m)")


class ChaosRefused(ValueError):
    """The requested chaos resource violates the catalog or namespace policy."""


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def resource_name(run_id: str, fault: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", f"chaosctl-{run_id}-{fault}".lower())[:63].rstrip("-")


def render(catalog: dict, fault: str, target: str, rung: str, run_id: str,
           allow_test_only: bool = False) -> dict:
    """Render a CR dict from the catalog, or raise ChaosRefused."""
    entry = (catalog.get("faults") or {}).get(fault)
    if entry is None:
        raise ChaosRefused(f"unknown fault {fault!r}")
    if not entry.get("enabled") and not (allow_test_only and entry.get("test_only")):
        raise ChaosRefused(f"fault {fault!r} is disabled")
    if target not in entry.get("applies_to", {}).get("nodes", []):
        raise ChaosRefused(f"fault {fault!r} does not apply to {target!r}")
    selector = (catalog.get("targets") or {}).get(target)
    if not selector:
        raise ChaosRefused(f"no selector for target {target!r}")
    params = (entry.get("ladder") or {}).get(rung)
    if params is None:
        raise ChaosRefused(f"rung {rung!r} not in ladder of {fault!r}")
    if not re.fullmatch(r"[a-z0-9-]{1,30}", run_id):
        raise ChaosRefused(f"bad run id {run_id!r}")

    params = {**(entry.get("params") or {}), **params}
    values = {
        "name": resource_name(run_id, fault),
        "namespace": NAMESPACE,
        "run_id": run_id,
        "selector": json.dumps(selector, sort_keys=True),
    }
    if "peer" in params:
        peer = (catalog.get("targets") or {}).get(params["peer"])
        if not peer or params["peer"] == target:
            raise ChaosRefused(f"bad peer {params['peer']!r} for {fault!r}")
        values["peer_selector"] = json.dumps(peer, sort_keys=True)
    if target in (catalog.get("containers") or {}):
        values["container"] = catalog["containers"][target]
    for k, v in params.items():
        if k == "peer":
            continue
        if k in values:
            raise ChaosRefused(f"ladder may not override {{{k}}}")
        if isinstance(v, bool) or not _SAFE_VALUE.fullmatch(str(v)):
            raise ChaosRefused(f"unsafe ladder value {k}={v!r}")
        values[k] = str(v)

    def fill(m: re.Match) -> str:
        if m.group(1) not in values:
            raise ChaosRefused(f"template {entry['template']!r} needs {{{m.group(1)}}}, not given for rung {rung!r}")
        return values[m.group(1)]

    text = catalog["templates"][entry["template"]]
    cr = yaml.safe_load(_PLACEHOLDER.sub(fill, text))
    labels = cr["metadata"].setdefault("labels", {})
    labels.update({**MANAGED, "chaosctl/run": run_id, "chaosctl/fault": fault.replace("_", "-")})
    if cr.get("kind") in JOB_KINDS:   # label the pods too, so cleanup and evidence can find them
        cr["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {}).update(labels)
    _check(cr, entry)
    return cr


def _scoped(selector: dict | None, what: str) -> None:
    if (selector or {}).get("namespaces") != [NAMESPACE]:
        raise ChaosRefused(f"{what} must target only n8n-chaos")
    if not (selector or {}).get("labelSelectors"):
        raise ChaosRefused(f"{what} must have labelSelectors")


def duration_s(cr: dict) -> int:
    if cr.get("kind") in JOB_KINDS:
        return int(cr.get("spec", {}).get("activeDeadlineSeconds") or 0)
    return int(str(cr.get("spec", {}).get("duration", "0")).rstrip("s") or 0)


def _check_job(cr: dict, entry: dict) -> None:
    spec = cr.get("spec", {})
    pod = spec.get("template", {}).get("spec", {})
    if spec.get("backoffLimit") != 0:
        raise ChaosRefused("job must have backoffLimit 0 (run once)")
    if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
        raise ChaosRefused("job may not use host namespaces")
    if pod.get("automountServiceAccountToken") is not False:
        raise ChaosRefused("job must not mount a service account token")
    for c in pod.get("containers", []) + pod.get("initContainers", []):
        if c.get("image") not in entry.get("allowed_images", []):
            raise ChaosRefused(f"image {c.get('image')!r} not in allowed_images")
        sc = c.get("securityContext") or {}
        if sc.get("privileged") or sc.get("allowPrivilegeEscalation") or (sc.get("capabilities") or {}).get("add"):
            raise ChaosRefused("job containers may not be privileged or add capabilities")
    for v in pod.get("volumes", []):
        if set(v) - {"name", "persistentVolumeClaim", "emptyDir"}:
            raise ChaosRefused(f"volume type of {v.get('name')!r} not allowed")
        claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
        if claim is not None and claim not in entry.get("allowed_pvcs", []):
            raise ChaosRefused(f"pvc {claim!r} not in allowed_pvcs")
    if entry.get("max_clients"):
        args = " ".join(" ".join(c.get("args", []) + c.get("command", [])) for c in pod.get("containers", []))
        m = re.search(r"-c (\d+)", args)
        if not m or int(m.group(1)) > int(entry["max_clients"]):
            raise ChaosRefused(f"pgbench clients must be <= max_clients={entry['max_clients']}")


def _check(cr: dict, entry: dict) -> None:
    """Defence in depth: re-verify the rendered object, whatever the template said."""
    spec = cr.get("spec", {})
    kind = cr["kind"]
    if kind not in ALLOWED_KINDS or kind != entry["chaos_kind"]:
        raise ChaosRefused(f"kind {kind!r} not allowed")
    if cr["metadata"].get("namespace") != NAMESPACE:
        raise ChaosRefused("resource namespace must be n8n-chaos")
    if kind in JOB_KINDS:
        _check_job(cr, entry)
    elif kind == "LoadSpike":
        if not 0 < float(spec.get("rate_rps", 0)) <= float(entry.get("max_rate_rps", 0)):
            raise ChaosRefused(f"load spike rate must be <= max_rate_rps={entry.get('max_rate_rps', 0)}")
    else:
        _scoped(spec.get("selector"), "selector")
        if spec.get("mode") != "one":
            raise ChaosRefused("mode must be 'one'")
    if "target" in spec and kind == "NetworkChaos":
        _scoped(spec["target"].get("selector"), "network target")
        if spec["target"].get("mode") not in ("one", "all"):
            raise ChaosRefused("network target mode must be 'one' or 'all'")
    one_shot = kind == "PodChaos" and spec.get("action") in ("pod-kill", "container-kill")
    if not one_shot and not entry.get("reversible"):
        raise ChaosRefused(f"{kind} must be reversible with a duration (only pod-kill/container-kill are one-shot)")
    if entry.get("reversible"):
        dur = duration_s(cr)
        if dur <= 0 or dur > int(entry.get("max_duration_s", 0)):
            raise ChaosRefused(f"reversible fault needs 0 < duration <= {entry.get('max_duration_s')}s")
    stress = spec.get("stressors") or {}
    if "cpu" in stress and int(stress["cpu"].get("workers", 0)) > int(entry.get("max_cpu_workers", 1)):
        raise ChaosRefused(f"cpu workers above max_cpu_workers={entry.get('max_cpu_workers', 1)}")
    if "memory" in stress:
        m = _SIZE_MB.fullmatch(str(stress["memory"].get("size", "")))
        if not m or int(m.group(1)) > int(entry.get("max_memory_mb", 0)) or int(stress["memory"].get("workers", 0)) != 1:
            raise ChaosRefused(f"memory stress must be 1 worker and <= max_memory_mb={entry.get('max_memory_mb', 0)}MB")
    if kind == "TimeChaos":
        m = _OFFSET.fullmatch(str(spec.get("timeOffset", "")))
        secs = int(m.group(2)) * (60 if m and m.group(3) == "m" else 1) if m else None
        if secs is None or secs > int(entry.get("max_offset_s", 0)):
            raise ChaosRefused(f"time offset must be <= max_offset_s={entry.get('max_offset_s', 0)}s")


def _to_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    from kubernetes.client import ApiClient
    return ApiClient().sanitize_for_serialization(obj)


class ChaosMesh:
    """Creates and removes every harness-made fault resource (Chaos Mesh CRs and Jobs)."""

    def __init__(self, api=None, catalog: dict | None = None, journal: Journal | None = None, batch=None):
        if api is None or batch is None:
            from kubernetes import client

            from chaosctl.kube import api_client
            api = api or client.CustomObjectsApi(api_client())
            batch = batch or client.BatchV1Api(api_client())
        self.api = api
        self.batch = batch
        self.catalog = catalog or load_catalog()
        self.journal = journal or NullJournal()

    def create(self, fault: str, target: str, rung: str, run_id: str, allow_test_only: bool = False) -> dict:
        cr = render(self.catalog, fault, target, rung, run_id, allow_test_only)
        if cr["kind"] in RUNNER_KINDS:
            raise ChaosRefused(f"{cr['kind']} is executed by the runner, not created in Kubernetes")
        if cr["kind"] in JOB_KINDS:
            self.batch.create_namespaced_job(NAMESPACE, cr)
        else:
            self.api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[cr["kind"]], cr)
        self.journal.write({"event": "chaos_create", "name": cr["metadata"]["name"], "fault": fault,
                            "target": target, "rung": rung, "run_id": run_id, "spec": cr["spec"]})
        return cr

    def list_managed(self, run_id: str | None = None, include_cleanup: bool = False) -> list[dict]:
        sel = "chaosctl=managed" + (f",chaosctl/run={run_id}" if run_id else "")
        out = []
        for kind, plural in PLURALS.items():
            try:
                items = self.api.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, label_selector=sel)
            except Exception as exc:  # CRD missing etc.
                if getattr(exc, "status", None) == 404:
                    continue
                raise
            for it in items.get("items", []):
                it.setdefault("kind", kind)
                out.append(it)
        for it in _to_dict(self.batch.list_namespaced_job(NAMESPACE, label_selector=sel)).get("items", []):
            if include_cleanup or (it.get("metadata", {}).get("labels") or {}).get("chaosctl/role") != "cleanup":
                it["kind"] = "Job"
                out.append(it)
        return out

    def get(self, kind: str, name: str) -> dict | None:
        try:
            if kind in JOB_KINDS:
                return _to_dict(self.batch.read_namespaced_job(name, NAMESPACE))
            return self.api.get_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[kind], name)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise

    def pause(self, kind: str, name: str) -> None:
        body = {"metadata": {"annotations": {PAUSE_ANNOTATION: "true"}}}
        self.api.patch_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[kind], name, body)
        self.journal.write({"event": "chaos_pause", "name": name})

    def _delete(self, kind: str, name: str) -> None:
        if kind in JOB_KINDS:
            self.batch.delete_namespaced_job(name, NAMESPACE, propagation_policy="Foreground")
        else:
            self.api.delete_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[kind], name)

    def delete_all(self, run_id: str | None = None, wait_s: float = 15.0) -> list[str]:
        """Delete every managed resource (restores reversible faults). Falls back to
        pause + finalizer removal (Chaos Mesh forced clean-up) if a delete is stuck.
        Faults with `cleanup: ballast` get a cleanup Job that removes the ballast file."""
        deleted, cleanups = [], set()
        for cr in self.list_managed(run_id):
            kind, name = cr["kind"], cr["metadata"]["name"]
            fault = (cr["metadata"].get("labels") or {}).get("chaosctl/fault", "").replace("-", "_")
            if (self.catalog["faults"].get(fault) or {}).get("cleanup") == "ballast":
                cleanups.add(fault)
            try:
                self._delete(kind, name)
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    self.journal.write({"event": "chaos_delete_error", "name": name, "err": str(exc)[:200]})
                    alerts.warn("chaos cleanup", f"could not delete {kind} {name}: {type(exc).__name__}: "
                                f"{str(exc)[:200]}", self.journal)
            deleted.append((kind, name))

        stuck = []
        deadline = time.monotonic() + wait_s + (30 if any(k in JOB_KINDS for k, _ in deleted) else 0)
        for kind, name in deleted:
            while self.get(kind, name) is not None and time.monotonic() < deadline:
                time.sleep(0.5)
            if self.get(kind, name) is not None:
                stuck.append((kind, name))
        for kind, name in stuck:
            if kind in JOB_KINDS:
                alerts.warn("chaos cleanup", f"Job {name} still exists {wait_s + 30:.0f}s after delete", self.journal)
                continue
            self.pause(kind, name)
            self.api.patch_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURALS[kind], name, {"metadata": {"finalizers": []}})
            self.journal.write({"event": "chaos_force_cleanup", "name": name})
            alerts.warn("chaos cleanup", f"{kind} {name} was stuck; forced clean-up (pause + finalizers removed). "
                        "Check the target is restored.", self.journal, kind="warning")
        for fault in sorted(cleanups):
            self.remove_ballast(fault)
        names = [n for _, n in deleted]
        self.journal.write({"event": "chaos_delete_all", "run_id": run_id, "names": names, "forced": [n for _, n in stuck]})
        return names

    def verify_injected(self, cr: dict, timeout_s: float = 30) -> tuple[bool, str]:
        """Did the fault actually take effect? A CR that is created but never injected (or a Job whose pod
        fails) must not pass as a clean experiment. Returns (ok, detail)."""
        kind, name = cr["kind"], cr["metadata"]["name"]
        t0 = time.monotonic()
        last = "no status yet"
        while time.monotonic() - t0 < timeout_s:
            obj = self.get(kind, name)
            if obj is None:
                return False, f"{kind} {name} disappeared before it was confirmed"
            st = obj.get("status") or {}
            if kind in JOB_KINDS:
                if st.get("failed"):
                    return False, f"job pod failed: {json.dumps(st.get('conditions'), default=str)[:200]} " \
                                  f"log: {self._job_log(name)[-200:]}"
                want = self._entry_for(cr).get("verify_log")
                if (st.get("active") or st.get("succeeded")) and not want:
                    return True, f"job pod running (active={st.get('active')}, succeeded={st.get('succeeded')})"
                if st.get("active") or st.get("succeeded"):
                    lines = [ln for ln in self._job_log(name).splitlines() if want in ln]
                    if lines:   # the job itself reports the effect (e.g. "disk now 100% used, 0 KB free")
                        return True, f"job reports: {lines[-1][:200]}"
                    last = f"job running, waiting for {want!r} in its log"
                    time.sleep(1)
                    continue
                last = "job pod not started"
            else:
                conds = {c["type"]: c["status"] for c in st.get("conditions") or []}
                recs = (st.get("experiment") or {}).get("containerRecords") or []
                if conds.get("AllInjected") == "True" or (recs and all(r.get("phase") == "Injected" for r in recs)):
                    return True, f"injected into {[r.get('id') for r in recs]}"
                msgs = [e.get("message") for r in recs for e in (r.get("events") or []) if e.get("type") == "Failed"]
                last = (msgs[-1] if msgs else f"conditions {conds}")
                # a one-shot pod/container kill finishes fast: records go Injected then the pod is gone
                if kind == "PodChaos" and cr["spec"].get("action") in ("pod-kill", "container-kill") and recs \
                        and any(r.get("injectedCount", 0) >= 1 for r in recs):
                    return True, f"kill executed on {[r.get('id') for r in recs]}"
            time.sleep(1)
        return False, f"not confirmed within {timeout_s:.0f}s: {str(last)[:300]}"

    def _entry_for(self, cr: dict) -> dict:
        fault = (cr["metadata"].get("labels") or {}).get("chaosctl/fault", "").replace("-", "_")
        return self.catalog["faults"].get(fault) or {}

    def _job_log(self, job_name: str) -> str:
        """Logs of a Job's pod ('' if none yet). Used to confirm the fault's effect, not just that it started."""
        try:
            if getattr(self, "core", None) is None:
                from kubernetes import client

                from chaosctl.kube import api_client
                self.core = client.CoreV1Api(api_client())
            pods = self.core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job_name}").items
            return "\n".join(self.core.read_namespaced_pod_log(p.metadata.name, NAMESPACE, tail_lines=50) or ""
                             for p in pods)
        except Exception as exc:  # noqa: BLE001 - surfaced in verify_injected's detail
            return f"(log unavailable: {type(exc).__name__})"

    def remove_ballast(self, fault: str, timeout_s: float = 60) -> bool:
        """Belt and braces after a disk-fill fault: a one-shot Job deletes the ballast file even if the
        ballast pod was killed before its trap ran (or postgres is down)."""
        entry = self.catalog["faults"][fault]
        params = entry["params"]
        name = f"chaosctl-cleanup-{fault.replace('_', '-')}"[:63]
        job = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": NAMESPACE,
                         "labels": {**MANAGED, "chaosctl/role": "cleanup", "chaosctl/fault": fault.replace("_", "-")}},
            "spec": {"backoffLimit": 2, "activeDeadlineSeconds": int(timeout_s), "template": {
                "metadata": {"labels": {**MANAGED, "chaosctl/role": "cleanup"}},
                "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                         "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": params["pvc"]}}],
                         "containers": [{"name": "cleanup", "image": params["image"], "imagePullPolicy": "IfNotPresent",
                                         "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                                         "command": ["sh", "-c", f"rm -f /data/{BALLAST}; ls /data/{BALLAST} 2>/dev/null "
                                                     "&& exit 1; echo ballast gone"]}]}}}}
        try:
            self.batch.delete_namespaced_job(name, NAMESPACE, propagation_policy="Foreground")
            time.sleep(2)
        except Exception:  # noqa: BLE001 - usually 404: no old cleanup job
            pass
        ok = False
        try:
            self.batch.create_namespaced_job(NAMESPACE, job)
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout_s:
                st = (_to_dict(self.batch.read_namespaced_job(name, NAMESPACE)).get("status") or {})
                if st.get("succeeded"):
                    ok = True
                    break
                if (st.get("failed") or 0) > 2:
                    break
                time.sleep(1)
        except Exception as exc:  # noqa: BLE001 - reported below
            alerts.warn("disk ballast cleanup", f"cleanup Job failed to run: {type(exc).__name__}: {str(exc)[:200]}",
                        self.journal)
        if not ok:
            alerts.warn("disk ballast cleanup", f"could not confirm /data/{BALLAST} was removed from {params['pvc']}; "
                        "postgres may stay out of disk. Remove it by hand.", self.journal)
        else:
            try:
                self.batch.delete_namespaced_job(name, NAMESPACE, propagation_policy="Background")
            except Exception:  # noqa: BLE001 - leftover finished job is harmless and has a label
                pass
        self.journal.write({"event": "ballast_cleanup", "fault": fault, "ok": ok})
        return ok
