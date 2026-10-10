"""Create/delete/pause Chaos Mesh resources, only from catalog templates, only in n8n-chaos."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import yaml

from chaosctl.journal import Journal, NullJournal
from chaosctl.kube import NAMESPACE

GROUP, VERSION = "chaos-mesh.org", "v1alpha1"
PLURALS = {"PodChaos": "podchaos", "NetworkChaos": "networkchaos", "StressChaos": "stresschaos"}
MANAGED = {"chaosctl": "managed"}
PAUSE_ANNOTATION = "experiment.chaos-mesh.org/pause"
CATALOG_PATH = Path(__file__).parents[1] / "catalog.yaml"
_PLACEHOLDER = re.compile(r"\{(name|namespace|run_id|selector|grace_period|duration)\}")


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

    values = {
        "name": resource_name(run_id, fault),
        "namespace": NAMESPACE,
        "run_id": run_id,
        "selector": json.dumps(selector, sort_keys=True),
        "grace_period": str(int(params.get("grace_period", 0))),
        "duration": str(int(params.get("duration", 0))),
    }
    text = catalog["templates"][entry["template"]]
    cr = yaml.safe_load(_PLACEHOLDER.sub(lambda m: values[m.group(1)], text))
    labels = cr["metadata"].setdefault("labels", {})
    labels.update({**MANAGED, "chaosctl/run": run_id, "chaosctl/fault": fault.replace("_", "-")})
    _check(cr, entry)
    return cr


def _check(cr: dict, entry: dict) -> None:
    """Defence in depth: re-verify the rendered object, whatever the template said."""
    spec = cr.get("spec", {})
    if cr["kind"] not in PLURALS or cr["kind"] != entry["chaos_kind"]:
        raise ChaosRefused(f"kind {cr['kind']!r} not allowed")
    if cr["metadata"].get("namespace") != NAMESPACE:
        raise ChaosRefused("resource namespace must be n8n-chaos")
    if spec.get("selector", {}).get("namespaces") != [NAMESPACE]:
        raise ChaosRefused("selector must target only n8n-chaos")
    if not spec.get("selector", {}).get("labelSelectors"):
        raise ChaosRefused("selector must have labelSelectors")
    if spec.get("mode") != "one":
        raise ChaosRefused("mode must be 'one'")
    if entry.get("reversible"):
        dur = int(str(spec.get("duration", "0")).rstrip("s") or 0)
        if dur <= 0 or dur > int(entry.get("max_duration_s", 0)):
            raise ChaosRefused(f"reversible fault needs 0 < duration <= {entry.get('max_duration_s')}s")


class ChaosMesh:
    def __init__(self, api=None, catalog: dict | None = None, journal: Journal | None = None):
        if api is None:
            from kubernetes import client

            from chaosctl.kube import api_client
            api = client.CustomObjectsApi(api_client())
        self.api = api
        self.catalog = catalog or load_catalog()
        self.journal = journal or NullJournal()

    def create(self, fault: str, target: str, rung: str, run_id: str, allow_test_only: bool = False) -> dict:
        cr = render(self.catalog, fault, target, rung, run_id, allow_test_only)
        self.api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[cr["kind"]], cr)
        self.journal.write({"event": "chaos_create", "name": cr["metadata"]["name"], "fault": fault,
                            "target": target, "rung": rung, "run_id": run_id, "spec": cr["spec"]})
        return cr

    def list_managed(self, run_id: str | None = None) -> list[dict]:
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
        return out

    def get(self, kind: str, name: str) -> dict | None:
        try:
            return self.api.get_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[kind], name)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise

    def pause(self, kind: str, name: str) -> None:
        body = {"metadata": {"annotations": {PAUSE_ANNOTATION: "true"}}}
        self.api.patch_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[kind], name, body)
        self.journal.write({"event": "chaos_pause", "name": name})

    def delete_all(self, run_id: str | None = None, wait_s: float = 15.0) -> list[str]:
        """Delete every managed resource (restores reversible faults). Falls back to
        pause + finalizer removal (Chaos Mesh forced clean-up) if a delete is stuck."""
        deleted = []
        for cr in self.list_managed(run_id):
            kind, name = cr["kind"], cr["metadata"]["name"]
            try:
                self.api.delete_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURALS[kind], name)
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    self.journal.write({"event": "chaos_delete_error", "name": name, "err": str(exc)[:200]})
            deleted.append((kind, name))

        stuck = []
        deadline = time.monotonic() + wait_s
        for kind, name in deleted:
            while self.get(kind, name) is not None and time.monotonic() < deadline:
                time.sleep(0.5)
            if self.get(kind, name) is not None:
                stuck.append((kind, name))
        for kind, name in stuck:
            self.pause(kind, name)
            self.api.patch_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURALS[kind], name, {"metadata": {"finalizers": []}})
            self.journal.write({"event": "chaos_force_cleanup", "name": name})
        names = [n for _, n in deleted]
        self.journal.write({"event": "chaos_delete_all", "run_id": run_id, "names": names, "forced": [n for _, n in stuck]})
        return names
