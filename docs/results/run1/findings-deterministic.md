# Findings report: r1011-023307, r1011-024059, r1011-024619, r1011-025233, r1011-025735

> **Deterministic draft, not AI-written** (AI writer disabled with --no-llm). Re-run `uv run chaosctl report` when Mistral is reachable.

Generated 2026-10-11 03:06 by `chaosctl report` from the run journals, evidence bundles and Agent 2 diagnoses.

## 1. Summary

**Resilience score: 81.3 / 100 (degraded but recovered)**, mean of 5 scored experiment(s); worst 42.2.

| Experiment | Fault | Target | Status | Score | Agent 2 root cause |
|---|---|---|---|---|---|
| r1011-023307/exp-01 | E1 Worker pod kill | n8n-worker | completed | 95.6 (resilient) | correct |
| r1011-024059/exp-01 | E1 Worker pod kill | n8n-worker | completed | 88.1 (resilient) | not run |
| r1011-024619/exp-01 | E1 Worker pod kill | n8n-worker | aborted | 85.2 (resilient) | not run |
| r1011-025233/exp-01 | E1 Worker pod kill | n8n-worker | completed | 95.6 (resilient) | correct |
| r1011-025735/exp-02 | E2 Redis pod kill | redis | aborted | 42.2 (failing) | correct |

### Resilience ranking (least resilient first)

| Rank | Experiment | Fault → target | Score | Weakest component |
|---|---|---|---|---|
| 1 | r1011-025735/exp-02 | E2 Redis pod kill → redis | 42.2 (failing) | latency |
| 2 | r1011-024619/exp-01 | E1 Worker pod kill → n8n-worker | 85.2 (resilient) | self recovery |
| 3 | r1011-024059/exp-01 | E1 Worker pod kill → n8n-worker | 88.1 (resilient) | data integrity |
| 4 | r1011-023307/exp-01 | E1 Worker pod kill → n8n-worker | 95.6 (resilient) | self recovery |
| 5 | r1011-025233/exp-01 | E1 Worker pod kill → n8n-worker | 95.6 (resilient) | self recovery |

| Component (weight) | Mean score | Experiments |
|---|---|---|
| self recovery (25%) | 76.0 | 5 |
| latency (20%) | 76.6 | 5 |
| data integrity (15%) | 83.6 | 5 |
| availability (30%) | 87.9 | 5 |
| detectability (10%) | n/a | 0 |

## 2. Environment and baseline

- Steady load: 2.0 orders/s (baseline captured over 120 s)
- W1 (sync): success 100 %, p50 4048 ms, p95 4057 ms
- W2 (async): success 100 %, p95 24 ms
- Queue: waiting mean 0, max 0
- Agent 1 plan source: fallback:llm_disabled, fallback:no valid response for injection_select, llm; LLM models used: ministral-14b-latest

## 3. Experiments

### r1011-023307 / exp-01 · E1 · Worker pod kill → n8n-worker (completed)

**What was injected:** Delete one n8n-worker pod with no graceful shutdown while executions are running.

**Why Agent 1 chose it** (llm): Test resilience of async job execution under worker pod failure affecting W1/W2 via edge:n8n-worker->mock-api and edge:n8n-worker->redis
Cites: node:n8n-worker, edge:n8n-worker->mock-api, edge:n8n-worker->redis, edge:n8n-main->n8n-worker, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 20, total > 180 s

**Expected vs observed**

- Hypothesis: The system will requeue failed jobs within 30s via Redis persistence, and the client will observe no more than 10% job failure rate in W1/W2 after worker recovery.
- Expected symptoms seen: none
- Expected but not seen: consumers_down, latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | 61.0 s |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 70.1 s |
| last in-flight order resolved | 3.1 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Detectors fired**

- High: `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed

**Agent 2 (blind, llm)**: Abrupt termination of an `n8n-worker` pod (ev:events) caused in-flight jobs to be lost or left unresolved, leading to silent failures for W1 clients while W2 clients received immediate 200 OK responses.
Failure chain: Worker pod `n8n-worker-5d9dfccf4-tzp4f` terminated abruptly with a `FailedPreStopHook` (ev:events). → In-flight jobs were dequeued but not processed to completion (ev:trace at t=61.1s shows `q_active` drop and W1 errors). → W2 clients received immediate 200 OK responses (fire-and-forget), while W1 clients waited indefinitely for unresolved jobs (ev:trace, ev:timeline). → The system lacked visibility into job failures, causing silent degradation for W1 traffic (det:silent_async_failure). → Jobs were not retried or reprocessed due to the abrupt termination (ev:fates shows no `lost_silently` but implies incomplete processing).
Fixes: `async_status_callback`, `graceful_shutdown`, `scale_workers`
Graded against the real injection: correct

**Resilience score: 95.6 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 98.0 | 490/500 W1 responses successful across post-injection samples |
| self recovery | 25% | 86.6 | SLIs back to baseline 70 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-024059 / exp-01 · E1 · Worker pod kill → n8n-worker (completed)

**What was injected:** Delete one n8n-worker pod with no graceful shutdown while executions are running.

**Why Agent 1 chose it** (fallback:no valid response for injection_select): node:n8n-worker is on 2/2 workflow paths, single point of failure (edge:n8n-main->n8n-worker); blast radius ['n8n-main']; retest (score 0.46)
Cites: node:n8n-worker, edge:n8n-main->n8n-worker

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 20, total > 180 s

**Expected vs observed**

- Hypothesis: in-flight W1 requests fail or hang while W2 keeps returning 200 and the wait queue grows; stalled jobs may be re-run after lock expiry, creating duplicates
- Expected symptoms seen: latency_degradation
- Expected but not seen: consumers_down
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | 0.0 s |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 70.0 s |
| last in-flight order resolved | 60.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |
| lost visible | 1 |

**Detectors fired**

- Medium: `latency_degradation`: W1 p95 peaked at 8.5 s vs baseline 4.1 s
- High: `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed
- Critical: `lost_work`: 2 in-flight requests never produced a row (1 after a 200 OK)

