# End-to-end acceptance test results (run 2: 11-fault catalog)

Test plan: `docs/E2E_TEST_PROMPT.md`. Branch `e2e-results` with `main` at `31b9bc9` merged (PR #3: failure catalog,
new dashboard, AI score). Run 1 (E1/E2 only, 01:52-03:16) is archived in `docs/results/run1/`.

Host: Arch Linux (kernel 7.1.7), x86_64, 16 CPUs, 14 GiB RAM; Docker Engine 29.9.0 (data root on
`/mnt/linuxStorage/docker-data`), kind 0.33.0, Helm 4.3.0, uv 0.11.26. LLM: Mistral `ministral-14b-latest`
(fallback model `ministral-8b-latest`), live API. 2 n8n workers for every experiment.

Results: PASS = met the criterion; FAIL = did not (with why); GAP = feature missing or cannot run.

## Summary (2026-10-11, 04:21-05:55, about 1 h 35 min)

**43 test rows: 39 PASS, 4 FAIL, 0 GAP.** Rebuilt from scratch (one Linux-only setup bug, worked around), every
safety test passed (context refusal, namespace protection, kill switch 9/9 with every chaos resource removed in
≤ 1.6 s, auto-stop on its own, watchdog never needed manual action, no LOCKED state). **All 11 enabled faults ran
end to end** on the live cluster, covering all 6 organizer failure types; Agent 1, Agent 2 and the report ran on
the live Mistral API.

Scope change (user request, 05:02): 7 weaker faults were disabled in `catalog.yaml` (weaker duplicates or no
measurable impact): worker_graceful_kill, worker_container_kill, mockapi_container_kill, mockapi_http_abort,
redis_latency, postgres_latency, packet_loss. One harness change (explained before applying, with unit tests):
`chaosctl run --max-experiments N`, because `--exp <fault>` let Agent 1 plan the same fault 3 times.

### Final ranked resilience scores (AI report over the 11 Phase-5 runs; formula reference in brackets)

**Overall 83.4 / 100, degraded but recovered** (deterministic draft: 84.9). Least resilient first:

| Rank | Fault | Type | Run | Score (Agent 2) | Formula | What happened |
|---|---|---|---|---|---|---|
| 1 | **E11 Postgres pod kill** | crash | r1011-053323 | **31.2 failing** | 35.5 | auto-stopped (p95 24.1 s); of 8 in-flight orders: 3 ok, 1 lost silently, 1 phantom success, 2 lost with error, 1 "success without row" |
| 2 | **E2 Redis pod kill** | crash | r1011-052657 | **37.2 failing** | 40.2 | auto-stopped (p95 24.0 s); Redis back in 1 s but n8n-main 1× and workers 2× restarted; W2 errored too; 3 phantom successes |
| 3 | E5 Mock API HTTP 500 | failed dependency | r1011-050940 | 65.1 fragile | 64.9 | W1 failures from 13 s, back in 48.7 s; 8/8 orders ok |
| 4 | E4 Mock API latency (+2 s) | slow network | r1011-051312 | 75.6 degraded | 84.0 | auto-stopped: 100 % W1 errors (mock-api health probe side effect, see findings) |
| 5 | E10 n8n-main pod kill | crash | r1011-052955 | 93.2 resilient | 93.9 | webhooks down ~10 s, back in 26.4 s; 8/8 ok |
| 6 | E3 Traffic spike (3×) | traffic spike | r1011-051950 | 93.3 resilient | 95.4 | queue to 59, drained in 79.2 s; 9/9 ok |
| 7 | E7 Postgres overload | overloaded DB | r1011-051604 | 97.5 resilient | 97.5 | absorbed (p95 1.13×) |
| 8-10 | E1 Worker pod kill (3 repeats) | crash | r1011-044913 | 97.6 / 97.6 / 97.9 | 97.7-97.9 | no visible symptom, back in ~32 s, but **1 W2 order lost silently every time** |
| 11 | E8 Postgres disk full (4 MiB left) | full disk | r1011-053831 | 98.6 resilient | 98.6 | absorbed |
| 12 | Worker pod failure (hung 30 s) | crash | r1011-050622 | 100 | 100 | absorbed by the second worker |
| 13 | Worker-Redis partition (20 s) | failed dependency | r1011-052352 | 100 | 100 | absorbed by the second worker |

Weakest score components (means): latency and self-recovery drive the failing scores; **detectability was scored
for 0 of 13 experiments**, so the total uses 4 of 5 components.

