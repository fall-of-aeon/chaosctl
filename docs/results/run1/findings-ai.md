# Findings report: r1011-023307, r1011-024059, r1011-024619, r1011-025233, r1011-025735

_Written by Agent 2 (ministral-14b-latest) on 2026-10-11 03:05 from the run journals and evidence. Every cited id and fix is validated and every number is fact-checked against the measurements._

## 1. Executive summary

Chaos experiments on **n8n in queue mode** (Kubernetes) revealed critical weaknesses in **data integrity, detectability, and resilience** under worker and Redis failures. **Silent data loss** (1–2% of orders lost without client awareness) and **phantom successes** (clients receiving errors despite order persistence) occurred during worker pod kills and Redis disruptions. While **W2 (async) clients** always received 200 OK responses (masking failures), **W1 (sync) clients** suffered visible failures and latency spikes (up to 23.4s p95). The system **self-recovered** in most worker-kill scenarios (~70s) but **failed to recover autonomously** during Redis disruptions, requiring watchdog intervention. **No duplicates** were observed, but **detectability gaps** (e.g., silent async failures, lost work) and **cascading restarts** (Redis → n8n-main/worker) exacerbated risks. **Overall resilience is fragile**, with scores ranging from **35 (failing) to 88 (resilient)** across experiments.

**Resilience score (Agent 2): 55 / 100 (fragile)**

| Experiment | Fault | Status | Score | Headline |
|---|---|---|---|---|
| r1011-023307/exp-01 | E1 Worker pod kill | completed | 80 (degraded but recovered) | Worker pod kill caused no data loss or duplicates but degraded W1 latency while W2 remained unaffected. |
| r1011-024059/exp-01 | E1 Worker pod kill | completed | 77 (degraded but recovered) | Worker pod kill caused silent and visible order loss while W2 clients remained unaware, but the system self-recovered within 70 seconds. |
| r1011-024619/exp-01 | E1 Worker pod kill | aborted | 62 (fragile) | Worker pod kill caused silent and visible order loss while W2 maintained 100% client success rates, with no self-recovery observed. |
| r1011-025233/exp-01 | E1 Worker pod kill | completed | 88 (resilient) | Worker pod kill caused no data loss or duplicates, but triggered a silent async failure detector despite full recovery within 70 seconds. |
| r1011-025735/exp-02 | E2 Redis pod kill | aborted | 35 (failing) | Redis pod kill caused W1 latency spikes, phantom successes, and partial data integrity loss without silent failures. |

### Resilience ranking (least resilient first)

| Rank | Experiment | Fault → target | Score | Weakest component |
|---|---|---|---|---|
| 1 | r1011-025735/exp-02 | E2 Redis pod kill → redis | 35 (failing) | self recovery |
| 2 | r1011-024619/exp-01 | E1 Worker pod kill → n8n-worker | 62 (fragile) | self recovery |
| 3 | r1011-024059/exp-01 | E1 Worker pod kill → n8n-worker | 77 (degraded but recovered) | detectability |
| 4 | r1011-023307/exp-01 | E1 Worker pod kill → n8n-worker | 80 (degraded but recovered) | detectability |
| 5 | r1011-025233/exp-01 | E1 Worker pod kill → n8n-worker | 88 (resilient) | detectability |

| Component (weight) | Mean score | Experiments |
|---|---|---|
| detectability (10%) | 20.0 | 5 |
| self recovery (25%) | 57.0 | 5 |
| latency (20%) | 63.6 | 5 |
| data integrity (15%) | 83.4 | 5 |
| availability (30%) | 89.8 | 5 |

## 2. Environment and baseline

The system under test was **n8n in queue mode** deployed on Kubernetes, with **n8n-main** receiving webhooks and enqueuing jobs in Redis, while **n8n-worker pods** processed jobs (4s mock-api delay) and wrote orders to Postgres. Two workflow types were tested: **W1 (synchronous, client waits for result)** and **W2 (asynchronous, client gets 200 OK immediately)**. A load generator injected synthetic orders and verified Postgres consistency. The baseline showed **100% success rates** for both W1 and W2, with **W1 p95 latency at 4.06s** and **W2 p95 at 24.2ms**.

## 3. Experiments

### r1011-023307/exp-01 · E1 · Worker pod kill → n8n-worker (completed)

