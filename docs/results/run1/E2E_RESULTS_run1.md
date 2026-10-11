# End-to-end acceptance test results

Test plan: `docs/E2E_TEST_PROMPT.md`. Branch `e2e-results`, based on `main` at `1258824` (PR #1 merged).

Host: Arch Linux (kernel 7.1.7), x86_64, 16 CPUs, 14 GiB RAM (about 4 GiB free at the start; no VM, so containers
share host memory). Docker Engine 29.9.0 (data root on `/mnt/linuxStorage/docker-data`), Python env via uv.

Results: PASS = met the criterion; FAIL = did not (with why); GAP = feature missing or cannot run.

## Summary (2026-10-11, 01:52-03:16, about 1 h 25 min)

**43 test rows: 37 PASS, 5 FAIL, 1 GAP.** All safety tests passed (context refusal, namespace protection, kill
switch 9/9, auto-stop when its limit is crossed, watchdog recovery with no manual action, no LOCKED state). Every AI
step ran on Mistral (`ministral-14b-latest`), not the fallback. Both enabled faults ran end to end on a cluster
rebuilt from scratch.

**Final ranked resilience scores** (AI report over 5 runs, overall **55 / 100, fragile**; formula cross-check in brackets):

| Rank | Run | Fault | Agent 2 score | Formula | Note |
|---|---|---|---|---|---|
| 1 | r1011-025735 | E2 Redis pod kill | **35** failing | 42.2 | Phase 5 run, default limits; auto-stopped `w1_p95 23.4 s > 20 s` |
| 2 | r1011-024619 | E1 worker pod kill | 62 fragile | 85.2 | T3.4c test run: 1 worker, artificial limit queue > 3 |
| 3 | r1011-024059 | E1 worker pod kill | 77 degraded | 88.1 | T3.4b test run: 1 worker |
| 4 | r1011-023307 | E1 worker pod kill | 80 degraded | 95.6 | T3.4a demo run, 2 workers |
| 5 | r1011-025233 | E1 worker pod kill | **88** resilient | 95.6 | Phase 5 run, default limits, 2 workers |

On the clean Phase 5 runs alone: **Redis kill 35, worker kill 88**. Weakest component overall: detectability (mean 20).

### Failures (next action for each)
1. **T3.4a/b: the scripted auto-stop demo does not stop.** `demo --queue-abort 20` never crosses the limit on this
   host: queue max 0 with 2 workers, 6 with 1 worker (the 8 GB Mac reached ~52). The controller itself works (T3.4c
   tripped on `queue_waiting 4 > 3` in 3.6 s; E2 tripped on p95 by itself). *Next:* set the demo's `--queue-abort`
   from the host's measured peak, or show the auto-stop with E2; update DEMO.md / JUDGES_GUIDE.md.
2. **T5.1.4: E1 impact does not match `knowledge/worker_pod_kill.yaml`.** With 2 workers neither expected symptom
   (`consumers_down`, `latency_degradation`) appears (queue 0, p95 1.13× baseline). *Next (knowledge-base owners):*
   state that the expected symptoms assume 1 worker, or add 2-worker expectations. Not edited here (teammates own it).
3. **T5.2.1a: `chaosctl run --exp E2` silently does nothing.** Root cause: `campaign.py` `plan()` filters Agent 1's
   plan by `--exp` *after* Agent 1 returns, and Agent 1 is never told the requested fault. Mistral planned only E1, so
   the plan became empty: exit 0, no message, empty results.json. *Next (harness fix, needs your OK):* pass the
   restriction into Agent 1 (score/offer only the requested faults) or fall back to the deterministic plan for a
   requested fault the LLM left out; print a skip reason and exit non-zero on an empty plan; add a unit test.
   Workaround used: `--no-llm`.
4. **T7.4: the AI report contains 7 factual errors**, e.g. E2 "3 lost orders" (0 lost), "the watchdog triggered
   restarts / did not recover autonomously" (watchdog did nothing, `manual_action_needed: False`), "185 %" (17 %),
   "53.3 % (75/165)" (45.5 %), "W2 clients always received 200 OK" (false for E2). Two of these numbers were
   flagged by the fact-check, the claims were not. *Next:* give the reporter explicit key facts for the watchdog
   outcome, W2 error counts and "recovery not measured after abort"; add ratio/percent and watchdog-claim checks to
   the fact-check; review the report by hand before showing it.

### Gaps
- **Only 2 enabled faults (E1, E2).** The plan expects E3/E4 with knowledge files; they are not in `catalog.yaml`
  yet, so Phase 5 covers 2 faults. The Day-2 requirement is at least 3 failure types.

### Other findings (not test failures)
- Kill-switch test (T3.3): in cli mode `cr_deleted_s` measures "kill + watchdog until Ready", because
  `chaosctl kill` is called synchronously before polling; the journal shows the CR was gone in 0.66 s. *Proposed
  harness fix:* start `chaosctl kill` with `Popen` and poll the CR while it runs; also assert `cr_deleted_s ≤ 3` in
  the test's pass check. With 2 workers the 10 s pod-failure made the deployment unready before the trigger in only
  4/9 runs.
- After an abort, `t_pod_ready`, `t_slis_recovered` and `recovery_time_s` are not recorded (None), which the AI
  report then misreads as "did not recover".
- The 1 Hz Redis probe misses a 1 s Redis outage (E2: 0/38 samples unreachable), so `queue_unreachable` never fires.
- The `silent_async_failure` detector fires on E1 runs with 0 W2 orders lost; Agent 2 then writes a "silent failure"
  story the fates do not support. Grading (keyword match) still says CORRECT.
- Every E1 run shows 2 W1 errors at ~61 s, explained by 2 n8n-main log lines "job stalled more than
  maxStalledCount": jobs held by the killed worker fail instead of being retried.
- Agent 1 (Mistral) picked E1 in all 3 LLM plans, even after E1 passed and E2 (score 0.80) was unlocked; its
  hypothesis assumes "Redis persistence", which the baseline profile does not have.
- `chaosctl report --all` includes test runs (T3.4b/c), which skews the ranking; there is no way to exclude runs.
- Dashboard: map labels every service "SPOF" (n8n-worker has 2 replicas); Agent 1 panel shows the plan's first
  experiment, not the injected one, for `--no-llm` runs; a dashboard started before the cluster keeps stale
  credentials.
- Chat (RAG) answers "today" questions with yesterday's documented Mac numbers and once cited a doc that is not in
  its listed sources.
- Grafana is OOMKilled with the trimmed values (irrelevant while scaled to 0).
- Wrong-context refusal is a Python traceback, not a one-line message.
- Host setup notes (Linux): Docker group membership needs a new login session; a host firewall dropping forwarded
  replies (`DOCKER-USER ... -i wlan0 -j DROP`) blocks in-cluster image pulls until replies to the Kind bridge are
  accepted; Arch needs `docker-buildx` for `scripts/up.sh`.

### Is the system demo-ready?
**Yes for safety and the core story, with fixes needed before the video.** Rebuild-from-scratch works, safety holds,
and the AI agents run live on Mistral. Before recording: (1) choose a `--queue-abort` that this host actually crosses
(or show the auto-stop with E2), (2) run E2 with `--no-llm` or fix `run --exp` (failure 3), (3) review or regenerate
the findings report so judges do not see the factual errors (failure 4), (4) cut warm-up and observation time to fit
3 minutes, (5) add a third failure type for the Day-2 "at least 3" requirement.

## Phase 0: Preflight (2026-10-11 01:52)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T0.1 | Tools | `docker version`, `kind version`, `kubectl version --client`, `helm version`, `uv --version` | docker (≥ 6 GB for containers), kind, kubectl, helm, uv present | docker 29.9.0 (Linux, no memory cap; 14 GiB host RAM, ~4 GiB free), kind v0.33.0, kubectl v1.37.1, helm v4.3.0, uv 0.11.26 | shell output | PASS (memory headroom tight: ~4 GiB free) |
| T0.2 | Host limits | `sysctl fs.inotify.*`, `ss -ltn` | ≥ 512 instances / 524288 watches; ports free | inotify 1024 / 524288. Ports 15678 16379 15432 18080 held by the existing `chaos` cluster, 8787 by a running dashboard; T1.1 deletes both, re-checked there | shell output | PASS (limits); ports: see T1.1 |
| T0.3 | Unit tests | `uv sync && uv run pytest` | all pass | 84 passed in 5.7 s | pytest output | PASS |
| T0.4 | Knowledge base | `uv run chaosctl kb` | every catalog fault with alias; "knowledge base consistent" | E1 worker_pod_kill (tier 1, enabled), E2 redis_pod_kill (tier 3, enabled), pod_failure (test only), worker_graceful_kill (disabled); "knowledge base consistent". No E3/E4 in the catalog yet | kb output | PASS (only 2 enabled faults: see Gaps) |
| T0.5 | LLM | `PYTHONPATH=. uv run python scripts/llm_smoke.py` | Mistral answers valid JSON, model ministral-14b-latest | `ministral-14b-latest`, source `api`, 1.54 s, `{'service': 'n8n-main', 'depends_on': ['redis', 'postgres']}` | smoke output | PASS |

## Phase 1: Clean build from scratch (01:55-02:12, 17 min)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T1.1 | Cluster | `kind delete cluster --name chaos`; `chaosctl cluster-up` | all pods Running; Chaos Mesh ready; n8n-chaos annotated | Delete freed ports 15678/16379/15432/18080. cluster-up 7 min 9 s (images cached from an earlier install on this host). Chaos Mesh controller/daemon/dashboard 1/1 Running; namespace annotation `chaos-mesh.org/inject: enabled`. 21/22 pods Running: **monitoring-grafana CrashLoopBackOff, container `grafana` OOMKilled** (5 restarts in 13 min) | /tmp/phase1.log, `kubectl get pods -A` | PASS (Grafana OOM is a finding; Grafana is scaled to 0 in T1.3) |
| T1.2 | App | `chaosctl up` + `chaosctl bootstrap` | postgres, redis, mock-api, n8n main + worker Ready; W1 200 + row; W2 200 + row later | up 1 min 11 s, bootstrap 64 s. All 5 pods 1/1 Running. W1 HTTP 200 in 4.74 s, 1 row; W2 HTTP 200 in 0.016 s, 1 row after the wait | /tmp/bootstrap.log | PASS |
| T1.3 | Post-setup | README 3.3 steps 4-5 | worker count recorded; Grafana/Prometheus at 0 | n8n-worker **2/2 Ready**; Grafana and Prometheus 0 pods | kubectl output | PASS |
| T1.4 | Graph | `chaosctl graph` | 5 services; edges via ConfigMap env + overlay; redis + postgres SPOF | 5 services + client. Edges n8n-main/worker→redis/postgres `k8s_env+overlay`; client→main, main→worker, worker→mock-api `overlay`. SPOF: postgres, redis (criticality 1.00), also mock-api (1.00) and n8n-main (0.80); n8n-worker (2 replicas) not a SPOF. All 1-replica services have gap `no_pdb` | graph.json | PASS |

## Phase 2: Baseline (02:11-02:13)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T2.1 | Baseline | `chaosctl baseline --duration 120` | W1 success ≥ 99 %, W1 p95 recorded, queue wait trend ≈ 0 | 2 req/s, 2 workers. W1 105/105 (100 %), p50 4047.6 ms, p95 4056.8 ms. W2 100/100, p50 20.9 ms, p95 24.2 ms. Queue wait mean 0, max 0, trend 0; active mean 7.98 | state/baseline.json (captured_at 1791665012) | PASS |

## Phase 3: Safety (02:14-)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T3.1 | Context refusal | throwaway context `e2e-dummy` (server `https://127.0.0.1:1`) active; `chaosctl graph`, `chaosctl run --exp E1` | refuses to run | Both stop with `WrongCluster: current kube context is 'e2e-dummy', expected 'kind-chaos'`; no PodChaos created. Context restored to kind-chaos, dummy deleted. Shown as a full Python traceback, not a clean one-line message (cosmetic) | command output | PASS |
| T3.3 | Kill switch | `chaosctl killswitch-test` (02:14:35-02:18:39) | 3 ways × 3; chaos removed ≤ 3 s every time; worker Ready ≤ 60 s | Tool verdict "9/9 passed". Rows (via, try: CR deleted s / worker Ready s after trigger / fault visible before trigger): cli 1: 1.4/1.4/no; cli 2: 1.4/1.4/no; **cli 3: 10.4/10.4/yes**; file 1: 1.6/1.6/no; file 2: 1.6/1.6/yes; file 3: 1.5/35.0/yes; signal 1: 1.5/5.6/no; signal 2: 1.5/1.5/no; signal 3: 1.6/44.7/yes. Worker Ready ≤ 60 s: 9/9. CR removed ≤ 3 s: **8/9** (cli try 3 took 10.4 s). The tool's own pass check tests only "Ready < 60 s after injection", not the 3 s limit. With 2 workers the 10 s pod-failure made the deployment unready before the trigger in only 4/9 runs, so 5 runs prove removal but not restore of a visible outage. Afterwards each worker had 8 restarts (no CrashLoopBackOff); `rollout restart deploy/n8n-worker` done for a clean slate. **Investigated cli try 3:** its journal shows `kill_switch_exec` at t=121.0, `chaos_delete_all` (CR deleted) at t=121.6 (0.66 s, ~0.8 s after the trigger), `kill_switch_done` at t=131.1 (`all_ready_s` 10.15). The test calls `chaosctl kill` synchronously and only then polls for the CR, so in cli mode `cr_deleted_s` measures "kill + watchdog until all pods Ready", not CR removal. Real removal ≤ 3 s in 9/9 | reports/killswitch-test.json, reports/ks-cli-3-65110/journal.jsonl, /tmp/ks.log | PASS (the 10.4 s is a measurement artifact in the test harness; see Gaps for the proposed fix) |
| T3.2 | Namespace protection | PodChaos `pod-kill` (mode one) on a Deployment in un-annotated namespace `e2e-unprotected` | selects nothing | Same pod name before and after 15 s, restartCount 0: target not killed. Namespace and chaos deleted afterwards | command output | PASS |
| T3.4a | Auto-stop (as written) | `chaosctl demo --queue-abort 20`, 2 workers (02:33-02:36) | controller aborts on its own, removes the fault, watchdog: all Ready, no manual action | Run r1011-023307: Agent 1 (cached LLM plan) picked E1; injected with 4 W1 + 4 W2 in flight, 8 active. **No abort**: status `completed`. Queue waiting max **0** (surviving worker absorbed the load), W1 p95 max 4.13 s. Pod Ready 31.5 s, SLIs recovered 70.1 s, 2 W1 errors at ~61 s, fates 8/8 completed once. The queue limit 20 is never reached with 2 workers, so the documented "show the automatic stop" demo does not stop | reports/r1011-023307 | FAIL (no auto-stop; not a controller fault, the limit was never crossed) |
| T3.4b | Auto-stop, 1 worker | worker scaled to 1; `chaosctl run --exp E1 --queue-abort 20` (02:40-02:45) | same | Run r1011-024059: status `completed`, **no abort**. Queue waiting max **6**, active 5-15, W1 p95 max 8.46 s. Pod Ready 31.5 s, SLIs 70.0 s. Fates (9 in flight): 7 completed once, **1 lost silently (W2)**, 1 lost visibly. This host (16 cores) drains the backlog far faster than the 8 GB Mac where the queue reached ~52 | reports/r1011-024059 | FAIL (limit 20 not reachable on this host) |
| T3.4c | Auto-stop, 1 worker, stricter limit | `chaosctl run --exp E1 --queue-abort 3` (02:46-02:50) | same | Run r1011-024619: **aborted by the controller** `queue_waiting 4 > 3` at 3.6 s after inject; chaos CR deleted 0.6 s later; watchdog `recovered: True, manual_action_needed: False`; all pods Ready afterwards, no managed chaos left. Fates: 7 completed once, 1 lost silently, 1 lost visibly. After an abort the timeline does not record `t_pod_ready` / `t_slis_recovered` (None) and `recovery_time_s` is None | reports/r1011-024619 | PASS (mechanism works; needs a host-appropriate limit) |
| T3.5 | LOCKED state | `chaosctl recover --ack` | clears LOCKED if a run ended LOCKED | No run ended LOCKED (no LOCKED file). `recover --ack` on the clean system: deleted [], 0.18 s, all Ready 0.61 s, recovered True | command output | PASS (LOCKED never happened) |

Worker count restored to **2** after T3.4c (all later results use 2 workers).

## Phase 5: Every fault, end to end (02:52-02:59, default limits, 2 workers)

Enabled faults per `chaosctl kb`: E1 worker_pod_kill (tier 1), E2 redis_pod_kill (tier 3). Cooldown ≥ 60 s between runs.

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T5.1.1 | E1 run | `chaosctl run --exp E1` (02:52:32-02:55:25) | completed or aborted | **completed**. In flight at inject: 4 W1 + 4 W2, 8 active. Pod Ready 31.5 s, queue drained 31.5 s, first symptom 60.9 s, SLIs recovered 70.0 s, last in-flight resolved 3.0 s. Restarts delta all 0. Watchdog recovered, no manual action | reports/r1011-025233/results.json | PASS |
| T5.1.2 | E1 files | `ls reports/r1011-025233` | journal.jsonl, results.json, evidence/exp-01.json, evidence/exp-01.plan.json | all four present | reports/r1011-025233 | PASS |
| T5.1.3 | E1 fates | results.json | fate total = in-flight count | 8 completed once, 0 lost silently, 0 duplicated, 0 phantom, 0 lost visibly; total 8 = 8 in flight | results.json `fates` | PASS |
| T5.1.4 | E1 impact | trace vs `knowledge/worker_pod_kill.yaml` | queue grows; expected symptoms `consumers_down`, `latency_degradation` | Queue waiting max **0**, active 8-11, W1 p95 max 4.59 s (1.13× baseline), worker deployment never unready in the 1 Hz pod samples. Neither expected symptom visible: with 2 workers the second worker absorbs the load. The knowledge file's expected symptoms describe the 1-worker system | reports/r1011-025233/evidence/exp-01.json | FAIL (impact does not match the knowledge file; the 2-worker fix hides it, see Gaps) |
| T5.1.5 | Healthy after | `kubectl get pods`, `get podchaos` | all Ready, no managed chaos | 6/6 pods Running/Ready, no PodChaos | kubectl output | PASS |
| T5.2.1a | E2 run (LLM) | `chaosctl run --exp E2` (02:56:49-02:56:56) | completed or aborted | **Nothing ran, exit 0, no message, empty results.json.** Agent 1 (Mistral, 5.3 s) planned only worker_pod_kill (cooldown 120 s); `run --exp` filters the plan *after* Agent 1 returns (`campaign.py` `plan()`), and Agent 1 is never told the requested fault, so the filter left an empty plan | reports/r1011-025650/journal.jsonl | FAIL (harness bug, see Failures) |
| T5.2.1b | E2 run (fallback plan) | `chaosctl run --exp E2 --no-llm` (02:57:34-02:59:18) | completed or aborted | **aborted by the controller** `w1_p95_ms 23430 > 20000` at 38.9 s. Runtime guard approved tier 3 (E1 passed in r1011-025233). In flight: 4 W1 + 4 W2, 8 active. Redis pod Ready 1.0 s; restarts delta n8n-main 1, n8n-worker 2; n8n-main unready 11.8-28.9 s, workers 11.8-38.9 s. SLIs not recovered within the run (None). Chaos CR deleted 0.6 s after abort; watchdog recovered, no manual action | reports/r1011-025735/results.json, journal.jsonl | PASS |
| T5.2.2 | E2 files | `ls reports/r1011-025735` | journal, results, evidence/exp-01.json, exp-01.plan.json | journal.jsonl, results.json, evidence/**exp-02**.json, exp-02.plan.json (the fallback plan numbers it exp-02) | reports/r1011-025735 | PASS (file names follow the plan id) |
| T5.2.3 | E2 fates | results.json | total = in-flight | 5 completed once, **3 phantom success** (client got an error, order saved), 0 lost, 0 duplicated; total 8 = 8 | results.json `fates` | PASS |
| T5.2.4 | E2 impact | trace vs `knowledge/redis_pod_kill.yaml` | Redis unreachable; expected `queue_unreachable`, `latency_degradation` | W1 p95 max 23.4 s (5.8× baseline); W2 errors in 22 of 38 samples (W2 stopped accepting too); n8n-main and workers restarted (`dependency_restart`, listed as possible). Redis probe **never** saw unreachable (0 of 38 samples): the outage (1.0 s) is shorter than the 1 Hz sample interval | reports/r1011-025735/evidence/exp-02.json | PASS (latency + cascade match; `queue_unreachable` missed by the 1 Hz probe) |
| T5.2.5 | Healthy after | kubectl | all Ready, no managed chaos | 6/6 Running/Ready (n8n-main 1 restart, each worker 1), no PodChaos | kubectl output | PASS |
| T5.3+ | E3, E4, … | `chaosctl kb` | every new enabled fault run end to end | No E3/E4 in `catalog.yaml` (kb lists only E1, E2 enabled; pod_failure test-only, worker_graceful_kill disabled) | kb output | GAP |

## Phase 6: Agent 2, blind diagnosis (03:00-03:03)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T6.1 | E1 diagnosis | `chaosctl diagnose --run r1011-025233 --exp exp-01` | source llm; fixes from playbook; grading CORRECT | source `llm`. Detectors fired: `silent_async_failure` only. Root cause: "Insufficient worker capacity (single worker pod) combined with a `maxStalledCount` threshold that was exceeded…". Fixes: scale_workers, async_status_callback, stalled_job_tuning (all in playbook.yaml). Grading: **CORRECT**. Fact check: the maxStalledCount part is supported (2 n8n-main log lines "job stalled more than maxStalledCount", which explains the 2 W1 errors at ~61 s); "single worker pod" is **wrong** (2 workers ran); `silent_async_failure` fired although 0 W2 orders were lost | reports/r1011-025233/evidence/exp-01.diagnosis.json | PASS (graded correct; contains one factual error) |
| T6.2 | E2 diagnosis | `chaosctl diagnose --run r1011-025735 --exp exp-02` | same | source `llm`. Detectors: latency_degradation, dependency_restart, phantom_success. Root cause: "Redis pod restart triggered a cascade of n8n-main and n8n-worker restarts, disrupting job result propagation to W1 clients and causing phantom successes and latency degradation." Fixes: redis_persistence, async_status_callback, scale_workers (all in playbook). Grading: **CORRECT**. Says "W2 clients succeed", contradicted by 22/38 samples with W2 errors | reports/r1011-025735/evidence/exp-02.diagnosis.json | PASS (one factual error) |
| T6.3 | Demo-run diagnosis | (from `chaosctl demo`, r1011-023307) | same | source `llm`; detectors: silent_async_failure; fixes async_status_callback, graceful_shutdown, scale_workers (playbook); grading CORRECT. Reasoning claims in-flight jobs were "lost or left in a 'phantom success' state" while the fate table shows 8/8 completed once | reports/r1011-023307/evidence/exp-01.diagnosis.json | PASS (graded correct; reasoning contradicts the fate table) |
| T6.y | Blindness | evidence vs diagnosis | no Chaos Mesh event or log line in Agent 2's input | Evidence files on disk keep 11-15 Chaos Mesh events (e.g. `chaosctl-r1011-025735-redis-pod-kill` "Successfully apply chaos…"); `diagnosis.blind()` drops every event/log line containing "chaos" before the LLM call. 0 Chaos Mesh mentions in all 3 diagnoses. Ordinary k8s events stay visible (`Killing`, `FailedPreStopHook`), as documented | evidence/*.json, *.diagnosis.json, diagnosis.py:52-57 | PASS |

## Phase 7: AI-written findings report and ranked score (03:01-03:07)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T7.1 | AI report | `chaosctl report --all` (03:01:57-03:05:27, 3.5 min) | "Written by Agent 2"; every experiment present; ranking lists every scored experiment | Header "Written by Agent 2 (ministral-14b-latest)". 5 experiments: r1011-023307, -024059, -024619, -025233 (E1) and -025735 (E2). The empty run r1011-025650 (T5.2.1a) is correctly absent. **Overall 55/100 (fragile).** Ranking: E2 r1011-025735 **35** (failing, weakest: self recovery); E1 r1011-024619 62 (fragile); E1 r1011-024059 77; E1 r1011-023307 80; E1 r1011-025233 **88** (resilient). Components by mean: detectability 20.0, self recovery 57.0, latency 63.6, data integrity 83.4, availability 89.8. `--all` also includes the T3.4b/c test runs (1 worker, artificial queue limits 20/3), which pull the E1 scores down | docs/results/findings-ai.md, findings-ai.json | PASS (ranking includes test runs, see Gaps) |
| T7.2 | Fact-check flags | findings.json `unverified_numbers` | empty, or each flag checked by hand | 5 flags, checked by hand: r1011-024059 "2.08" (8.46/4.05 = 2.09×: correct ratio); "4.05" labelled baseline p95 (real baseline p95 4.06 s, p50 4.05 s: minor mislabel); r1011-024619 "77.8" (7/9 completed once = 77.8 %: correct); r1011-025735 "53.3" in "53.3 % (75/165)" (75/165 = 45.5 %: **wrong**); "185" in "exceeding the 20 s abort limit by 185 %" (23.4 vs 20 s = 17 %: **wrong**) | findings-ai.json | PASS (flags work; 2 of 5 flagged numbers are real errors) |
| T7.3 | Cross-check | report notes (> 20 points apart) | list and judge | One note: r1011-024619 Agent 2 **62** vs formula **85.2** (23 points). Other gaps: E2 35 vs 42.2, r1011-023307 80 vs 95.6, r1011-024059 77 vs 88.1, r1011-025233 88 vs 95.6 (Agent 2 is consistently lower). Judgement for r1011-024619: Agent 2 scores self-recovery low because the run was "aborted", but the abort came from the artificial test limit (`queue_waiting 4 > 3`), not from the system; the formula ignores the 1 silent loss beyond D = 77.8 (15 % weight). The formula is closer for system behaviour, but both under-weight the silent loss | report section 3 | PASS (note shown and judged) |
| T7.4 | Report vs evidence | read findings.md against results.json | no contradictions | **7 factual errors**, all in prose: (1) E2 "3 phantom successes and 3 lost orders (phantom successes)": fates are 5 completed once + 3 phantom, 0 lost; (2) E2 "did not recover autonomously; the watchdog triggered restarts": restarts were n8n processes exiting, watchdog `recovered: True, manual_action_needed: False`; (3) E2 "did not recover within the 120 s deadline": run aborted at 38.9 s, recovery not measured (n/a); (4) "185 %" (should be 17 % over the limit); (5) "53.3 % (75/165)" (45.5 %); (6) E2 "queue unreachability … observed": the probe never saw Redis unreachable; (7) executive summary "W2 clients always received 200 OK": false for E2 (W2 errors in 22/38 samples). Weakness #4 "No self-recovery from Redis disruptions" rests on errors 2-3 | docs/results/findings-ai.md | FAIL (7 claims contradict results.json) |
| T7.5 | Deterministic draft | `chaosctl report --all --no-llm` | labelled draft | "**Deterministic draft, not AI-written** (AI writer disabled with --no-llm)". Formula ranking: E2 42.2 (failing), r1011-024619 85.2, r1011-024059 88.1, r1011-023307 95.6, r1011-025233 95.6. Afterwards `report --all` re-run: answered from the LLM cache in < 1 s, identical to the first AI report | docs/results/findings-deterministic.md | PASS |

## Phase 8: Dashboard and chat (02:33-02:36 live; 03:07-03:12 after the report)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T8.1 | Dashboard live | `chaosctl dashboard` (restarted after the cluster rebuild) during `chaosctl demo` r1011-023307 | step bar through all 8 steps; map shows target + blast radius; charts move; fate table, Agent 2 panel and score panel fill in; screenshots light + dark | Seen live: step "2. Baseline" with countdown "11 s to injection", map with TARGET on n8n-worker and dashed blast radius on n8n-main, Agent 1 menu (Mistral, scores 0.80/0.74, pick), latency chart with the inject marker at 02:34:08; after the run step "7. Agent 2 diagnoses", fate table 8 completed once, Agent 2 panel (Mistral) filled. After `chaosctl report`: step "8. Report & score", score panel 55/100 fragile with 5 ranked bars and components. Steps 3-6 were not captured on screen (polled every ~40 s). Issues: (a) the map labels every service "SPOF", including n8n-worker with 2 replicas (`chaosctl graph` says not a SPOF); (b) for the `--no-llm` E2 run the Agent 1 panel says "Picked: worker pod kill" while the run injected redis_pod_kill (it shows the plan's first experiment); (c) a dashboard started before the cluster existed keeps stale kube credentials and shows every service "Not ready" until restarted. Light mode follows `prefers-color-scheme` and needs a reload | docs/results/dashboard-dark-*.jpg (3), dashboard-light-*.jpg (3) | PASS (with 3 display issues) |
| T8.2 | Ask Agent 2 | 3 questions in the dashboard chat | answers from Mistral citing sources that exist | Q1 "Why were orders affected when Redis restarted?": `answered by mistral`; sources JUDGES_GUIDE.md (2 sections) + run:r1011-025735/exp-02:diagnosis (all exist); says "60-second outage" (yesterday's Mac figure from the docs; today's run was auto-stopped at 38.9 s). Q2 "Did adding a second worker help in today's runs?": mistral; answers only from yesterday's runs r1010-163348 / r1010-155337; inline citation `doc:PROGRESS.md#Final runs today…` is not in its listed sources (listed: JUDGES_GUIDE.md). Q3 "How long did the n8n worker take to recover after it was killed today?": mistral; source PROGRESS.md (exists); answers "~34 seconds" from r1010-163348, not today's 31.5 s | docs/results/dashboard-light-3-chat.jpg, page text | PASS (Mistral + real sources; answers prefer stale doc numbers over the current runs) |

## Phase 9: Demo rehearsal (03:10:54-03:16:14)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T9.1 | Demo, timed | DEMO.md: off camera `rm -f KILL`, `recover --ack`, `baseline --duration 120`; on camera `chaosctl graph`, `chaosctl demo --queue-abort 20` | total time; slow/confusing parts; fit in 3 min | Off camera 2 min 5 s (baseline W1 105/105, p95 4053.3 ms, queue max 0). **On camera 3 min 14 s** (03:12:59.9-03:16:13.8): graph 0.8 s; Agent 1 (Mistral) 4.75 s; warm-up 60 s; observe 100.5 s; recovery 0.6 s; fate table + Agent 2 (Mistral 15.4 s) + printing ~28 s. Run r1011-031301: Agent 1 picked **E1 again** (3rd LLM pick of E1 although E2 is unlocked), completed, no auto-stop (queue never above 20 with 2 workers), SLIs 70.0 s, 8/8 completed once, Agent 2 CORRECT. Chat (~20 s per answer) and kill-switch slide (instant) come on top. **For the 3-min video:** cut the 60 s warm-up and ~70 s of the observation window (or pre-record); the "Impact / auto-stop" step shows no stop with 2 workers (use a pre-recorded 1-worker run or a host-appropriate `--queue-abort`); the audience never sees the Redis kill unless it is run explicitly (`run --exp E2 --no-llm`, see T5.2.1a); the n8n owner-account setup must be done before recording | reports/r1011-031301, /tmp/rehearsal.log | PASS (timed; over 3 min live, needs cuts listed) |

Note on the E1 detector: `silent_async_failure` fired in all E1 runs with 2 workers, including runs with 0 W2 orders lost. Agent 2 then builds a "silent failure" story the fates do not support. The grading only checks keywords (`worker`, `consumer`), so these runs still grade CORRECT.

## Phase 4: Agent 1 (02:15, run during T3.3; dry-run never injects)

| ID | Area | Command | Expected | Observed (real numbers) | Evidence | Result |
|---|---|---|---|---|---|---|
| T4.1 | Agent 1 (LLM) | `chaosctl plan --dry-run` | "Plan source: llm"; reasons cite real graph ids; guard orders tier 1 before tier 3 and gates tier 3; every enabled fault is a candidate | Plan source `llm` (ministral-14b-latest, 6.8 s). Candidates: redis_pod_kill score 0.80 (tier 3), worker_pod_kill 0.74 (tier 1): both enabled faults. Mistral planned **one** experiment, E1 → node:n8n-worker, citing node:n8n-worker, edge:n8n-worker->mock-api, edge:n8n-worker->redis, edge:n8n-main->n8n-worker, wf:W1, wf:W2 (all exist in graph.json). Guard: approve, tier 1. Because the LLM did not plan E2, the tier-3 gate is not exercised here (it is in T4.2). Hypothesis claims requeue "via Redis persistence", which is factually wrong (Redis has no persistence in the baseline profile) | reports/plans/journal.jsonl (agent1_scored, agent1_plan, ts 1791665093) | PASS (gate shown in T4.2; wrong premise in hypothesis noted) |
| T4.2 | Agent 1 fallback | `chaosctl plan --dry-run --no-llm` | valid deterministic plan | Source `fallback:llm_disabled`. 2 experiments; guard "reordered by tier rule": worker_pod_kill (tier 1) before redis_pod_kill (tier 3); E2 verdict "tier 3 allowed only after tier 1 experiment exp-01 passes (checked again at run time)". E2 limits: error-rate abort off (outage by design) | plan output | PASS |
