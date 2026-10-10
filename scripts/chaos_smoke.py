"""Live smoke test of every fault driver in the catalog (no load, no agents).

For each case: create the resource from the catalog, confirm it was injected
(ChaosMesh.verify_injected), run a functional probe (from a worker pod, or psql in the
postgres pod), delete everything via delete_all, and probe again to prove it was
restored. Usage: uv run python scripts/chaos_smoke.py [fault ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

from chaosctl.chaos.chaosmesh import ChaosMesh
from chaosctl.kube import NAMESPACE

RUN_ID = "smoke"
NODE = "node"

# probe scripts run with `node -e` in a worker pod; each prints one JSON line
HTTP = """const t=Date.now();fetch('http://mock-api:8080/work?request_id=smoke',{signal:AbortSignal.timeout(20000)})
.then(r=>console.log(JSON.stringify({status:r.status,ms:Date.now()-t})))
.catch(e=>console.log(JSON.stringify({error:String(e.cause?.code||e.name),ms:Date.now()-t})))"""
TCP = """const net=require('net');const t=Date.now();const s=net.connect({host:'%s',port:%d});
s.setTimeout(5000,()=>{console.log(JSON.stringify({error:'timeout',ms:Date.now()-t}));s.destroy()});
s.on('connect',()=>{const c=Date.now()-t;%s});
s.on('error',e=>console.log(JSON.stringify({error:e.code,ms:Date.now()-t})))"""
REDIS = TCP % ("redis", 6379, "s.write('PING\\r\\n');s.once('data',()=>{console.log(JSON.stringify({connect_ms:c,ms:Date.now()-t}));s.end()})")
PG = TCP % ("postgres", 5432, "console.log(JSON.stringify({connect_ms:c,ms:c}));s.end()")

# these two run psql inside the postgres pod (target "postgres:")
PG_CONNS = "postgres:" + "psql -U n8n -d n8n -Atc \"select count(*) from pg_stat_activity where application_name='pgbench'\""
PG_WRITE = "postgres:" + ("psql -U n8n -d n8n -v ON_ERROR_STOP=1 -c \"create table if not exists smoke_disk(x text); "
                         "insert into smoke_disk select repeat('x', 2000) from generate_series(1, 20000); "
                         "checkpoint; drop table smoke_disk;\"")

# fault, rung, probe, check(during) -> bool, check(after) -> bool, probe every worker?
CASES = [
    ("mockapi_http_500", "code500_d30", HTTP, lambda r: r.get("status") == 500, lambda r: r.get("status") == 200, False),
    ("mockapi_http_abort", "d30", HTTP, lambda r: "error" in r, lambda r: r.get("status") == 200, False),
    ("mockapi_latency", "plus2s", HTTP, lambda r: r.get("ms", 0) >= 5800, lambda r: r.get("ms", 0) < 5500, False),
    ("redis_latency", "l100ms", REDIS, lambda r: r.get("ms", 0) >= 80, lambda r: r.get("ms", 1e9) < 50, False),
    ("postgres_latency", "l100ms", PG, lambda r: r.get("ms", 0) >= 80, lambda r: r.get("ms", 1e9) < 50, False),
    ("worker_redis_partition", "d20", REDIS, lambda r: "error" in r, lambda r: "error" not in r, True),
    ("worker_container_kill", "kill", None, None, None, False),
    ("mockapi_container_kill", "kill", HTTP, lambda r: True, lambda r: r.get("status") == 200, False),
    ("packet_loss", "loss30", REDIS, lambda r: r.get("ms", 0) >= 150 or "error" in r, lambda r: r.get("ms", 1e9) < 50, False),
    ("postgres_overload", "hold85", PG_CONNS, lambda r: r.get("held", 0) >= 80, lambda r: r.get("held", 99) < 5, False),
    ("postgres_disk_full", "full", PG_WRITE, lambda r: "No space left" in r.get("error", ""),
     lambda r: r.get("ok") is True, False),
    # disabled in the catalog (known_issue): run only on fresh pods, e.g. after a worker rollout restart
    ("worker_cpu_stress", "w1_load80", None, None, None, False),
    ("worker_memory_stress", "m256", None, None, None, False),
    ("worker_clock_skew", "plus2m", None, None, None, False),
]


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout.strip()


def workers() -> list[str]:
    return sh("kubectl", "-n", NAMESPACE, "get", "pod", "-l", "app.kubernetes.io/component=worker",
              "-o", "jsonpath={.items[*].metadata.name}").split()


def probe(pod: str, script: str) -> dict:
    if script.startswith("postgres:"):
        r = subprocess.run(["kubectl", "-n", NAMESPACE, "exec", "postgres-0", "--", "sh", "-c", script[9:]],
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        if "pgbench" in script:
            return {"held": int(out.splitlines()[-1]) if out and out.splitlines()[-1].isdigit() else -1}
        return {"ok": True} if r.returncode == 0 else {"error": out[-200:]}
    out = sh("kubectl", "-n", NAMESPACE, "exec", pod, "--", NODE, "-e", script)
    try:
        return json.loads(out.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"error": f"no output: {out[:80]}"}


def main(only: list[str]) -> int:
    cm = ChaosMesh()
    if cm.list_managed():
        print("refusing: chaosctl-managed chaos already exists")
        return 2
    failed = 0
    for fault, rung, script, during_ok, after_ok, every_worker in CASES:
        if only and fault not in only:
            continue
        pods = workers()
        target = "redis" if fault == "packet_loss" else cm.catalog["faults"][fault]["applies_to"]["nodes"][0]
        cr = cm.create(fault, target, rung, RUN_ID, allow_test_only=True)
        kind, name = cr["kind"], cr["metadata"]["name"]
        try:
            ok, targets = cm.verify_injected(cr)
            if ok and script and fault.startswith("postgres_"):
                time.sleep(8 if fault == "postgres_disk_full" else 5)   # pgbench connects / ballast fills
            line = {"fault": fault, "rung": rung, "kind": kind, "injected": ok, "targets": targets}
            if ok and script:
                during = {p: probe(p, script) for p in (pods if every_worker else pods[:1])}
                line["during"] = during
                ok = any(during_ok(r) for r in during.values()) if every_worker else during_ok(during[pods[0]])
        finally:
            cm.delete_all(RUN_ID)
        if script:
            time.sleep(15 if fault.startswith(("postgres_", "mockapi_container")) else 3)
            after = {p: probe(p, script) for p in (pods if every_worker else pods[:1])}
            line["after"] = after
            ok = ok and all(after_ok(r) for r in after.values())
        line["result"] = "PASS" if ok else "FAIL"
        failed += not ok
        print(json.dumps(line), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