**Agent 2:** not run yet (`uv run chaosctl diagnose --run r1011-024059 --exp exp-01`).

**Resilience score: 88.1 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 98.0 | 488/498 W1 responses successful across post-injection samples |
| self recovery | 25% | 86.7 | SLIs back to baseline 70 s after injection, no help |
| latency | 20% | 82.9 | peak p95 8.5 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 77.8 | 7/9 in-flight orders completed exactly once; lost silently 1, lost visible 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-024619 / exp-01 · E1 · Worker pod kill → n8n-worker (aborted)

**What was injected:** Delete one n8n-worker pod with no graceful shutdown while executions are running.

**Why Agent 1 chose it** (llm): Test resilience of async job execution under worker pod failure while enqueuing via redis and dequeueing via redis
Cites: node:n8n-worker, node:redis, edge:n8n-main->redis, edge:n8n-worker->redis, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 3, total > 180 s

**Expected vs observed**

- Hypothesis: The system will retry failed jobs from the Redis queue and maintain client-facing availability for W2, but may experience transient delays in W1 due to missing worker pod.
- Expected symptoms seen: none
- Expected but not seen: consumers_down, latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | 0.0 s |
| target replaced/Ready | n/a |
| queue back to baseline | n/a |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 60.0 s |
| automatic stop | queue_waiting 4 > 3 |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |
| lost visible | 1 |

**Detectors fired**

- Critical: `lost_work`: 2 in-flight requests never produced a row (1 after a 200 OK)

**Agent 2:** not run yet (`uv run chaosctl diagnose --run r1011-024619 --exp exp-01`).

**Resilience score: 85.2 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 20/20 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 77.8 | 7/9 in-flight orders completed exactly once; lost silently 1, lost visible 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-025233 / exp-01 · E1 · Worker pod kill → n8n-worker (completed)

**What was injected:** Delete one n8n-worker pod with no graceful shutdown while executions are running.

**Why Agent 1 chose it** (llm): Test resilience of async job execution under worker pod failure while jobs are enqueued/dequeued via redis
Cites: node:n8n-worker, node:redis, edge:n8n-main->redis, edge:n8n-worker->redis, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: The system will requeue failed jobs to another n8n-worker pod within 30s, maintaining <5% job loss and <10s client latency degradation.
- Expected symptoms seen: none
- Expected but not seen: consumers_down, latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | 60.9 s |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 70.0 s |
| last in-flight order resolved | 3.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Detectors fired**