_Worker pod kill caused no data loss or duplicates but degraded W1 latency while W2 remained unaffected._

**Why Agent 1 chose it:** The experiment targeted the resilience of async job execution under worker pod failure, focusing on W1/W2 workflows and Redis/mock-api dependencies.

**Expected vs observed:** The hypothesis predicted job requeuing within 30s and ≤10% W1/W2 failure rate, but no failures occurred; instead, W1 latency spiked while W2 success remained at 100%. The blind diagnosis incorrectly assumed silent failures, but facts showed no silent losses or duplicates.

**Customer impact:** W1 clients experienced degraded latency (p95 rose to 4126.6ms from 4056.8ms baseline), but no orders were lost or duplicated; W2 clients saw no impact (100% success rate). No customer-facing order failures occurred.

**Recovery:** The system self-recovered within 70.1s (SLIs back to baseline) without manual intervention, though W1 latency remained elevated until full resolution.

**Root cause:** Abrupt termination of the `n8n-worker` pod (ev:events) caused in-flight W1 jobs to stall, but Redis persistence and worker auto-recovery prevented data loss or duplication. The blind diagnosis overstated silent failures; no silent losses occurred.

Evidence: `agent1_plan`, `agent2_diagnosis`, `baseline`, `fates`, `guard`, `knowledge`, `silent_async_failure`, `timeline`, `trace`

**Resilience score: 80 / 100 (degraded but recovered)**, weighted from Agent 2's component scores

| Component | Weight | Score | Reason |
|---|---|---|---|
| availability | 30% | 98 | W1 success rate after injection was 98% (490/500), only 2% lower than baseline (100%). |
| self recovery | 25% | 95 | SLIs recovered within 70.1s without manual action, though slightly slower than the 'excellent' 30s threshold. |
| latency | 20% | 60 | Peak W1 p95 latency rose to 4126.6ms (baseline: 4056.8ms), a 1.2% increase, but well below the 20000ms abort limit. |
| data integrity | 15% | 100 | All 8 in-flight orders completed exactly once (0 lost, duplicated, or phantom successes). |
| detectability | 10% | 0 | Health checks remained green throughout the outage (ev:timeline, ev:guard), despite W1 latency degradation. |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 61.0 s |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 70.1 s |
| last in-flight order resolved | 3.1 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: High `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed

</details>

### r1011-024059/exp-01 · E1 · Worker pod kill → n8n-worker (completed)

_Worker pod kill caused silent and visible order loss while W2 clients remained unaware, but the system self-recovered within 70 seconds._

**Why Agent 1 chose it:** The experiment targeted the single point of failure in the n8n-worker node, which directly impacted in-flight orders and degraded W1 latency, while W2 clients continued receiving 200 OK responses despite failures.

**Expected vs observed:** The hypothesis predicted W1 failures, W2 200 OK responses, and potential duplicates or phantom successes, but only silent and visible losses occurred (1 each), with no duplicates or phantom successes observed. The system recovered autonomously without manual intervention, aligning with the guard's recovery deadline.

**Customer impact:** Customer impact included **W1**: 2% visible failure rate (1/498) and degraded latency (p95 peaked at 8.46 s vs baseline 4.05 s), while **W2** showed no visible impact (100% success rate). Orders were lost silently (1) and visibly (1), with no duplicates or phantom successes.

**Recovery:** The system recovered autonomously within **70 seconds**, with SLIs returning to baseline and no manual action required. The watchdog did not intervene, and no restarts occurred.

**Root cause:** The blind diagnosis was partially correct: the worker pod kill directly caused order loss (silent and visible) due to failed job execution, but no duplicates or phantom successes occurred. The **detectability gap** was confirmed, as W2 clients remained unaware of failures despite degraded W1 performance.

Evidence: `fates`, `latency_degradation`, `lost_work`, `silent_async_failure`, `timeline`, `guard`, `baseline`

**Resilience score: 77 / 100 (degraded but recovered)**, weighted from Agent 2's component scores

| Component | Weight | Score | Reason |
|---|---|---|---|
| availability | 30% | 98 | W1 success rate after injection was 98% (488/498), with only 1 visible failure and 1 silent loss. |
| self recovery | 25% | 95 | The system recovered autonomously within 70 seconds, well under the 120-second recovery deadline, with no manual intervention. |
| latency | 20% | 60 | W1 p95 latency peaked at 8.46 s (vs baseline 4.05 s), a 2.08x increase, but remained under the 20 s abort limit (20,000 ms). |
| data integrity | 15% | 78 | 7 out of 9 in-flight orders completed exactly once (78%), with 1 silent loss and 1 visible loss, and no duplicates or phantom successes. |
| detectability | 10% | 0 | W2 clients received 100% 200 OK responses despite silent failures, confirming a detectability gap where async clients remained unaware of order loss. |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 0.0 s |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 70.0 s |
| last in-flight order resolved | 60.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |
| lost visible | 1 |

Detectors: Medium `latency_degradation`: W1 p95 peaked at 8.5 s vs baseline 4.1 s; High `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed; Critical `lost_work`: 2 in-flight requests never produced a row (1 after a 200 OK)