### Weaknesses found (from the evidence, most severe first) and recommended fixes
1. **Postgres is a single point of failure that loses and corrupts orders.** A 2 s Postgres restart lost 3 of 8
   in-flight orders and told one synchronous customer "OK" for an order that was never saved. *Fixes:* `postgres_ha`,
   `db_connection_pooling`, `idempotency_keys`.
2. **A 1 s Redis restart cascades into a full outage** (n8n-main and both workers restart, W2 stops accepting,
   3 phantom successes). Reproduced on 3 runs on 2 hosts. *Fixes:* `redis_persistence`, `redis_timeout_threshold`,
   `worker_health_probe`.
3. **Silent data loss on a worker crash.** Every worker kill lost exactly 1 async (W2) order that the customer was
   told was accepted, with no error, no latency change and recovery in 32 s: invisible to health checks. *Fixes:*
   `async_status_callback`, `graceful_shutdown`.
4. **Downstream API trouble stops all synchronous orders.** HTTP 500s and +2 s latency on mock-api both made it fail
   its readiness probe (same port, 1 s timeout), so it left the Service and 100 % of W1 calls failed. There is no
   retry on the HTTP node. *Fixes:* `http_retry_backoff`, `http_timeout_budget`; give mock-api a health check that the
   fault proxy does not delay (a separate port).
5. **No headroom for traffic bursts.** A 3× spike built a 59-job backlog that took 79 s to drain. *Fixes:*
   `autoscale_workers`, `queue_depth_alert`.
6. Resilient: n8n-main restart (26 s), Postgres CPU overload, nearly-full disk, a hung worker and a worker cut off
   from Redis were absorbed with 2 workers.

