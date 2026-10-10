# Progress log

## Gates
| # | Status |
|---|---|
| G1 n8n license | CLOSED: organizers confirmed n8n is fine. List Sustainable Use License in README + team form. |
| G2 K8s go/no-go | OPEN: clock starts at T1a. Team chose the full Kind plan despite 8 GB host RAM (see G3). |
| G3 Resources | Host: macOS 15.7.1, Apple Silicon (arm64), **8 GB RAM total**; Docker Desktop 28.1.1 with 8 CPUs and **3.8 GB memory** at start. Below the 8-12 GB the plan assumes. Decision (2026-10-10): proceed with full Kind plan; raise Docker Desktop memory; trim monitoring (no Alertmanager, short retention). Watch for OOM kills; Compose fallback remains the escape hatch. |
| G5 n8n version | OPEN (T1b). |
| G7 Hosted LLM OK? | OPEN: ask organizers whether Gemini (hosted) is acceptable vs "open-source model". |
| G8 Gemini quota | OPEN: needs API key; check RPM/RPD in AI Studio. |

## Phase 0 (2026-10-10)
- Tools: kubectl v1.32.2 (client), kind v0.33.0 and Helm v4.3.0 installed via Homebrew (plan said Helm >= 3.12; Helm 4 used), uv 0.12.9.
- Python: project venv via uv. Plan suggested 3.11+; all deps install on the host's Python 3.14.7, so 3.14 is used (`requires-python >=3.12`).
- Deps: langgraph 1.2.14, google-genai 2.29.0, pydantic 2.14.0, typer 0.27.3, kubernetes 37.0.1, httpx 0.28.1, redis 8.1.0, psycopg 3.3.6, pytest 9.1.1.
- `chaosctl/llm.py` written: cache, retries, fallback model, call budget, redaction. `tests/test_llm.py` 7/7 pass (offline, fake transport).
- Gemini live smoke test: PASSED with gemini-3.8-flash (structured JSON validated, 8.9 s first call). Run: `PYTHONPATH=. uv run python scripts/gemini_smoke.py`.

## T1a (2026-10-10) DONE
- `scripts/cluster-up.sh`: Kind `chaos` (k8s 1.37.0) with NodePorts on 127.0.0.1: n8n 15678, redis 16379, postgres 15432, mock-api 18080 (1xxxx host ports avoid clashing with a local Homebrew Postgres). kube-prometheus-stack 92.3.0 (no Alertmanager, 6 h retention, trimmed requests), Chaos Mesh 2.8.4 (containerd, FilterNamespace, 1 replica, no leader election, no DNS server). Namespace `n8n-chaos` annotated.
- Acceptance: all pods Running; Chaos dashboard HTTP 200; Grafana healthy; pod-failure on a dummy Deployment injected and auto-restored after 20 s; un-annotated namespace not selected. Memory after install: ~3.1 GiB of 5.8 GiB.
- Slow part: in-cluster image pulls from ghcr.io (~10 min).

## T2/T3/T4b/T5/T6 code (written ahead of the cluster, against fixtures)
- graph.py + overlay.yaml (T2), catalog.yaml + chaos/chaosmesh.py (T3), safety.py (T4b), loadgen.py/probes.py (T4a), agents/injection.py (T5/T6), runner.py, cli.py. 55 unit tests pass. Live acceptance pending on the n8n stack.
- Decision: the injection agent's guard re-checks the tier rule at run time (`runtime_guard`), so E2 is only approved after E1 actually passed.

## T1b (2026-10-10) DONE
- `chaosctl up` + `bootstrap`: postgres, redis (no persistence), mock-api, n8n 2.42.4 queue mode (1 worker). W1 200 + row, W2 200 + row later, W3 heartbeat rows. n8n 2.x uses `n8n publish:workflow --id` (not `update:workflow --active`). Bull prefix `bull` confirmed (`bull:jobs:*`).
- Docker Desktop crashed once (daemon gone) during the first deploy on the 8 GB host; restarted, cluster recovered, redeploy succeeded. Memory after deploy ~4.1/5.8 GiB.

