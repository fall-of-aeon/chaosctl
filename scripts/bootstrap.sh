#!/usr/bin/env bash
# Import credentials + workflows into n8n, activate them, restart so webhooks register, smoke-test.
set -euo pipefail
cd "$(dirname "$0")/.."
CTX=kind-chaos; NS=n8n-chaos
K="kubectl --context $CTX -n $NS"
[ "$(kubectl config current-context)" = "$CTX" ] || { echo "refusing: current context is not $CTX"; exit 1; }
set -a; . state/secrets.env; set +a

main_pod() { $K get pod -l app.kubernetes.io/component=main -o jsonpath='{.items[0].metadata.name}'; }
$K rollout status deploy/n8n-main --timeout=300s
MAIN=$(main_pod)

# Postgres credential: plaintext `data` object is encrypted by n8n on import.
umask 077
CRED=$(mktemp)
cat > "$CRED" <<JSON
[{"id":"pgChaos0000001","name":"chaos-postgres","type":"postgres",
  "data":{"host":"postgres","port":5432,"database":"n8n","user":"n8n","password":"$PG_PASSWORD","ssl":"disable","allowUnauthorizedCerts":false}}]
JSON
$K cp "$CRED" "$MAIN:/tmp/cred.json" >/dev/null
rm -f "$CRED"
$K exec "$MAIN" -- n8n import:credentials --input=/tmp/cred.json
$K exec "$MAIN" -- rm -f /tmp/cred.json

$K exec "$MAIN" -- mkdir -p /tmp/wf
for f in workflows/*.json; do $K cp "$f" "$MAIN:/tmp/wf/$(basename "$f")" >/dev/null; done
$K exec "$MAIN" -- n8n import:workflow --separate --input=/tmp/wf

for id in chaosW1sync0001 chaosW2async001 chaosW3beat0001; do
  if $K exec "$MAIN" -- n8n publish:workflow --help >/dev/null 2>&1; then
    $K exec "$MAIN" -- n8n publish:workflow --id="$id"
  else
    $K exec "$MAIN" -- n8n update:workflow --id="$id" --active=true
  fi
done

# Workflows activated from the CLI only take effect after a restart.
$K rollout restart deploy/n8n-main deploy/n8n-worker
$K rollout status deploy/n8n-main --timeout=300s
$K rollout status deploy/n8n-worker --timeout=300s

echo ">> smoke test"
for i in $(seq 1 30); do
  curl -sf -o /dev/null http://127.0.0.1:15678/healthz/readiness && break; sleep 2
done
RID1=$(uuidgen | tr A-Z a-z); RID2=$(uuidgen | tr A-Z a-z)
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -s -m 60 -w ' [W1 HTTP %{http_code} %{time_total}s]\n' -H 'content-type: application/json' \
  -d "{\"request_id\":\"$RID1\",\"workflow\":\"W1\",\"received_at\":\"$NOW\"}" http://127.0.0.1:15678/webhook/chaos-w1
curl -s -m 60 -w ' [W2 HTTP %{http_code} %{time_total}s]\n' -H 'content-type: application/json' \
  -d "{\"request_id\":\"$RID2\",\"workflow\":\"W2\",\"received_at\":\"$NOW\"}" http://127.0.0.1:15678/webhook/chaos-w2
sleep 8
$K exec statefulset/postgres -- psql -U n8n -d n8n -tAc \
  "SELECT workflow, count(*) FROM orders WHERE request_id IN ('$RID1','$RID2') OR workflow='W3' GROUP BY 1 ORDER BY 1"
echo "bootstrap done"