- High: `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed

**Agent 2 (blind, llm)**: Insufficient worker capacity (single worker pod) combined with a `maxStalledCount` threshold that was exceeded during transient worker restarts, causing silent async failures (W2) and degraded sync performance (W1).
Failure chain: Worker pod restart (ev:events) disrupts job processing. → Single worker cannot keep up with queue backlog (ev:trace: q_active=8-11, ev:logs:n8n-main-59654d75c9-kz947). → Jobs stall and exceed `maxStalledCount`, triggering silent failures (ev:logs:n8n-main-59654d75c9-kz947). → Async clients (W2) receive immediate 200 OK without job completion (det:silent_async_failure). → Sync clients (W1) experience degraded success rates (ev:trace: w1_err=2 at t=61.1s).
Fixes: `scale_workers`, `async_status_callback`, `stalled_job_tuning`
Graded against the real injection: correct

**Resilience score: 95.6 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 98.0 | 491/501 W1 responses successful across post-injection samples |
| self recovery | 25% | 86.7 | SLIs back to baseline 70 s after injection, no help |
| latency | 20% | 100 | peak p95 4.6 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-025735 / exp-02 · E2 · Redis pod kill → redis (aborted)

**What was injected:** Delete the Redis pod (no persistence) while jobs are queued and running.

**Why Agent 1 chose it** (fallback:llm_disabled): node:redis is on 2/2 workflow paths, single point of failure (edge:n8n-main->redis, edge:n8n-worker->redis); blast radius ['n8n-main', 'n8n-worker']; untested (score 0.8)
Cites: node:redis, edge:n8n-main->redis, edge:n8n-worker->redis

**Safety guard:** approve · blast radius ['node:n8n-main', 'node:n8n-worker'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: queued and active jobs are lost because redis has no persistence; n8n may restart if the outage exceeds the 10 s redis timeout
- Expected symptoms seen: latency_degradation
- Expected but not seen: queue_unreachable
- Not anticipated by the knowledge base: phantom_success

| Timeline (s after injection) | |
|---|---|
| first symptom | 0.0 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 1.0 s |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 11.3 s |
| automatic stop | w1_p95_ms 23430 > 20000 |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 5 |
| phantom success | 3 |

**Detectors fired**

- High: `latency_degradation`: W1 p95 peaked at 23.4 s vs baseline 4.1 s
- High: `dependency_restart`: n8n-main restarted 1x
- Medium: `phantom_success`: 3 clients got an error but the work was done

**Agent 2 (blind, llm)**: Redis pod restart (ev:events) triggered a cascade of `n8n-main` and `n8n-worker` restarts, disrupting job result propagation to W1 clients and causing phantom successes and latency degradation.
Failure chain: Redis pod restart (ev:events) causes queue instability and consumer unreadiness (ev:trace, ev:timeline) → `n8n-main` and `n8n-worker` restart (ev:restarts_delta, ev:events) due to dependency failure → Job processing continues but result callbacks to W1 clients fail (ev:fates, det:phantom_success) → W1 clients experience timeouts or errors while W2 clients succeed (ev:trace, det:latency_degradation) → Latency spikes (W1 p95=23.4s) and phantom successes (3) due to lost state (ev:baseline, det:latency_degradation)
Fixes: `redis_persistence`, `async_status_callback`, `scale_workers`
Graded against the real injection: correct

**Resilience score: 42.2 / 100 (failing)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 45.5 | 75/165 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 0 | peak p95 23.4 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 62.5 | 5/8 in-flight orders completed exactly once; phantom success 3 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

## 4. Weaknesses, ranked by severity

| Severity | Weakness | Seen in | Evidence | Fixes |
|---|---|---|---|---|
| Critical | `lost_work` | 2× | 2 in-flight requests never produced a row (1 after a 200 OK) | `redis_persistence`, `async_status_callback`, `graceful_shutdown` |
| High | `silent_async_failure` | 3× | W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed | `async_status_callback` |
| High | `latency_degradation` | 2× | W1 p95 peaked at 8.5 s vs baseline 4.1 s | `scale_workers`, `stalled_job_tuning` |
| High | `dependency_restart` | 1× | n8n-main restarted 1x | `redis_timeout_threshold` |
| Medium | `phantom_success` | 1× | 3 clients got an error but the work was done | `idempotency_keys` |

## 5. Recommended fixes (advisory; an engineer applies them)

| # | Fix | Recommended by | Addresses | Effort / impact | Verify |
|---|---|---|---|---|---|
| 1 | Give async (W2) clients a way to learn the real outcome (`async_status_callback`) | Agent 2 (3×) | lost_work, silent_async_failure | medium / high | `reconciliation gap (accepted W2 without a row after 30 s) stays 0 during E1` |
| 2 | Run at least 2 n8n workers (`scale_workers`) | Agent 2 (3×) | latency_degradation | low / high | `kubectl -n n8n-chaos get deploy n8n-worker  # READY 2/2, then re-run E1` |
| 3 | Enable Redis AOF persistence on a PVC (`redis_persistence`) | Agent 2 (1×) | lost_work | medium / high | `redis-cli -p 16379 CONFIG GET appendonly  # yes, then re-run E2` |
| 4 | Keep graceful shutdown for planned worker stops (`graceful_shutdown`) | Agent 2 (1×) | lost_work | low / low | `graceful worker stop loses no jobs` |
| 5 | Tune lock duration / stalled interval for faster job recovery (`stalled_job_tuning`) | Agent 2 (1×) | latency_degradation | low / medium | `time to last in-flight resolved drops in E1` |
| 6 | Make order inserts idempotent on request_id (`idempotency_keys`) | playbook match | phantom_success | low / high | `SELECT request_id, count(*) FROM orders GROUP BY 1 HAVING count(*) > 1  # 0 rows` |
| 7 | Raise QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD above typical Redis restart time (`redis_timeout_threshold`) | playbook match | dependency_restart | low / medium | `n8n-main restart count unchanged during E2` |

## 6. How the score is computed

R = 0.30·A + 0.25·S + 0.20·L + 0.15·D + 0.10·M, each 0-100 (weights in `chaosctl/scoring.yaml`).
- A availability: successful W1 responses / all W1 responses after injection.
- S self-recovery: 100 if SLIs are back within 30 s, falling to 70 at the recovery deadline; 60 if pods recovered but SLIs did not within the window; 40 if the watchdog restarted something; 0 if LOCKED.
- L latency: 100 if peak W1 p95 ≤ 1.5× baseline, 0 at the abort limit, linear in between.
- D data integrity: in-flight orders completed exactly once / all in-flight orders.
- M detectability: 0 if n8n-main stayed Ready through a consumer/queue outage, 100 if health checks showed it, n/a if no such outage happened.
Components without data are n/a and the other weights are rescaled (coverage is shown). The score compares experiments on this testbed; it is not a certified reliability measure.