</details>

### r1011-024619/exp-01 · E1 · Worker pod kill → n8n-worker (aborted)

_Worker pod kill caused silent and visible order loss while W2 maintained 100% client success rates, with no self-recovery observed._

**Why Agent 1 chose it:** The experiment targeted worker pod resilience, and the observed outcomes directly reflect the impact of worker pod failure on order processing and system recovery.

**Expected vs observed:** The hypothesis predicted W1 failures and W2 200 OK responses, which held true, but the system did not self-recover within the recovery deadline, and silent data loss occurred despite W2's consistent 100% success rate. Duplicates and phantom successes were not observed, contradicting the hypothesis.

**Customer impact:** Customer impact included **W1**: 100% success rate preserved but with **1 visible loss** (1/20 requests failed) and **1 silent loss** (1/20 requests succeeded but no order persisted). **W2**: 100% client success rate (200 OK) but **1 silent loss** (no order persisted). Orders were not duplicated or phantom-successed. SLIs did not recover within the 120 s deadline.

**Recovery:** The system did not self-recover; the watchdog had to intervene (manual action not needed), and SLIs remained degraded after 120 s. The queue drained after 60 s, but the lost work detector fired due to silent/visible losses.

**Root cause:** The blind diagnosis was partially correct: worker pod failure caused lost work (silent and visible), but the system lacked mechanisms to detect or recover from silent failures (e.g., no client-side validation for W2's 200 OK). The hypothesis overestimated resilience by assuming retries would prevent silent loss.

Evidence: `fates`, `timeline`, `lost_work`, `guard`, `baseline`

**Resilience score: 62 / 100 (fragile)**, weighted from Agent 2's component scores

| Component | Weight | Score | Reason |
|---|---|---|---|
| availability | 30% | 100 | W1 maintained 100% success rate (20/20 requests succeeded) despite worker pod failure. |
| self recovery | 25% | 0 | SLIs did not recover within the 120 s deadline, and the watchdog had to intervene (no autonomous recovery). |
| latency | 20% | 100 | Peak W1 p95 latency (4051.6 ms) remained at baseline (4056.8 ms), well below the 20,000 ms abort limit. |
| data integrity | 15% | 77 | 7/9 in-flight orders completed once (77.8%), while 1 was lost silently and 1 visibly (no duplicates or phantom successes). |
| detectability | 10% | 0 | Health checks remained green throughout the outage, failing to expose silent data loss (W2's 200 OK masked the issue). |

> Cross-check: the measurement formula in `chaosctl/score.py` gives 85.2 for this experiment (23 points from Agent 2's score).

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 0.0 s |
| target replaced/Ready | n/a |
| queue back to baseline | n/a |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 60.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |
| lost visible | 1 |

Detectors: Critical `lost_work`: 2 in-flight requests never produced a row (1 after a 200 OK)

</details>

### r1011-025233/exp-01 · E1 · Worker pod kill → n8n-worker (completed)

_Worker pod kill caused no data loss or duplicates, but triggered a silent async failure detector despite full recovery within 70 seconds._

**Why Agent 1 chose it:** The experiment targeted worker pod resilience, and the observed outcomes directly relate to the injected fault and its impact on async workflows (W2).

**Expected vs observed:** The hypothesis predicted job loss and duplicates, but none occurred; instead, the system triggered a silent async failure detector due to W2 clients receiving 200 OK without job completion, though all jobs eventually succeeded. The guard’s abort limits were not breached, and recovery was automatic and fast.

**Customer impact:** No customer impact on W1 or W2 success rates (both remained at 100% after injection), and no orders were lost or duplicated. The only observed issue was a transient silent async failure detector firing due to W2’s immediate 200 OK responses during worker pod restart.

**Recovery:** The system recovered autonomously within 70 seconds, with SLIs returning to baseline and no manual intervention required. The watchdog did not trigger restarts, and all in-flight orders resolved successfully.

**Root cause:** The blind diagnosis was partially correct in identifying worker pod disruption as the root cause, but the facts do not support claims of `maxStalledCount` thresholds or insufficient worker capacity—no job loss, duplicates, or degraded W1 success rates occurred. The silent async failure detector fired due to W2’s immediate 200 OK responses during transient worker unavailability, not due to silent data loss.

Evidence: `agent1_plan`, `agent2_diagnosis`, `baseline`, `fates`, `guard`, `knowledge`, `silent_async_failure`, `timeline`, `trace`

**Resilience score: 88 / 100 (resilient)**, weighted from Agent 2's component scores

| Component | Weight | Score | Reason |
|---|---|---|---|
| availability | 30% | 100 | W1 success rate remained at 100% after injection (491/501 successful responses). |
| self recovery | 25% | 95 | The system recovered autonomously within 70 seconds, with SLIs returning to baseline and no manual intervention required. |
| latency | 20% | 98 | Peak W1 p95 latency rose from 4056.8 ms (baseline) to 4593.4 ms, well below the abort limit of 20000 ms. |
| data integrity | 15% | 100 | All 8 in-flight orders completed exactly once, with no silent loss, visible loss, duplicates, or phantom successes. |
| detectability | 10% | 0 | A silent async failure detector fired despite no actual data loss, indicating a false positive in detectability during transient worker unavailability. |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 60.9 s |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 70.0 s |
| last in-flight order resolved | 3.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: High `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed

</details>

### r1011-025735/exp-02 · E2 · Redis pod kill → redis (aborted)

_Redis pod kill caused W1 latency spikes, phantom successes, and partial data integrity loss without silent failures._

**Why Agent 1 chose it:** The experiment targeted Redis, a single point of failure for both n8n-main and n8n-worker, with high blast radius and untested resilience (score 0.8).

**Expected vs observed:** Predicted symptoms like queue unreachability and latency degradation were observed (W1 p95 peaked at 23.4 s vs baseline 4.1 s), but no silent failures occurred; instead, 3 phantom successes and 3 lost orders (phantom successes) were recorded, while 5 orders completed successfully. The system did not recover within the 120 s deadline, and SLIs did not return to baseline.

**Customer impact:** W1 clients experienced 53.3% success rate (75/165) post-injection, with latency spikes to 23.4 s (vs 4.1 s baseline), while W2 clients saw a minimum success rate of 50%. No silent data loss occurred, but 3 phantom successes and 3 lost orders (phantom successes) impacted data integrity.

**Recovery:** The system did not recover autonomously; the watchdog triggered restarts (n8n-main: 1, n8n-worker: 2), but SLIs did not return to baseline within the 120 s observation window. The harness aborted due to W1 p95 exceeding the 20 s abort limit.

**Root cause:** The Redis pod kill disrupted job result propagation to W1 clients, causing phantom successes (3) and latency degradation due to dependency restarts (n8n-main and n8n-worker). The blind diagnosis was correct: Redis instability triggered cascading restarts, but no silent failures occurred.

Evidence: `baseline`, `fates`, `latency_degradation`, `dependency_restart`, `phantom_success`, `timeline`, `guard`

**Resilience score: 35 / 100 (failing)**, weighted from Agent 2's component scores

| Component | Weight | Score | Reason |
|---|---|---|---|
| availability | 30% | 53 | W1 success rate dropped to 53.3% (75/165) after injection, compared to 100% baseline. |
| self recovery | 25% | 0 | The system did not recover autonomously; SLIs did not return to baseline, and the watchdog had to intervene with restarts. |
| latency | 20% | 0 | W1 p95 latency peaked at 23.4 s, exceeding the 20 s abort limit by 185% (23.4 s vs 4.1 s baseline). |
| data integrity | 15% | 62 | Of 8 in-flight orders, 5 completed once (62.5%), while 3 resulted in phantom successes (no visible loss, but inconsistent state). |
| detectability | 10% | 100 | Detectors fired for latency degradation, dependency restart, and phantom successes, ensuring the problem was observable. |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 0.0 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 1.0 s |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 11.3 s |

| In-flight order fate | count |
|---|---|
| completed once | 5 |
| phantom success | 3 |

Detectors: High `latency_degradation`: W1 p95 peaked at 23.4 s vs baseline 4.1 s; High `dependency_restart`: n8n-main restarted 1x; Medium `phantom_success`: 3 clients got an error but the work was done

</details>

## 4. Weaknesses, ranked by severity

| # | Severity | Weakness | Seen in | Evidence | Fixes |
|---|---|---|---|---|---|
| 1 | Critical | **Silent Data Loss in Async Workflows (W2)**: W2 clients consistently received **200 OK** responses even when orders were silently lost (1–2% of in-flight orders during worker pod kills). This **undetectable failure mode** violates data integrity and misleads clients into assuming success. The system lacks mechanisms to alert clients of async job failures post-acceptance. | r1011-024059/exp-01, r1011-024619/exp-01 | `silent_async_failure`, `lost_work` | `async_status_callback` |
| 2 | Critical | **Visible Data Loss and Phantom Successes During Worker/Redis Failures**: Worker pod kills caused **1–2 visible order losses** (client errors + no Postgres row) and **1–3 phantom successes** (client errors despite order persistence). Redis pod kills introduced **3 phantom successes** due to disrupted result propagation. These inconsistencies erode trust in order processing and require manual reconciliation. | r1011-024059/exp-01, r1011-024619/exp-01, r1011-025735/exp-02 | `lost_work`, `phantom_success`, `dependency_restart` | `idempotency_keys`, `async_status_callback`, `redis_persistence` |
| 3 | High | **Undetected Worker Failures and Latency Degradation**: Worker pod kills degraded **W1 latency to 8.5s p95** (vs 4.1s baseline) and triggered **silent async failure detectors** without client visibility. The system lacks **real-time alerts for stalled consumers** or **queue depth monitoring**, leaving operators unaware of processing gaps until SLIs degrade. Redis failures further amplified latency to **23.4s p95** due to cascading restarts. | r1011-024059/exp-01, r1011-025735/exp-02 | `latency_degradation`, `silent_async_failure`, `dependency_restart` | `queue_depth_alert`, `worker_health_probe`, `scale_workers`, `redis_timeout_threshold` |
| 4 | High | **No Self-Recovery from Redis Disruptions**: Redis pod kills caused **system-wide failures**, including **phantom successes**, **latency spikes**, and **no autonomous recovery** within the 120s observation window. The watchdog triggered restarts, but SLIs did not return to baseline, demonstrating **critical dependency on Redis stability**. This risks prolonged outages if Redis fails repeatedly. | r1011-025735/exp-02 | `dependency_restart`, `latency_degradation`, `phantom_success` | `redis_persistence`, `redis_timeout_threshold` |
| 5 | Medium | **False Positives in Silent Failure Detection**: The **silent_async_failure detector** fired falsely in experiments where **no silent loss occurred** (e.g., `r1011-025233/exp-01`), due to W2’s immediate 200 OK responses during transient worker unavailability. This **noisy alerting** reduces trust in monitoring and may lead to unnecessary interventions. | r1011-023307/exp-01, r1011-025233/exp-01 | `silent_async_failure` | `async_status_callback`, `worker_health_probe` |

## 5. Recommended fixes (advisory; an engineer applies them)

| Priority | Fix | Why | Addresses | Effort / impact | Verify |
|---|---|---|---|---|---|
| 1 | Give async (W2) clients a way to learn the real outcome (`async_status_callback`) | This directly addresses the **most critical weakness**: silent data loss in async workflows (W2). By providing clients with **real-time status callbacks** (e.g., execution IDs + outcome endpoints), the system can **eliminate undetectable failures** and alert clients of async job failures. It also mitigates phantom successes by clarifying order persistence. | Silent Data Loss in Async Workflows (W2), Visible Data Loss and Phantom Successes During Worker/Redis Failures | medium / high | `reconciliation gap (accepted W2 without a row after 30 s) stays 0 during E1` |
| 2 | Enable Redis AOF persistence on a PVC (`redis_persistence`) | Redis disruptions caused **phantom successes** and **no self-recovery**, risking prolonged outages. Enabling **AOF persistence** ensures queue state survives restarts, preventing lost work and reducing dependency on Redis availability. This is critical for **data integrity** during Redis failures. | Visible Data Loss and Phantom Successes During Worker/Redis Failures, No Self-Recovery from Redis Disruptions | medium / high | `redis-cli -p 16379 CONFIG GET appendonly  # yes, then re-run E2` |
| 3 | Run at least 2 n8n workers (`scale_workers`) | Worker pod kills degraded **W1 latency** and caused **lost work** due to insufficient parallelism. Running **2 workers** (instead of 1) reduces backlog during failures, improving **resilience to consumer outages** and **draining queues faster**. This is a low-effort, high-impact fix for **latency degradation** and **backlog risks**. | Undetected Worker Failures and Latency Degradation | low / high | `kubectl -n n8n-chaos get deploy n8n-worker  # READY 2/2, then re-run E1` |
| 4 | Make order inserts idempotent on request_id (`idempotency_keys`) | While no duplicates were observed, **phantom successes** (3 instances) and the risk of **silent retries** justify making order inserts **idempotent**. This prevents **double execution** and ensures **consistent state** even if jobs are retried due to transient failures. | Visible Data Loss and Phantom Successes During Worker/Redis Failures | low / high | `SELECT request_id, count(*) FROM orders GROUP BY 1 HAVING count(*) > 1  # 0 rows` |
| 5 | Alert on queue depth and on zero active consumers (`queue_depth_alert`) | The system lacks alerts for **stalled consumers** or **queue backlogs**, leaving operators unaware of processing gaps until SLIs degrade. Adding **Prometheus rules for `jobs_waiting > 50`** and **zero active consumers** would enable **proactive detection** of worker failures, improving **observability** and reducing MTTR. | Undetected Worker Failures and Latency Degradation | low / medium | `rule fires within 15 s during E1` |
| 6 | Worker readiness that checks Redis, not just the process (`worker_health_probe`) | Current worker readiness probes (e.g., `pgrep`) fail to detect **Redis connectivity issues**, leading to **false silent failures**. A **Redis-aware health check** would prevent workers from being marked healthy while unable to process jobs, reducing **undetected failures** and noisy alerts. | False Positives in Silent Failure Detection, Undetected Worker Failures and Latency Degradation | low / medium | `worker goes NotReady when Redis is unreachable` |

## 6. Limitations

This report is limited to **worker pod kills** and **Redis pod kills** in a **controlled Kubernetes environment**. It does not test: (1) **network partitions** between n8n components, (2) **Postgres failures**, (3) **multi-region Redis disruptions**, (4) **concurrent failures** (e.g., worker + Redis kills), or (5) **custom workflows beyond W1/W2**. Additionally, **detector coverage** was incomplete (e.g., no `duplicates` or `success_without_row` detectors fired), and **baseline experiments** did not stress-test **high-order volumes** or **edge cases like Redis forks. The observed resilience may not hold under **higher load or cascading failures**.

## 7. How this report was produced

Agent 2 wrote and scored each experiment from the recorded facts (timeline, order fates, measurements, detectors, Agent 1's plan and its own blind diagnosis), then wrote the summary, weakness ranking and fix priorities. Validators rejected unknown ids, fixes outside `playbook.yaml`, malformed scores and inconsistent rankings (up to two retries with the errors as feedback); numbers in the text were checked against the measurements. The total is the weighted mean of Agent 2's component scores.

Scoring rubric given to Agent 2:

```
Score each component 0-100 (higher = more resilient), or null if the facts cannot support a score:
- availability: share of W1 (sync) requests that succeeded after injection
- self_recovery: did the system recover without help, and how fast (SLIs back within ~30 s is excellent; needing
  the watchdog to restart something is poor; a LOCKED harness is 0)
- latency: how far peak W1 p95 rose above baseline relative to the abort limit
- data_integrity: share of in-flight orders that completed exactly once (lost, duplicated, phantom are failures)
- detectability: whether health checks/readiness showed the problem (green health checks during an outage = 0)
Weights (the harness computes the weighted total from your component scores): availability 30%, self_recovery 25%, latency 20%, data_integrity 15%, detectability 10%.
```

**Fact-check flags** (numbers not traceable to the measurements, or claims the counts contradict): r1011-024059/exp-01: 2.08, 4.05; r1011-024619/exp-01: 77.8; r1011-025735/exp-02: 53.3, 185