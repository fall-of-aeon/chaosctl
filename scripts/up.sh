#!/usr/bin/env bash
# T1b: deploy Postgres, Redis, mock-api and n8n (queue mode) into n8n-chaos.
# Usage: scripts/up.sh [baseline|hardened]
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE=${1:-baseline}
CTX=kind-chaos
NS=n8n-chaos
N8N_CHART_VERSION=1.15.0
K="kubectl --context $CTX -n $NS"

[ "$(kubectl config current-context)" = "$CTX" ] || { echo "refusing: current context is not $CTX"; exit 1; }

# Local-only secrets, generated once, never committed (state/ is gitignored).
SECRETS=state/secrets.env
if [ ! -f "$SECRETS" ]; then
  umask 077
  {
    echo "PG_PASSWORD=$(openssl rand -hex 16)"
    echo "PG_PROBE_PASSWORD=$(openssl rand -hex 16)"
    echo "N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)"
  } > "$SECRETS"
fi
set -a; . "$SECRETS"; set +a

$K create secret generic pg-secrets --from-literal=password="$PG_PASSWORD" \
  --from-literal=probe-password="$PG_PROBE_PASSWORD" --dry-run=client -o yaml | kubectl --context $CTX apply -f -
$K create secret generic n8n-secrets --from-literal=N8N_ENCRYPTION_KEY="$N8N_ENCRYPTION_KEY" \
  --from-literal=N8N_HOST=127.0.0.1 --from-literal=N8N_PORT=5678 --from-literal=N8N_PROTOCOL=http \
  --dry-run=client -o yaml | kubectl --context $CTX apply -f -

# `kind load docker-image` fails with Docker Desktop's containerd image store (attestation
# manifests), so stream a single-platform image straight into the node's containerd.
case "$(docker info -f '{{.Architecture}}')" in aarch64|arm64) PLAT=linux/arm64;; *) PLAT=linux/amd64;; esac
docker build -q --provenance=false --sbom=false -t chaosctl/mock-api:0.1.0 mock_api >/dev/null
for img in redis:7.4.5-alpine postgres:17.6-alpine docker.n8n.io/n8nio/n8n:2.42.4; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null
done
for img in redis:7.4.5-alpine postgres:17.6-alpine docker.n8n.io/n8nio/n8n:2.42.4 chaosctl/mock-api:0.1.0; do
  echo ">> loading $img"
  docker save --platform "$PLAT" "$img" | docker exec -i chaos-control-plane ctr -n k8s.io images import --snapshotter=overlayfs - >/dev/null
done

REDIS_MANIFEST=k8s/redis.yaml
[ "$PROFILE" = hardened ] && REDIS_MANIFEST=k8s/redis-hardened.yaml
kubectl --context $CTX apply -f k8s/postgres.yaml -f "$REDIS_MANIFEST" -f k8s/mock-api.yaml -f k8s/n8n-nodeport.yaml
$K rollout status statefulset/postgres --timeout=180s
$K rollout status deployment/redis --timeout=120s
$K rollout status deployment/mock-api --timeout=120s

helm upgrade --install n8n oci://ghcr.io/n8n-io/n8n-helm-chart/n8n --version $N8N_CHART_VERSION \
  --kube-context $CTX -n $NS -f helm-values/n8n-$PROFILE.yaml --wait --timeout 10m

$K get pods -o wide
echo "up done ($PROFILE). n8n: http://127.0.0.1:15678"