### Failures (next action for each)
1. **T1.2a: `chaosctl up` fails on a Linux host.** `scripts/pg_small_disk.sh` needs `/dev/loopN` inside the Kind node;
   on Linux the node container has only `/dev/loop-control` (Docker Desktop's VM provides the nodes), so the mount
   fails and Postgres is left scaled to 0. A failed run also leaves a half-made image that makes the next run skip the
   data copy (empty database). *Next:* in the script, create missing loop nodes (`losetup -f` / `mknod`) or attach the
   image with `losetup --find --show`, and delete the image if `create` fails part-way.
2. **T6.3: Agent 2 missed 2 of 7 runs with impact.** mockapi_latency (LLM answer rejected for citing unknown evidence
   ids, the fallback blamed Postgres) and traffic_spike (looks like a slow dependency: the evidence has no offered-load
   signal). Also invented "Missing Python 3" for a no-impact run. *Next:* add offered load (requests/s) to the
   evidence; list `ev:baseline.*` ids or reject them earlier in the prompt.
3. **T7.1a: the AI report needs more than the default LLM budget.** `CHAOSCTL_LLM_BUDGET=30` (from `.env.example`)
   is exhausted by a campaign report (~4 calls per experiment since PR #2), giving the deterministic draft. *Next:*
   give `report` its own budget or raise the default; `CHAOSCTL_LLM_BUDGET=90` worked.
4. **T7.4: the AI report has 8 factual errors in its prose** (scores, ranking and fate counts are correct): e.g.
   "did not recover" for auto-stopped runs that the watchdog recovered, mock-api latency "aborted after 180 s"
   (it was 29.9 s), "single-worker deployments" (all runs had 2 workers), "Postgres overload caused queue backlogs"
   (queue 0), and a speculative race-condition cause stated as fact. *Next:* add key facts for watchdog outcome,
   worker count and abort time; extend the fact-check to claims, not only numbers; review the report by hand.

### Other findings (not test failures)
- **HTTPChaos also breaks the target's readiness probe** (k8s: 19 × "Readiness probe failed … context deadline
  exceeded" on mock-api `/healthz` during the latency fault; Down 15.8-30.9 s), so "HTTP 500" and "+2 s latency"
  behave like "mock-api down". The faults' results are real, but they do not test what their names say.
- **Agent 1 ignores constraints.** With `--exp main_pod_kill` Mistral planned other faults and an invented rung
  ("persistent"); the validator rejected the plan and the deterministic plan ran, flagged loudly (works as designed).
  With `--exp worker_pod_kill` it planned 3 repeats of the same fault (fixed for runs with `--max-experiments 1`).
  Hypotheses assume behaviour the system does not have (HTTP retries, Redis persistence).
- **`--exp` always uses the gentlest rung**, so the harsh rungs (disk `full`, `hold85`, `x5`, latency +30 s) were not
  run as full experiments; several faults were absorbed at their gentlest rung.
- The RAG chat (`POST /api/chat`) mixes yesterday's documented Mac numbers into answers about today's runs, missed
  the Postgres-kill run when asked about it, and its panel is no longer on the dashboard page.
- The dashboard keeps a stale red "LLM disabled by --no-llm" banner from an earlier command during later LLM runs,
  and the rebuilt dashboard has no light theme.
- `scripts/llm_smoke.py` is answered from the LLM cache after its first run, so it cannot detect a dead API.
- The "reordered by tier rule" message prints experiment ids that do not exist in the final plan.
- The wrong-context refusal is still a Python traceback, not a one-line message.
- Kill-switch test: with 2 workers the fault was visible before the trigger in only 1 of 9 runs.

### Is the system demo-ready?
**Yes.** It builds from scratch (with the Linux loop-device workaround), safety holds, all 11 faults across the 6
failure types run with real measurements, Agent 1 and Agent 2 run live on Mistral, and the AI report produces a
ranked resilience score (Postgres kill 31, Redis kill 37 at the bottom). The scripted demo (`demo --queue-abort 20`)
now auto-stops and fits in about 2 minutes. Before recording: (1) fix or document the loop-device step for Linux
hosts, (2) run `report` with a larger LLM budget and correct the report's prose errors by hand (failure 4) before
showing it, (3) clear stale dashboard banners (`state/degraded.jsonl`) left by test commands.

## Phase 0: Preflight (2026-10-11 04:21)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T0.1 | Tools | versions | docker (≥ 6 GB), kind, kubectl, helm, uv | docker 29.9.0 (no memory cap; 14 GiB host RAM, ~6 GiB available), kind v0.33.0, kubectl v1.37.1, helm v4.3.0, uv 0.11.26; 16 CPUs, load 1.7 | shell output | PASS |
| T0.2 | Host limits | `sysctl fs.inotify.*`, ports | ≥ 512 / 524288; ports free | inotify 1024 / 524288. Ports held by the old cluster until T1.1 deleted it; all four free afterwards (see T1.1) | shell output | PASS |
| T0.3 | Unit tests | `uv sync && uv run pytest` | all pass | **210 passed** in 10.6 s | pytest output | PASS |
| T0.4 | Knowledge base | `uv run chaosctl kb` | every fault with alias; consistent | 22 catalog faults, 22 knowledge files: **18 enabled** (aliases E1-E11 on 11 of them, 7 without alias), 4 off (worker_cpu_stress, worker_memory_stress, worker_clock_skew test-only/disabled; pod_failure test-only). "knowledge base consistent" | kb output | PASS |
| T0.5 | LLM | `scripts/llm_smoke.py` | Mistral answers valid JSON | The smoke prompt was answered from the LLM cache (`source: cache`, identical prompt to the 01:53 run), which proves nothing about the API. Same `generate_json` call with a fresh seed (cache key differs): `source: api`, `ministral-14b-latest`, valid JSON in 1.50 s | smoke output + one-off call | PASS (smoke script alone cannot detect a dead API once cached) |

## Phase 1: Clean build from scratch (04:21-04:36, 15 min)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T1.1 | Cluster | `kind delete cluster --name chaos`; `chaosctl cluster-up` | all pods Running; Chaos Mesh ready; n8n-chaos annotated | Delete 04:21:07, ports 15678/16379/15432/18080 free. cluster-up 04:21:12-04:29:02 (**7 min 50 s**, exit 0); Chaos Mesh controller/daemon/dashboard Running; namespace annotated | /tmp/p1-run2.log | PASS |
| T1.2a | App (as written) | `chaosctl up` | postgres, redis, mock-api, n8n Ready | **up exited 32 after 38 s**: new step `scripts/pg_small_disk.sh` scaled Postgres to 0, then `mount: /mnt/pgdisk-init: failed to setup loop device for /var/lib/chaosctl/pgdisk.img`, leaving Postgres stopped. Root cause: on a Linux host the Kind node container has `/dev/loop-control` but no `/dev/loopN` block devices (`losetup -f` → "/dev/loop0 (lost)"); the host's `loop` module (built as a module, `CONFIG_BLK_DEV_LOOP=m`) was not loaded when the node container was created. Docker Desktop's VM (where the script was written) provides them | /tmp/p1-run2.log | **FAIL** (README does not work on a Linux host) |
| T1.2b | App (workaround) | in the Kind node: `mknod /dev/loop0..3 b 7 N`; removed the half-made `pgdisk.img` (otherwise the script takes "remount" mode and skips copying Postgres's data, giving an empty database); re-ran `scripts/up.sh baseline` (04:32:51-04:33:38) | same | up exit 0 in 47 s; "postgres data now on a 498900 KB filesystem"; all pods Running | /tmp/up-retry.log | PASS (with workaround) |
| T1.2c | Bootstrap | `chaosctl bootstrap` (04:33:48-04:34:53) | W1 200 + row; W2 200 + row later | W1 HTTP 200 in 4.72 s, 1 row; W2 HTTP 200 in 0.024 s, 1 row | /tmp/boot2.log | PASS |
| T1.3 | Post-setup | README steps 4-5 | worker count; Grafana/Prometheus 0 | n8n-worker **2/2**; Grafana and Prometheus 0 pods; every pod Running | kubectl | PASS |
| T1.4 | Graph | `chaosctl graph` | 5 services; env + overlay edges; redis + postgres SPOF | 5 services + client (+ grafana/prometheus nodes). Edges n8n-main/worker→redis/postgres `k8s_env+overlay`, others `overlay`. SPOF: postgres, redis, mock-api, n8n-main (1 replica each); n8n-worker (2) not a SPOF | graph.json | PASS |

## Phase 2: Baseline (04:36:37-04:38:41)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T2.1 | Baseline | `chaosctl baseline --duration 120` | W1 ≥ 99 %, p95 recorded, queue trend ≈ 0 | 2 req/s, 2 workers. W1 105/105 (100 %), p50 4048.0 ms, p95 **4055.6 ms**. W2 100/100, p50 20.6 ms, p95 26.7 ms. Queue wait mean 0, max 0, trend 0; active mean 8.31 | state/baseline.json | PASS |

## Phase 3: Safety (04:39-04:47)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T3.1 | Context refusal | throwaway context `e2e-dummy` (dead server); `chaosctl graph`, `chaosctl run --exp E1` | refuses | both: `WrongCluster: current kube context is 'e2e-dummy', expected 'kind-chaos'` (still as a Python traceback); no chaos created; context restored | command output | PASS |
| T3.2 | Namespace protection | PodChaos pod-kill in un-annotated namespace | selects nothing | same pod, 0 restarts after 15 s; Chaos Mesh event "Failed to select targets: no pod is selected"; cleaned up | command output | PASS |
| T3.3 | Kill switch | `chaosctl killswitch-test` (04:39:41-04:42:59) | CR removed ≤ 3 s; worker Ready ≤ 60 s | **9/9**. CR deleted after trigger (s): cli 1.5/1.6/1.5, file 1.5/1.5/1.6, signal 1.6/1.5/1.5 (all ≤ 1.6 s). Worker Ready: ≤ 1.6 s in 7 runs, 24.9 s (file 1), 18.8 s (signal 1). Fault visible before the trigger in only 1/9 runs (2 workers mask a 10 s pod-failure). Afterwards each worker had 7 restarts and one was 0/1 Ready → `rollout restart deploy/n8n-worker` | reports/killswitch-test.json | PASS |
| T3.4 | Auto-stop | `chaosctl demo --queue-abort 20` (04:45:22-04:47:12) | controller aborts by itself, removes fault, watchdog OK | Run r1011-044522: Agent 1 (`llm:cache:ministral-14b-latest`) led with **mockapi_http_500** (code500_d30). Injected with 4 W1 + 4 W2 in flight; first symptom 13.3 s; **aborted by the controller `queue_waiting 21 > 20` at 19.8 s**; CR deleted 0.7 s later; watchdog `recovered: True, manual_action_needed: False`; restarts 0; fates 8/8 completed once; Agent 2 graded CORRECT. During the fault mock-api's readiness probe (`/healthz`, same port 8080, 1 s timeout) timed out (k8s event 04:46:41), so the pod briefly went not-Ready: the HTTPChaos proxy also hits the health check | reports/r1011-044522, k8s events | PASS |
| T3.5 | LOCKED | `chaosctl recover --ack` | clears LOCKED | no run ended LOCKED (checked again after Phase 5) | state/ | PASS |

## Phase 4: Agent 1 (04:40)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T4.1 | Agent 1 (LLM) | `chaosctl plan --dry-run` | source llm; real graph ids; tier order + gate; every enabled fault a candidate | Source `llm:api:ministral-14b-latest`. Candidates: **35 (fault, rung) pairs covering all 18 enabled faults**. Plan (cap 6): mockapi_http_500 (code500_d30), worker_pod_failure (d30), packet_loss redis (loss10), postgres_latency (l100ms), traffic_spike (x3), mockapi_latency (plus2s): 5 services, tier 1 before tier 2, all approved, **0 unknown citations**. No tier-3 fault planned, so the tier-3 gate is not exercised here (checked at run time in Phase 5). Display bug: "reordered by tier rule: exp-03 … exp-08" uses ids that do not exist in the printed plan (exp-01 … exp-06). Hypothesis for the HTTP 500 fault assumes n8n "will retry failed HTTP requests", but the workflows' HTTP node has no `retryOnFail` (FACTS) | /tmp/plan2.log, reports/plans/journal.jsonl | PASS |
| T4.2 | Fallback | `chaosctl plan --dry-run --no-llm` | valid deterministic plan | Source "DETERMINISTIC FALLBACK - the LLM did not choose this plan", flagged "Warning: 1 problem(s) … NOT fully normal". 6 experiments (container kills, http 500, packet loss on postgres and redis, traffic spike), tier order kept, all approved. The fallback leaves a WARNING banner on the dashboard ("LLM disabled by --no-llm") that stays visible during later runs whose plan did come from the LLM | /tmp/plan2b.log, dashboard screenshot | PASS (stale banner noted) |

## Phase 5: Every enabled fault, end to end (04:49-, default limits, 2 workers)

Scope change during the run (user request, 05:02): 7 weaker faults disabled in `catalog.yaml` (`enabled: false` with a
comment): worker_graceful_kill, worker_container_kill, mockapi_container_kill, mockapi_http_abort, redis_latency,
postgres_latency, packet_loss. **11 enabled faults remain, covering all 6 organizer failure types.** `chaosctl kb`
consistent, tests pass.

Harness change during the run (explained before applying): with `--exp <fault>` Agent 1 filled the plan with **repeats
of the same fault** (r1011-044913: worker_pod_kill × 3, cooldowns 120/180/300 s, 15 min for one fault). `run` had no
cap (`demo` caps at 1 internally). Added an opt-in `chaosctl run --max-experiments N` (default unchanged) passing the
campaign's existing `max_experiments`; 2 unit tests (`tests/test_cli_run.py`); 212 tests pass. All later runs use
`--max-experiments 1`.

| ID | Fault (rung) | Run | Plan source | Status | Observed (real numbers) | In-flight fates | Agent 2 (Phase 6) | Result |
|---|---|---|---|---|---|---|---|---|
| T5.1 | E1 worker_pod_kill (ungraceful) | r1011-044913 (3 experiments, see above) | llm:api:ministral-14b-latest | completed ×3 | No SLI symptom in any repeat (t_first_symptom None); replacement Ready / SLIs back **32.5 s, 32.4 s, 31.5 s**; restarts 0 | **1 W2 lost silently in every repeat** (8+1, 7+1, 7+1 completed once + lost silently) | CORRECT ×3 (14b, 14b, 8b fallback model); exp-01 also recommended redis_persistence and postgres_ha (irrelevant) | PASS |
| T5.2 | worker_pod_failure (d30) | r1011-050622 | llm:api | completed | Pod "Ready" at 1.0 s (pause image), SLIs back 30.4 s, n8n-worker +4 restarts, no SLI symptom | 9/9 completed once | NOT GRADED (NO_IMPACT); root cause invented from a log line: "Missing Python 3 in n8n-worker pods" | PASS (no impact) |
| T5.3 | E5 mockapi_http_500 (code500_d30) | r1011-050940 | llm:api | completed | First symptom 13.2 s, SLIs back 48.7 s; restarts 0 | 8/8 completed once | CORRECT ("mock-api pod unreadiness caused HTTP call timeouts") | PASS |
| T5.4 | E4 mockapi_latency (**plus2s**, the gentlest rung) | r1011-051312 | llm:api | **aborted** `w1_error_rate 1.00 > 0.5` | First symptom 13.2 s; **mock-api not Ready 15.8-30.9 s** (16 samples): the HTTPChaos proxy makes the readiness probe (`/healthz`, same port, 1 s timeout) time out (k8s: 19 × "Readiness probe failed … context deadline exceeded"), so mock-api leaves the Service and every W1 call fails. +2 s on a 4 s call with a 30 s timeout should not fail anything | 8/8 completed once | **MISSED, fallback**: LLM answer rejected (cited unknown evidence `ev:baseline.w1`), template blamed Postgres ("database errors in the logs", none in the logs) | PASS (run valid; impact is the probe side effect, see findings) |
| T5.5 | E7 postgres_overload (cpu4) | r1011-051604 | llm:api | completed | No SLI symptom; queue max 0, W1 p95 max 4.57 s (1.13×); SLIs back 56.9 s (after the 45 s fault window) | 8/8 completed once | NOT GRADED (NO_IMPACT); Agent 2: "No real degradation", no fix (correct behaviour) | PASS (absorbed) |
| T5.6 | E3 traffic_spike (x3: 2 → 6 req/s for 60 s) | r1011-051950 | llm:api | completed | First symptom 8.2 s; **queue waiting max 59**; W1 p95 max 4.69 s; backlog drained and SLIs back **79.2 s** | 9/9 completed once | **MISSED**: "Transient dependency (mock-api or postgres) slowdown"; the evidence has no offered-load signal, so a spike looks like a slow dependency (known from PROGRESS) | PASS (run valid; diagnosis miss) |
| T5.7 | worker_redis_partition (d20) | r1011-052352 | llm:api | completed | No SLI symptom, queue 0, W1 p95 max 4.34 s, no W2 errors; SLIs back 20.3 s (fault window) | 8/8 completed once | NOT GRADED (NO_IMPACT); "No degradation" (correct) | PASS (absorbed by the second worker) |
| T5.8 | E2 redis_pod_kill (ungraceful) | r1011-052657 | llm:api | **aborted** `w1_p95_ms 23958 > 20000` | Runtime guard: "tier 3 allowed: tier 1 experiment exp-01 passed". Redis Ready 1.0 s, but n8n-main restarted 1×, workers 2×; n8n-main unready 12.4-28.5 s, workers 12.4-36.6 s; first symptom 11.3 s; W1 p95 max 24.0 s; **W2 errors in 22/35 samples**; SLIs not recovered within the run | 5 completed once, **3 phantom success** | CORRECT ("Redis pod restart caused a dependency failure for n8n-main and n8n-worker…"); fixes redis_persistence, redis_timeout_threshold, worker_health_probe | PASS |
| T5.9 | E10 main_pod_kill (ungraceful) | r1011-052955 | **fallback:invalid_plan** | completed | Restricted to main_pod_kill, Mistral (24.9 s) still planned other faults (redis_pod_kill; mockapi_http_500 with an invented rung "persistent"); the validator rejected the plan and the deterministic plan ran, flagged as "LLM NOT USED". Impact: **webhooks down**: n8n-main unready 0.7-9.8 s, replacement Ready 11.2 s, W1 errors in 10/56 and W2 errors in 11/56 samples, W1 p95 max 6.56 s; SLIs back **26.4 s** | 8/8 completed once | CORRECT ("n8n-main pod restart disrupted webhook processing…"); fixes webhook_processors, redis_timeout_threshold, queue_depth_alert | PASS (plan validator caught an invalid LLM plan) |
| T5.10 | E11 postgres_pod_kill (ungraceful) | r1011-053323 | llm:api | **aborted** `w1_p95_ms 24062 > 20000` | Postgres Ready 2.0 s (unready in one sample at 0.3 s); first symptom 1.0 s; W1 errors in 8/22 and W2 errors in 8/22 samples; W1 p95 max 24.1 s; no restarts; watchdog recovered, no manual action | **3 completed once, 1 lost silently, 1 phantom success, 2 lost visibly, 1 success without row** (client told OK, no order row) | CORRECT ("Postgres pod restart caused database unavailability…"); fixes postgres_ha, db_connection_pooling, autoscale_workers | PASS |
| T5.11 | E8 postgres_disk_full (nearly_full) | r1011-053831 | llm:api | completed | Injection verified by the Job ("disk now 99% used, 4756 KB free"); no SLI symptom, W1 p95 max 4.11 s; SLIs back 55.8 s (fault window 40 s). The `full` rung (`No space left on device`) is not reachable with `--exp` (gentlest rung only) | 8/8 completed once | NOT GRADED (NO_IMPACT); "No degradation" (correct) | PASS (absorbed at 4 MiB free) |

Checks for every Phase 5 run (T5.x.2, T5.x.3, T5.x.5): all 11 runs have journal.jsonl, results.json,
evidence/exp-01.json, exp-01.plan.json (and exp-01.diagnosis.json after Phase 6); fate total = in-flight count in
11/11; after every run all 6 pods were Running/Ready and no `chaosctl=managed` chaos resource or Job was left
(the loop's "chaos left: 1" was the "No resources found" line being counted). Phase 5 took 04:49-05:42 (53 min).

## Phase 6: Agent 2, blind diagnosis (run alongside Phase 5)

| ID | Area | Expected | Observed | Result |
|---|---|---|---|---|
| T6.1 | Source | llm, not fallback | 13 of 14 diagnoses from the LLM (`llm:api:ministral-14b-latest`, one on the fallback model `ministral-8b-latest`); 1 fallback: mockapi_latency (LLM cited unknown evidence `ev:baseline.w1`/`w2`, rejected by the validator; labelled, graded as "does not count as an Agent 2 result") | PASS (1 fallback, labelled) |
| T6.2 | Fixes | from playbook.yaml only | every recommended fix id is in playbook.yaml (validator); NO_IMPACT runs returned no fix | PASS |
| T6.3 | Grading | CORRECT | Runs with impact: **5 of 7 CORRECT** (worker_pod_kill ×3 repeats, mockapi_http_500 ×2 incl. the demo run, redis_pod_kill, main_pod_kill, postgres_pod_kill); **MISSED**: mockapi_latency (fallback blamed Postgres), traffic_spike (blamed a slow dependency). NO_IMPACT (not graded): worker_pod_failure, postgres_overload, worker_redis_partition, postgres_disk_full; 3 of 4 correctly said "no degradation", worker_pod_failure invented "Missing Python 3" | FAIL (2 misses) |
| T6.y | Blindness | no Chaos Mesh event/log line in Agent 2's input | 0 Chaos Mesh mentions in all 14 diagnoses; `diagnosis.blind()` strips them before the LLM call | PASS |

## Phase 8: Dashboard and chat

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T8.1 | Dashboard live | new `chaosctl dashboard` (chaosctl/dash) during `chaosctl demo` r1011-044522 | phase strip advances; map shows target + blast radius; charts move; panels fill; screenshots light + dark | Seen live: phase strip "2 Baseline" with "arms after warm-up" → "5 Impact and auto-stop" ("watching impact; safety limits armed"); zoned system map with "NEXT FAULT: mockapi http 500" during warm-up and "FAULT: mockapi http 500, pod mock-api-…, T+17 s, 8 in flight at injection" while observing; amber blast radius on n8n-main + n8n-worker; mock-api derived state **Down** ("no Ready pod; mock-api admin probe unreachable"), n8n-worker Degraded with "first symptom T+13 s"; W2 strip "silent: clients still get 200 while n8n-worker, mock-api is impaired". After `chaosctl report`: "Resilience score and ranking" panel (see T7). Issue: a **stale red WARNING banner** from an earlier `plan --no-llm` ("LLM disabled by --no-llm; using the deterministic plan") stays on screen during later runs whose plan came from the LLM | docs/results/dashboard-dark-*.jpg (3) | PASS (stale banner noted; **no light screenshots: the rebuilt dashboard (chaosctl/dash) has no light theme**, the old one followed `prefers-color-scheme`) |
| T8.2 | Ask Agent 2 (RAG) | `POST /api/chat` (the chat panel is no longer on the page) | answers from Mistral citing existing sources | 3/3 `llm:api:ministral-14b-latest`, sources exist. Accuracy: Q1 "Why were orders affected when Redis restarted?" mixes yesterday's Mac numbers from JUDGES_GUIDE ("14 phantom successes, 10 lost, 60-second outage") with today's run (3 phantom successes). Q2 "What happened to in-flight orders when Postgres was killed?": "the provided sources do not contain information": retrieval did not return r1011-053323. Q3 "Which fault lost an async W2 order silently?": worker_pod_kill (correct) but "no other faults" (postgres_pod_kill also lost 1) and cites a run-1 test run | curl output | PASS (criterion met; 3 accuracy problems) |

## Phase 9: Demo rehearsal

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T9.1 | Demo, timed | `chaosctl demo --queue-abort 20` = DEMO.md step 3 (the T3.4 run, timed from its journal) | total time; fits 3 min | **1 min 51 s** (04:45:21-04:47:12): plan 1 s (cached LLM), warm-up 60 s, inject 04:46:24, **auto-stop at +19 s**, recovered 04:46:46, Agent 2 diagnosis (LLM 13.7 s) and printing ~26 s. Plus `chaosctl graph` (< 1 s) and the off-camera baseline (2 min). With an auto-stopping fault the demo fits 3 minutes with the warm-up cut or narrated; the dashboard shows the fault, blast radius, Down/Degraded states and the W2 "silent" strip live. Agent 1 leads with mockapi_http_500 now (not E1), so the demo shows a dependency failure and the auto-stop. Before recording: clear `state/degraded.jsonl` banners from test commands | reports/r1011-044522 | PASS |

## Phase 7: AI-written findings report and ranked score (05:42-05:53)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T7.1a | AI report (as configured) | `chaosctl report --run <11 Phase-5 runs>` (05:42-05:47) | "Written by Agent 2" | **Deterministic draft**: "AI writer unavailable: **call budget of 30 exhausted**". `.env` sets `CHAOSCTL_LLM_BUDGET=30`; since PR #2 the report needs ~1 analysis + 3 scoring calls per experiment + a summary (13 experiments ≈ 53 calls), so the default budget cannot produce an AI report for a full campaign | docs/results/findings-deterministic.md | FAIL (budget too small for a campaign report) |
| T7.1b | AI report (budget raised for one command) | `CHAOSCTL_LLM_BUDGET=90 chaosctl report --run …` (05:47-05:53, 6 min) | "Written by Agent 2"; every experiment; ranking | "Written and scored by Agent 2 (ministral-14b-latest)"; **13 experiments** (E1's 3 repeats each scored); median of 3 judgements each; **overall 83.4 / 100 (degraded but recovered)**, worst 31.2; ranking below; 6 weaknesses ranked by severity, 7+ fixes from the playbook | docs/results/findings-ai.md, .json | PASS |
| T7.2 | Fact-check flags | `unverified_numbers` | empty or checked by hand | 1 flag: r1011-051604 "12.7" in "4571.6 ms vs baseline 4055.6 ms, +12.7 %" → (4571.6 − 4055.6) / 4055.6 = 12.7 %: **correct** (false positive) | findings-ai.json | PASS |
| T7.3 | Cross-check | AI vs formula > 20 points | list and judge | **No experiment differs by more than 20 points** (largest: mockapi_latency 75.6 vs 84.0, 8.4 points; Redis 37.2 vs 40.2; Postgres kill 31.2 vs 35.5). The AI scores track the formula closely since PR #2 (formula given as a reference) | report section 1 | PASS |
| T7.4 | Report vs evidence | read findings.md against results.json / evidence | no contradictions | **8 factual errors**, all in prose (scores, ranking and fate counts are right): (1) exec summary "mock-api latency and Postgres/Redis failures did not [recover]": those runs were auto-stopped; recovery is not measured after an abort, and the watchdog recovered every one with no manual action; (2) mockapi_latency "aborted after 180 seconds … (6/41 > 0.5)": aborted at **29.9 s** on `w1_error_rate 1.00`; 6/41 = 0.15; (3) same section "did not recover … due to unresolved queue backlog and database errors": no database errors in the evidence; (4) exec summary "Postgres overload caused queue backlogs": queue max 0; (5) weakness 5 and fix 4 "single-worker deployments caused 59 waiting jobs": every run had **2 workers**; (6) weakness 2 "queue backlogs (15 pending jobs)" for the Postgres kill: measured queue max 21, W1 pending max 22; (7) weakness 4 "W2 clients always received 200 OK", citing the n8n-main kill run where W2 errored in 11/56 samples; (8) weakness 1 explains the worker-kill silent loss as "Redis marking jobs completed before workers wrote to Postgres", which no evidence shows (speculation presented as fact). Postgres-kill summary omits the 1 "success without row" | docs/results/findings-ai.md | FAIL (8 claims contradict or exceed the evidence) |
| T7.5 | Deterministic draft | (T7.1a, same command) | labelled draft | "Deterministic draft, not AI-written (AI writer unavailable: call budget of 30 exhausted)"; formula ranking: Postgres kill 35.5, Redis kill 40.2, HTTP 500 64.9, latency 84.0, main kill 93.9, traffic spike 95.4 … partition 100; overall 84.9 | docs/results/findings-deterministic.md | PASS |
