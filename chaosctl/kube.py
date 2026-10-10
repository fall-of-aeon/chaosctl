"""Kubernetes access pinned to the Kind cluster created by cluster-up."""
from __future__ import annotations

import os

EXPECTED_CONTEXT = os.environ.get("CHAOSCTL_KUBE_CONTEXT", "kind-chaos")
NAMESPACE = "n8n-chaos"


class WrongCluster(RuntimeError):
    pass


def current_context() -> str | None:
    from kubernetes import config

    _, active = config.list_kube_config_contexts()
    return active["name"] if active else None


def api_client(check: bool = True):
    """ApiClient for EXPECTED_CONTEXT; refuses if kubectl's current context differs."""
    from kubernetes import client, config

    if check and current_context() != EXPECTED_CONTEXT:
        raise WrongCluster(f"current kube context is {current_context()!r}, expected {EXPECTED_CONTEXT!r}")
    return config.new_client_from_config(context=EXPECTED_CONTEXT)
