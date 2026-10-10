"""Re-collect the log lines of saved evidence bundles, limited to each experiment's time window.

Bundles written before 2026-10-11 took each pod's last 200 lines regardless of time, so errors from an
earlier experiment (e.g. a disk-full smoke test) leaked into later runs' evidence. Only works while the
pods that ran the experiment still exist. Usage: python scripts/refresh_evidence_logs.py RUN_ID [EXP_ID ...]
"""
import json
import sys
import time
from pathlib import Path

from chaosctl.probes import K8sProbe
from chaosctl.runner import collect_logs

ROOT = Path(__file__).resolve().parents[1]


def main(run_id: str, exps: list[str]) -> None:
    ev_dir = ROOT / "reports" / run_id / "evidence"
    k8s = K8sProbe()
    for f in sorted(ev_dir.glob("exp-*.json")):
        if "." in f.stem or (exps and f.stem not in exps):
            continue
        b = json.loads(f.read_text())
        t_inject = (b.get("timeline") or {}).get("t_inject")
        if not t_inject:
            print(f"{run_id}/{f.stem}: no t_inject, skipped")
            continue
        old = sum(len(v) for v in (b.get("logs") or {}).values())
        b["logs"] = collect_logs(k8s, t_inject - 30, t_inject + 420)
        b["collection_errors"] = list(b.get("collection_errors") or []) + list(k8s.errors)
        b["logs_refreshed_at"] = time.time()
        k8s.errors.clear()
        f.write_text(json.dumps(b, indent=2, default=str))
        print(f"{run_id}/{f.stem}: {old} -> {sum(len(v) for v in b['logs'].values())} log lines (windowed)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
