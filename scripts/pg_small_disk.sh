#!/usr/bin/env bash
# Put Postgres's data directory on a small loop-mounted ext4 filesystem inside the Kind node, so the
# postgres_disk_full fault fills a REAL (but tiny) disk. The Postgres PV is a hostPath on the Kind
# node, i.e. on the laptop's disk: filling that directly would fill the laptop. Idempotent.
#
# The original data directory is copied, not moved: it stays (stale) under the mount. After a Kind
# node restart the loop mount is gone; run this script again (it remounts the existing image).
set -euo pipefail
NODE=${KIND_NODE:-chaos-control-plane}
NS=n8n-chaos
SIZE_MB=${PG_DISK_MB:-512}
IMG=/var/lib/chaosctl/pgdisk.img
CTX=${CHAOSCTL_KUBE_CONTEXT:-kind-chaos}
K="kubectl --context $CTX -n $NS"

PV=$($K get pvc data-postgres-0 -o jsonpath='{.spec.volumeName}')
PVC_DIR=$(kubectl --context "$CTX" get pv "$PV" -o jsonpath='{.spec.hostPath.path}{.spec.local.path}')
[ -n "$PVC_DIR" ] || { echo "ERROR: cannot find the Postgres PV path" >&2; exit 1; }
size_kb() { docker exec "$NODE" df -Pk "$PVC_DIR" | awk 'NR==2{print $2}'; }

if docker exec "$NODE" mountpoint -q "$PVC_DIR"; then
  echo "postgres data already on the small disk ($(size_kb) KB): $PVC_DIR"
  exit 0
fi
if docker exec "$NODE" test -f "$IMG"; then MODE=remount; else MODE=create; fi
echo ">> $MODE small disk for postgres (${SIZE_MB} MiB) at $PVC_DIR; postgres restarts"

$K scale statefulset postgres --replicas=0
$K wait --for=delete pod/postgres-0 --timeout=120s 2>/dev/null || true
docker exec "$NODE" sh -euc "
  mkdir -p /var/lib/chaosctl
  if [ $MODE = create ]; then
    fallocate -l ${SIZE_MB}M $IMG
    mkfs.ext4 -q -F -m 0 $IMG          # -m 0: no root reserve, so 'full' means full for postgres too
    mkdir -p /mnt/pgdisk-init
    mount -o loop $IMG /mnt/pgdisk-init
    cp -a $PVC_DIR/. /mnt/pgdisk-init/
    umount /mnt/pgdisk-init
  fi
  mount -o loop $IMG $PVC_DIR
"
$K scale statefulset postgres --replicas=1
$K rollout status statefulset/postgres --timeout=180s
[ "$(size_kb)" -lt 4194304 ] || { echo "ERROR: mount did not take effect" >&2; exit 1; }
echo "postgres data now on a $(size_kb) KB filesystem ($IMG in node $NODE)"
