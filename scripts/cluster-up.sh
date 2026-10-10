#!/usr/bin/env bash
# T1a: Kind + kube-prometheus-stack + Chaos Mesh + annotated n8n-chaos namespace.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=chaos
CTX=kind-$CLUSTER
CHAOS_MESH_VERSION=2.8.4
KPS_VERSION=92.3.0

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config scripts/kind-config.yaml --wait 120s
fi
kubectl config use-context "$CTX" >/dev/null
[ "$(kubectl config current-context)" = "$CTX" ] || { echo "wrong kube context"; exit 1; }

kubectl apply --context "$CTX" -f k8s/namespace.yaml

echo ">> kube-prometheus-stack $KPS_VERSION"
helm upgrade --install monitoring oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --kube-context "$CTX" --version "$KPS_VERSION" -n monitoring --create-namespace \
  -f helm-values/kube-prometheus-stack.yaml --wait --timeout 10m

echo ">> chaos-mesh $CHAOS_MESH_VERSION"
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update chaos-mesh >/dev/null
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --kube-context "$CTX" --version "$CHAOS_MESH_VERSION" -n chaos-mesh --create-namespace \
  -f helm-values/chaos-mesh.yaml --wait --timeout 10m

kubectl --context "$CTX" get pods -n chaos-mesh -l app.kubernetes.io/instance=chaos-mesh
echo "cluster-up done (context $CTX)"
