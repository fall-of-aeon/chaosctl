#!/usr/bin/env bash
# Remove the n8n stack (keeps the cluster, Chaos Mesh and monitoring).
set -euo pipefail
cd "$(dirname "$0")/.."
CTX=kind-chaos
[ "$(kubectl config current-context)" = "$CTX" ] || { echo "refusing: current context is not $CTX"; exit 1; }
helm uninstall n8n --kube-context $CTX -n n8n-chaos || true
kubectl --context $CTX delete -f k8s/n8n-nodeport.yaml -f k8s/mock-api.yaml -f k8s/redis.yaml -f k8s/postgres.yaml --ignore-not-found
echo "down done (PVCs kept; delete with: kubectl -n n8n-chaos delete pvc --all)"