## T2 live: graph from the real cluster finds n8n-main/worker -> redis/postgres edges via ConfigMap env (source k8s_env+overlay).

## T4a baseline (2026-10-10)
- 3 req/s oversubscribed one worker (active ~10 = concurrency, queue trend +37). Steady lowered to **2 req/s**: W1 100 % success, p50 4.1 s, p95 4.7 s; W2 p95 0.1 s; queue flat (mean 0.01), ~8 active. Default abort thresholds kept (p95 20 s is ~4x baseline).

## T4b kill switch (2026-10-10): 7/9 within the 60 s criterion
- Chaos resource deleted within 1-3 s in all 9 runs (CLI, ./KILL, SIGINT x3). Two runs failed the 60 s worker-Ready criterion (59 s, 105 s): nine consecutive pod-failures on the same pod pushed the worker into CrashLoopBackOff ("exec: n8n not found" while the pause image was swapped in). Finding: repeated faults on one pod inflate recovery via kubelet back-off. Restored with a rollout restart.

## T5/T6 live: `plan --dry-run` with Gemini returns a valid plan citing real graph ids; guard orders E1 before E2 and gates E2 on a passed E1.

## T7 E1 worker kill (run r1010-155337)
- Injected with 5 W1 + 5 W2 in flight, 8 active. Active dropped to 0, wait rose 0 -> ~52, W1 p95 4.7 s -> 29.8 s, W2 kept returning 200 (silent). Replacement worker Ready at +34 s. **All 10 in-flight requests completed exactly once** (no loss, no duplicates; hypothesis of duplicates refuted). Backlog (~50) did not drain under continued 2 req/s load within 180 s: no headroom with one worker.
- Bug found and fixed: observe window and safety cap both 180 s raced, labelling the run "aborted"; observe now ends 5 s before the cap.
- Warm-up raised from 10 s to 60 s per BUILD_PLAN 11.1.

## Auto-stop rehearsal (run r1010-155845): E1 with queue_waiting abort = 20
- Controller tripped on "queue_waiting 22 > 20" within seconds; load stopped, chaos deleted, watchdog confirmed all workloads Ready without manual action. Fates: 6 completed once, 1 lost silently (W2), 1 lost visibly (W1). Cause not yet established.

## Live dashboard: `chaosctl dashboard` -> http://127.0.0.1:8787 (service tiles, W1 p95 vs baseline, throughput W1 vs W2, queue waiting vs active, event markers, experiment panel, fate table).

## Agent 2 (diagnosis) + dashboard panel (2026-10-10)
- `chaosctl diagnose`: blind (Chaos Mesh events/log lines stripped), deterministic detectors, Gemini reasoning steps (observe/hypothesize/check/conclude), root cause, failure chain, fixes only from `playbook.yaml`; advisory only, never changes the system. Grades itself afterwards against the plan. On the first E1 run: root cause CORRECT ("single worker"), top fix "run at least 2 workers".
- Fixed during testing: blindness leak (Chaos Mesh events visible to Agent 2), false-positive `queue_state_lost` on E1.

## E1 re-runs (2026-10-10)
- r1010-160741 (1 worker): auto-stopped by the safety controller, `w1_p95_ms 24044 > 20000` after the 90 s grace: backlog never drains with one worker. Tier rule therefore keeps E2 locked.
- Engineer applied Agent 2's top fix by hand: `kubectl scale deploy/n8n-worker --replicas=2`.
- r1010-161435: E1 skipped, `baseline_unstable` (pre-flight refused to inject; n8n-main had Postgres connection timeouts).
- r1010-162355: DISCARDED as invalid: host CPU saturated (load average 46 on 8 cores; Grafana 52 %, Prometheus 38 %, kube-apiserver 43 % from 1 Hz probes), system already degraded before injection.
- Mitigation: Grafana and Prometheus scaled to 0 (not used by the demo; our dashboard replaces them), pod probe now uses one API call instead of five. 2-worker baseline after: W1 100 %, p95 6.0 s, queue 0.
- Judge guide: docs/JUDGES_GUIDE.md.
