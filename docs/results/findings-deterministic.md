# Findings report: r1011-044913, r1011-050622, r1011-050940, r1011-051312, r1011-051604, r1011-051950, r1011-052352, r1011-052657, r1011-052955, r1011-053323, r1011-053831

> **Deterministic draft, not AI-written** (AI writer unavailable: call budget of 30 exhausted). Re-run `uv run chaosctl report` when Mistral is reachable.

Generated 2026-10-11 05:47 by `chaosctl report` from the run journals, evidence bundles and Agent 2 diagnoses.

## 1. Summary

**Resilience score: 84.9 / 100 (degraded but recovered)**, mean of 13 scored experiment(s); worst 35.5.

| Experiment | Fault | Target | Status | Score | Agent 2 root cause |
|---|---|---|---|---|---|
| r1011-044913/exp-01 | E1 Worker pod kill | n8n-worker | completed | 97.9 (resilient) | correct |
| r1011-044913/exp-02 | E1 Worker pod kill | n8n-worker | completed | 97.7 (resilient) | correct |
| r1011-044913/exp-03 | E1 Worker pod kill | n8n-worker | completed | 97.8 (resilient) | correct |
| r1011-050622/exp-01 |  Worker pod failure (hung worker) | n8n-worker | completed | 100.0 (resilient) | correct |
| r1011-050940/exp-01 | E5 Mock API returns HTTP 500/503 | mock-api | completed | 64.9 (fragile) | correct |
| r1011-051312/exp-01 | E4 Mock API latency | mock-api | aborted | 84.0 (degraded but recovered) | missed |
| r1011-051604/exp-01 | E7 Postgres overload | postgres | completed | 97.5 (resilient) | missed |
| r1011-051950/exp-01 | E3 Traffic spike | n8n-main | completed | 95.4 (resilient) | missed |
| r1011-052352/exp-01 |  Worker-Redis network partition | n8n-worker | completed | 100.0 (resilient) | missed |
| r1011-052657/exp-01 | E2 Redis pod kill | redis | aborted | 40.2 (failing) | correct |
| r1011-052955/exp-01 | E10 n8n-main pod kill | n8n-main | completed | 93.9 (resilient) | correct |
| r1011-053323/exp-01 | E11 Postgres pod kill | postgres | aborted | 35.5 (failing) | correct |
| r1011-053831/exp-01 | E8 Postgres disk full | postgres | completed | 98.6 (resilient) | missed |

### Resilience ranking (least resilient first)

| Rank | Experiment | Fault → target | Score | Weakest component |
|---|---|---|---|---|
| 1 | r1011-053323/exp-01 | E11 Postgres pod kill → postgres | 35.5 (failing) | latency |
| 2 | r1011-052657/exp-01 | E2 Redis pod kill → redis | 40.2 (failing) | latency |
| 3 | r1011-050940/exp-01 | E5 Mock API returns HTTP 500/503 → mock-api | 64.9 (fragile) | latency |
| 4 | r1011-051312/exp-01 | E4 Mock API latency → mock-api | 84.0 (degraded but recovered) | self recovery |
| 5 | r1011-052955/exp-01 | E10 n8n-main pod kill → n8n-main | 93.9 (resilient) | availability |
| 6 | r1011-051950/exp-01 | E3 Traffic spike → n8n-main | 95.4 (resilient) | self recovery |
| 7 | r1011-051604/exp-01 | E7 Postgres overload → postgres | 97.5 (resilient) | self recovery |
| 8 | r1011-044913/exp-02 | E1 Worker pod kill → n8n-worker | 97.7 (resilient) | data integrity |
| 9 | r1011-044913/exp-03 | E1 Worker pod kill → n8n-worker | 97.8 (resilient) | data integrity |
| 10 | r1011-044913/exp-01 | E1 Worker pod kill → n8n-worker | 97.9 (resilient) | data integrity |
| 11 | r1011-053831/exp-01 | E8 Postgres disk full → postgres | 98.6 (resilient) | self recovery |
| 12 | r1011-050622/exp-01 | Worker pod failure (hung worker) → n8n-worker | 100.0 (resilient) | self recovery |
| 13 | r1011-052352/exp-01 | Worker-Redis network partition → n8n-worker | 100.0 (resilient) | availability |

| Component (weight) | Mean score | Experiments |
|---|---|---|
| latency (20%) | 76.7 | 13 |
| availability (30%) | 85.6 | 13 |
| self recovery (25%) | 87.8 | 13 |
| data integrity (15%) | 89.5 | 13 |
| detectability (10%) | n/a | 0 |

## 2. Environment and baseline

- Steady load: 2.0 orders/s (baseline captured over 120 s)
- W1 (sync): success 100 %, p50 4048 ms, p95 4056 ms
- W2 (async): success 100 %, p95 27 ms
- Queue: waiting mean 0, max 0
- Agent 1 plan source: fallback:invalid_plan, llm:api:ministral-14b-latest; LLM models used: ministral-14b-latest, ministral-8b-latest

## 3. Experiments

### r1011-044913 / exp-01 · E1 · Worker pod kill → n8n-worker (completed)

**What was injected:** Delete one n8n-worker pod with no graceful shutdown while executions are running.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test resilience of async job execution under worker pod failure while enqueuing jobs via edge:n8n-main->redis and dequeueing via edge:n8n-worker->redis
Cites: node:n8n-worker, edge:n8n-main->redis, edge:n8n-worker->redis, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: The system will retry failed jobs within 30s and maintain client-facing availability via edge:client->n8n-main, despite worker pod crashes.
- Expected symptoms seen: none
- Expected but not seen: consumers_down, latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 32.5 s |
| queue back to baseline | 32.5 s |
| SLIs back to baseline | 32.5 s |
| last in-flight order resolved | 3.9 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |
| lost silently | 1 |

**Detectors fired**

- Critical: `lost_work`: 1 in-flight requests never produced a row (1 after a 200 OK)

**Agent 2 (blind, llm:api:ministral-14b-latest)**: Abrupt worker pod termination due to FailedPreStopHook, causing loss of in-flight work and potential row persistence failures in Postgres.
Failure chain: Worker pod terminates abruptly (FailedPreStopHook) during job execution → In-flight job completes in worker but Postgres write is interrupted or lost → Client receives 200 OK (job appears successful) but no row is persisted in Postgres → System recovers and continues processing other jobs without detecting the lost work → Deterministic detector flags 'lost_work' fate (1 job lost silently)
Fixes: `graceful_shutdown`, `redis_persistence`, `postgres_ha`
Graded against the real injection: correct

**Resilience score: 97.9 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 310/310 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.2 | SLIs back to baseline 32 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 88.9 | 8/9 in-flight orders completed exactly once; lost silently 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-044913 / exp-02 · E1 · Worker pod kill → n8n-worker (completed)

**What was injected:** Delete one n8n-worker pod with no graceful shutdown while executions are running.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test impact of concurrent worker pod failures on async job backlog and client workflows W1/W2 under edge:n8n-worker->mock-api and edge:n8n-worker->postgres dependencies
Cites: node:n8n-worker, edge:n8n-worker->mock-api, edge:n8n-worker->postgres, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: The system will degrade gracefully with job queue growth but retain client availability via edge:client->n8n-main for 2+ minutes before requiring manual intervention.
- Expected symptoms seen: none
- Expected but not seen: consumers_down, latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 32.4 s |
| queue back to baseline | 32.4 s |
| SLIs back to baseline | 32.4 s |
| last in-flight order resolved | 3.2 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |

**Detectors fired**

- Critical: `lost_work`: 1 in-flight requests never produced a row (1 after a 200 OK)

**Agent 2 (blind, llm:api:ministral-14b-latest)**: Abrupt worker pod termination due to a failed PreStop hook, causing in-flight jobs to be abandoned without completion or error signaling.
Failure chain: 1. Worker pod (`n8n-worker-6994cf9db5-kn4kc`) fails during PreStop hook execution, preventing graceful shutdown. → 2. In-flight job(s) are abandoned mid-execution, with no retry or error propagation. → 3. The abandoned job fails to write to Postgres, resulting in a silent loss (`lost_silently=1`). → 4. W1 clients wait indefinitely for the missing result, while W2 clients receive a 200 OK without knowing the job failed. → 5. The system masks the failure until post-mortem analysis reveals the silent loss via `fates`.
Fixes: `graceful_shutdown`, `async_status_callback`, `scale_workers`
Graded against the real injection: correct

**Resilience score: 97.7 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 307/307 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.2 | SLIs back to baseline 32 s after injection, no help |
| latency | 20% | 100 | peak p95 4.6 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 87.5 | 7/8 in-flight orders completed exactly once; lost silently 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-044913 / exp-03 · E1 · Worker pod kill → n8n-worker (completed)

**What was injected:** Delete one n8n-worker pod with no graceful shutdown while executions are running.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test resilience of async job execution under worker pod failure while enqueuing jobs via edge:n8n-main->redis and dequeueing via edge:n8n-worker->redis, with focus on edge:n8n-worker->mock-api timeout=30s
Cites: node:n8n-worker, edge:n8n-main->redis, edge:n8n-worker->mock-api, edge:n8n-worker->redis, wf:W1

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: The system will handle HTTP timeouts to edge:n8n-worker->mock-api by retrying jobs, but client workflow W1 will experience delays exceeding 30s if all workers are down.
- Expected symptoms seen: none
- Expected but not seen: consumers_down, latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 31.5 s |
| last in-flight order resolved | 3.6 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |

**Detectors fired**

- Critical: `lost_work`: 1 in-flight requests never produced a row (1 after a 200 OK)

**Agent 2 (blind, llm:api:ministral-8b-latest)**: Abrupt `n8n-worker` termination due to failed PreStopHook, leaving jobs unfinished and failing to notify W1 clients of async failures.
Failure chain: Worker PreStopHook fails → abrupt pod termination → In-flight jobs remain unprocessed in Redis → W1 clients timeout waiting for async results (no callback) → 1 job lost silently (det:lost_work) → W2 clients unaffected (immediate 200 response)
Fixes: `async_status_callback`, `graceful_shutdown`, `scale_workers`
Graded against the real injection: correct

**Resilience score: 97.8 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 305/305 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.5 | SLIs back to baseline 32 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 87.5 | 7/8 in-flight orders completed exactly once; lost silently 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-050622 / exp-01 · Worker pod failure (hung worker) → n8n-worker (completed)

**What was injected:** Chaos Mesh swaps one worker's container for a pause image for 30-60 s: the worker stops working but stays Ready.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test resilience of n8n-worker to transient failures under load, affecting edge:n8n-worker->mock-api and edge:n8n-worker->redis
Cites: node:n8n-worker, edge:n8n-worker->mock-api, edge:n8n-worker->redis, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: The n8n system will maintain >=90% job execution success rate (mock-api calls) even when one worker pod is non-responsive for 30 seconds, leveraging the remaining worker pod (node:n8n-worker replica).
- Expected symptoms seen: none
- Expected but not seen: latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 30.4 s |
| SLIs back to baseline | 30.4 s |
| last in-flight order resolved | 4.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 9 |

**Agent 2 (blind, llm:api:ministral-14b-latest)**: Missing Python 3 in n8n-worker pods caused worker startup failures, leading to job processing delays and latency degradation for W1 clients.
Failure chain: n8n-worker pods lack Python 3 → workers fail to start (ev:logs:n8n-worker-6994cf9db5-7f7m9) → 4 worker restarts occur (ev:restarts_delta) → active consumers drop temporarily → Redis queue backlog persists due to stalled workers (ev:trace: q_active fluctuates) → W1 clients experience latency spikes (p95 ~4048ms vs. baseline 4055ms) (ev:baseline, ev:trace) → System recovers once workers stabilize (ev:timeline: t_slis_recovered=30.4)
Fixes: `worker_health_probe`, `redis_timeout_threshold`, `scale_workers`
Graded against the real injection: correct

**Resilience score: 100.0 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 305/305 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.9 | SLIs back to baseline 30 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 9/9 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-050940 / exp-01 · E5 · Mock API returns HTTP 500/503 → mock-api (completed)

**What was injected:** Chaos Mesh rewrites the mock API's responses on /work to HTTP 500 (30 s) or 503 (60 s).

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test HTTP 500 fault on mock-api to observe n8n-worker's retry/resilience on edge:n8n-worker->mock-api
Cites: node:mock-api, node:n8n-worker, edge:n8n-worker->mock-api, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main', 'node:n8n-worker'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: n8n-worker will retry failed HTTP requests to mock-api within 30s, and the system will recover without client-side failures in W1/W2
- Expected symptoms seen: silent_async_failure, sync_errors
- Expected but not seen: none
- Not anticipated by the knowledge base: latency_degradation, queue_backlog

| Timeline (s after injection) | |
|---|---|
| first symptom | 13.2 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 42.6 s |
| SLIs back to baseline | 48.7 s |
| last in-flight order resolved | 3.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Detectors fired**

- High: `latency_degradation`: W1 p95 peaked at 23.7 s vs baseline 4.1 s
- High: `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed
- High: `sync_errors`: 130 of 389 W1 requests failed after the start (33%)
- Medium: `queue_backlog`: waiting jobs rose to 39 while workers were busy (active up to 20): demand exceeded throughput

**Agent 2 (blind, llm:api:ministral-14b-latest)**: Mock-api pod unreadiness caused HTTP call timeouts in n8n-worker pods, leading to stalled job processing and queue backlog.
Failure chain: Mock-api pod readiness probe failed (ev:events) → n8n-worker pods timed out on HTTP calls to mock-api (ev:logs:n8n-worker-6994cf9db5-7f7m9, ev:logs:n8n-worker-6994cf9db5-gprjc) → Job processing stalled, causing queue backlog (ev:trace, det:queue_backlog) → W1 clients experienced degraded latency and failures (det:latency_degradation, det:sync_errors) → W2 clients remained unaware of failures due to silent async responses (det:silent_async_failure)
Fixes: `http_retry_backoff`, `async_status_callback`, `worker_health_probe`
Graded against the real injection: correct

**Resilience score: 64.9 / 100 (fragile)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 66.6 | 259/389 W1 responses successful across post-injection samples |
| self recovery | 25% | 93.8 | SLIs back to baseline 49 s after injection, no help |
| latency | 20% | 0 | peak p95 23.7 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-051312 / exp-01 · E4 · Mock API latency → mock-api (aborted)

**What was injected:** Chaos Mesh delays every request to the mock API's /work by 2, 10 or 30 s (30 s passes the HTTP node timeout).

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test latency impact on W1/W2 via edge:n8n-worker->mock-api (timeout=30s) without violating sync constraints
Cites: node:mock-api, edge:n8n-worker->mock-api, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main', 'node:n8n-worker'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: A 2-second latency increase in mock-api will cause n8n-worker to queue more jobs in redis without triggering timeouts in W1/W2
- Expected symptoms seen: none
- Expected but not seen: latency_degradation
- Not anticipated by the knowledge base: backlog_not_drained, db_errors

| Timeline (s after injection) | |
|---|---|
| first symptom | 13.2 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | n/a |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 3.0 s |
| automatic stop | w1_error_rate 1.00 > 0.5 |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Detectors fired**

- High: `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed
- High: `backlog_not_drained`: queue still 34 waiting at end of window; SLIs never returned to baseline
- High: `db_errors`: 24 database error log lines, e.g. 'connect ECONNREFUSED 10.96.40.51:8080'
- Medium: `sync_errors`: 6 of 41 W1 requests failed after the start (15%)
- Medium: `queue_backlog`: waiting jobs rose to 39 while workers were busy (active up to 20): demand exceeded throughput

**Agent 2 (blind, fallback:invalid (finding cites unknown evidence ['ev:baseline.w1', 'ev:baseline.w2']))**: The Postgres database failed or refused writes (database errors in the logs), so executions could not be saved.
Failure chain: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed → queue still 34 waiting at end of window; SLIs never returned to baseline → 6 of 41 W1 requests failed after the start (15%) → waiting jobs rose to 39 while workers were busy (active up to 20): demand exceeded throughput
Fixes: `db_connection_pooling`, `autoscale_workers`, `postgres_ha`
Graded against the real injection: missed

**Resilience score: 84.0 / 100 (degraded but recovered)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 85.4 | 35/41 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-051604 / exp-01 · E7 · Postgres overload → postgres (completed)

**What was injected:** A pgbench Job loads the n8n database: 4 or 8 CPU-heavy clients, or 85 of the 100 connections held, for 45 s.

**Why Agent 1 chose it** (fallback:invalid_plan): node:postgres is on 2/2 workflow paths, single point of failure (edge:n8n-main->postgres, edge:n8n-worker->postgres); blast radius ['n8n-main', 'n8n-worker']; untested (score 0.85)
Cites: node:postgres, edge:n8n-main->postgres, edge:n8n-worker->postgres

**Safety guard:** approve · blast radius ['node:n8n-main', 'node:n8n-worker'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: queries and execution saves slow down (CPU) or new connections are refused (held connections): W1 p95 rises or W1 fails; W2 keeps returning 200 while work backs up
- Expected symptoms seen: none
- Expected but not seen: latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 45.7 s |
| SLIs back to baseline | 56.9 s |
| last in-flight order resolved | 3.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Agent 2 (blind, llm:api:ministral-14b-latest)**: No real degradation or incident detected. System operated within expected behavior for queue-mode processing.
Failure chain: No anomalies detected in metrics, logs, or system state. → W1/W2 clients behaved as expected (W1 waits, W2 responds immediately). → Queue drained successfully (`t_queue_drained=45.7s`), no backlog or errors. → All components (Redis, Postgres, workers) remained stable (no restarts, `unready=[]`). → No symptoms or failures reported (`saw_symptom=false`, `fates` all zero except `completed_once`).
Fixes: 
Graded against the real injection: missed

**Resilience score: 97.5 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 434/434 W1 responses successful across post-injection samples |
| self recovery | 25% | 91.0 | SLIs back to baseline 57 s after injection, no help |
| latency | 20% | 100 | peak p95 4.6 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-051950 / exp-01 · E3 · Traffic spike → n8n-main (completed)

**What was injected:** Our load generator raises webhook traffic from 2 to 6 or 10 requests/s for 60 s.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test n8n-main's resilience to moderate traffic spikes under normal conditions (edge:client->n8n-main, edge:n8n-main->redis, edge:n8n-main->postgres).
Cites: node:n8n-main, edge:client->n8n-main, edge:n8n-main->redis, edge:n8n-main->postgres, wf:W1, wf:W2

**Safety guard:** approve · blast radius [] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: A 3x traffic spike (6 requests/s) will not cause n8n-main to drop enqueued jobs (edge:n8n-main->redis) or fail to persist data (edge:n8n-main->postgres) within 60s.
- Expected symptoms seen: queue_backlog
- Expected but not seen: latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | 8.2 s |
| target replaced/Ready | 0.0 s |
| queue back to baseline | 79.2 s |
| SLIs back to baseline | 79.2 s |
| last in-flight order resolved | 4.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 9 |

**Detectors fired**

- Medium: `queue_backlog`: waiting jobs rose to 59 while workers were busy (active up to 20): demand exceeded throughput

**Agent 2 (blind, llm:api:ministral-14b-latest)**: Transient dependency (mock-api or postgres) slowdown or timeout causing worker stalls and queue backlog
Failure chain: Dependency (mock-api or postgres) slows down or times out → Workers stall while waiting for dependency responses, reducing throughput → Queue backlog grows as new jobs arrive faster than workers can process them (q_wait rises to 59) → W2 clients stop receiving immediate 200s (w2_ok drops from 15 to 10) → W1 clients experience degraded latency (p95 rises to ~4693ms) → System recovers once dependency stabilizes (~80s later)
Fixes: `http_retry_backoff`, `http_timeout_budget`, `autoscale_workers`
Graded against the real injection: missed

**Resilience score: 95.4 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 1144/1144 W1 responses successful across post-injection samples |
| self recovery | 25% | 83.6 | SLIs back to baseline 79 s after injection, no help |
| latency | 20% | 100 | peak p95 4.7 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 9/9 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-052352 / exp-01 · Worker-Redis network partition → n8n-worker (completed)

**What was injected:** Chaos Mesh cuts one worker off from Redis (both directions) for 20 or 60 s; the other worker is unaffected.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test Redis partition resilience on one worker while jobs are enqueued/dequeued via edge:n8n-worker->redis and edge:n8n-worker->mock-api
Cites: node:n8n-worker, node:redis, edge:n8n-worker->redis, edge:n8n-worker->mock-api, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: The unaffected worker will continue processing jobs from Redis, but job completion time for W1/W2 will increase due to reduced throughput.
- Expected symptoms seen: none
- Expected but not seen: latency_degradation
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 20.3 s |
| SLIs back to baseline | 20.3 s |
| last in-flight order resolved | 3.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Agent 2 (blind, llm:api:ministral-14b-latest)**: No degradation or failure detected in the system. All metrics, traces, and fate outcomes align with expected behavior for an n8n queue-mode setup with W1/W2 clients.
Failure chain: System operates normally with no errors or failures. → W1 clients wait for async results (p95 ~4050ms), W2 clients receive immediate 200s (p95 ~26ms). → Jobs are enqueued in Redis, processed by workers, and written to Postgres without loss or duplication. → No symptoms (`saw_symptom=false`), no restarts, and no anomalies in logs or metrics. → Baseline and trace data confirm stable performance and 100% success rates.
Fixes: 
Graded against the real injection: missed

**Resilience score: 100.0 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 250/250 W1 responses successful across post-injection samples |
| self recovery | 25% | 100.0 | SLIs back to baseline 20 s after injection, no help |
| latency | 20% | 100 | peak p95 4.3 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-052657 / exp-01 · E2 · Redis pod kill → redis (aborted)

**What was injected:** Delete the Redis pod (no persistence) while jobs are queued and running.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test Redis queue failure impact on n8n-worker and mock-api processing under load
Cites: node:redis, edge:n8n-main->redis, edge:n8n-worker->redis, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main', 'node:n8n-worker'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: Redis pod failure will cause pending jobs in n8n-worker to fail with dequeue timeouts, triggering retries or cascading failures in mock-api calls (edge:n8n-worker->mock-api) within 30s, while n8n-main remains operational but enqueues new jobs slowly due to Redis unavailability.
- Expected symptoms seen: latency_degradation
- Expected but not seen: queue_unreachable
- Not anticipated by the knowledge base: ingress_down, phantom_success, sync_errors

| Timeline (s after injection) | |
|---|---|
| first symptom | 11.3 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 1.0 s |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 11.1 s |
| automatic stop | w1_p95_ms 23958 > 20000 |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 5 |
| phantom success | 3 |

**Detectors fired**

- Critical: `ingress_down`: W2 webhooks failed too in 20 samples: requests were not even accepted
- High: `latency_degradation`: W1 p95 peaked at 24.0 s vs baseline 4.1 s
- High: `dependency_restart`: n8n-main restarted 1x
- High: `sync_errors`: 75 of 124 W1 requests failed after the start (60%)
- Medium: `phantom_success`: 3 clients got an error but the work was done

**Agent 2 (blind, llm:api:ministral-14b-latest)**: Redis pod restart caused a dependency failure for n8n-main and n8n-worker, leading to lost queue state and transient connectivity issues.
Failure chain: Redis pod restart (ev:events) triggers readiness probe failures (ev:logs:redis-6c4795cc9b-lsglf). → n8n-main and n8n-worker fail to connect to Redis (ev:logs:n8n-main-75bd5c8c86-77j48, ev:logs:n8n-worker-6994cf9db5-7f7m9), causing restarts (ev:restarts_delta). → Queue state is lost, leading to phantom successes (ev:fates: 'phantom_success': 3) and W1/W2 failures (ev:trace). → Latency degrades due to retries and lost work (det:latency_degradation). → W2 clients fail to accept requests (det:ingress_down), and W1 clients experience sync errors (det:sync_errors).
Fixes: `redis_persistence`, `redis_timeout_threshold`, `worker_health_probe`
Graded against the real injection: correct

**Resilience score: 40.2 / 100 (failing)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 39.5 | 49/124 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 0 | peak p95 24.0 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 62.5 | 5/8 in-flight orders completed exactly once; phantom success 3 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-052955 / exp-01 · E10 · n8n-main pod kill → n8n-main (completed)

**What was injected:** Delete the single n8n-main pod: webhooks are not accepted until the replacement is Ready.

**Why Agent 1 chose it** (fallback:invalid_plan): node:n8n-main is on 2/2 workflow paths, single point of failure (edge:client->n8n-main); blast radius []; untested (score 0.62)
Cites: node:n8n-main, edge:client->n8n-main

**Safety guard:** approve · blast radius [] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: webhook calls fail until n8n-main is back; jobs already queued still finish on the workers
- Expected symptoms seen: ingress_down, sync_errors
- Expected but not seen: none
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | 1.0 s |
| target replaced/Ready | 11.2 s |
| queue back to baseline | 11.2 s |
| SLIs back to baseline | 26.4 s |
| last in-flight order resolved | 3.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Detectors fired**

- Critical: `ingress_down`: W2 webhooks failed too in 7 samples: requests were not even accepted
- High: `dependency_restart`: n8n-main restarted -1x
- Medium: `sync_errors`: 45 of 280 W1 requests failed after the start (16%)

**Agent 2 (blind, llm:api:ministral-14b-latest)**: n8n-main pod restart disrupted webhook processing, causing temporary unavailability of the ingress endpoint and subsequent sync errors for W1 clients.
Failure chain: n8n-main pod restart triggered by K8s (ev:events, ev:restarts_delta) → Webhook endpoint (W2) became unavailable during restart (det:ingress_down, ev:trace) → W1 clients experienced sync errors due to delayed job processing (det:sync_errors, ev:trace) → Queue backlog drained slowly as n8n-main recovered (ev:trace, ev:timeline) → System stabilized once n8n-main was fully ready (t=11.2s) and queue was drained (t=11.2s)
Fixes: `webhook_processors`, `redis_timeout_threshold`, `queue_depth_alert`
Graded against the real injection: correct

**Resilience score: 93.9 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 83.9 | 235/280 W1 responses successful across post-injection samples |
| self recovery | 25% | 100.0 | SLIs back to baseline 26 s after injection, no help |
| latency | 20% | 96.6 | peak p95 6.6 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-053323 / exp-01 · E11 · Postgres pod kill → postgres (aborted)

**What was injected:** Delete the single Postgres pod: nothing can be saved until it restarts and recovers.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test PostgreSQL SPOF resilience; critical for `edge:n8n-main->postgres` and `edge:n8n-worker->postgres`
Cites: node:postgres, edge:n8n-main->postgres, edge:n8n-worker->postgres, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main', 'node:n8n-worker'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: PostgreSQL pod restart will cause `n8n-main` and `n8n-worker` to fail gracefully, with `client` workflows `W1` and `W2` eventually recovering after PostgreSQL recovery (client-visible delay ≤ 5 minutes)
- Expected symptoms seen: db_errors, sync_errors
- Expected but not seen: none
- Not anticipated by the knowledge base: backlog_not_drained, ingress_down, latency_degradation

| Timeline (s after injection) | |
|---|---|
| first symptom | 1.0 s |
| target replaced/Ready | 2.0 s |
| queue back to baseline | 2.0 s |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 56.0 s |
| automatic stop | w1_p95_ms 24062 > 20000 |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 3 |
| lost silently | 1 |
| phantom success | 1 |
| lost visible | 2 |
| success without row | 1 |

**Detectors fired**

- Critical: `lost_work`: 3 in-flight requests never produced a row (1 after a 200 OK)
- Critical: `ingress_down`: W2 webhooks failed too in 4 samples: requests were not even accepted
- High: `latency_degradation`: W1 p95 peaked at 24.1 s vs baseline 4.1 s
- High: `backlog_not_drained`: queue still 15 waiting at end of window; SLIs never returned to baseline
- High: `sync_errors`: 18 of 29 W1 requests failed after the start (62%)
- High: `db_errors`: 4 database error log lines, e.g. 'Postgres pool client error: terminating connection due to administrator command'
- Medium: `phantom_success`: 1 clients got an error but the work was done

**Agent 2 (blind, llm:api:ministral-14b-latest)**: Postgres pod restart (ev:events) caused database unavailability, triggering worker timeouts and queue stagnation.
Failure chain: Postgres pod restart (ev:events) → DB unavailability (ev:logs:n8n-worker: 'Database ping failed') → Workers fail to connect to DB (ev:logs:n8n-worker: 'timeout exceeded when trying to connect') → job execution timeouts (ev:logs:n8n-main: 'Problem with execution 4527') → Queue backlog grows (ev:trace: q_wait=21) as workers stall (ev:trace: q_active=20) → W1 clients timeout waiting for results (ev:trace: w1_err=3 at t=4.3s), W2 clients fail intermittently (ev:trace: w2_ok drops) → Lost work and phantom successes (ev:fates: 'lost_silently': 1, 'phantom_success': 1) due to incomplete DB writes
Fixes: `postgres_ha`, `db_connection_pooling`, `autoscale_workers`
Graded against the real injection: correct

**Resilience score: 35.5 / 100 (failing)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 37.9 | 11/29 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 0 | peak p95 24.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 37.5 | 3/8 in-flight orders completed exactly once; lost silently 1, phantom success 1, lost visible 2, success without row 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

### r1011-053831 / exp-01 · E8 · Postgres disk full → postgres (completed)

**What was injected:** A Job fills the real (512 MiB) Postgres disk with a ballast file for 40 s, leaving 4 MiB or nothing free.

**Why Agent 1 chose it** (llm:api:ministral-14b-latest): Test PostgreSQL disk pressure impact on enqueue/dequeue workflows (W1/W2) via edge:n8n-main->postgres and edge:n8n-worker->postgres
Cites: node:postgres, edge:n8n-main->postgres, edge:n8n-worker->postgres, wf:W1, wf:W2

**Safety guard:** approve · blast radius ['node:n8n-main', 'node:n8n-worker'] · abort if W1 error rate > 0.5, p95 > 20000 ms, queue > 500, total > 180 s

**Expected vs observed**

- Hypothesis: PostgreSQL disk pressure (4 MiB free) will cause n8n-worker job failures due to insert timeouts, propagating to client timeouts in W1 and silent failures in W2
- Expected symptoms seen: none
- Expected but not seen: db_errors
- Not anticipated by the knowledge base: none

| Timeline (s after injection) | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 2.0 s |
| queue back to baseline | 55.8 s |
| SLIs back to baseline | 55.8 s |
| last in-flight order resolved | 3.0 s |
| manual action needed | False |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

**Agent 2 (blind, llm:api:ministral-14b-latest)**: No degradation or failure detected: the system operated normally with expected queue-mode behavior (W1 latency ~4s, W2 instant responses).
Failure chain: System started with baseline metrics (W1 p95=4055ms, W2 p95=26.7ms). → Trace confirmed stable queue processing (q_active=8, q_wait=0, no errors). → No symptoms or anomalies recorded (`saw_symptom: false`). → All components (Redis, Postgres, workers) operational with no restarts. → Diagnosis: No failure or degradation occurred.
Fixes: 
Graded against the real injection: missed

**Resilience score: 98.6 / 100 (resilient)** · coverage 90% of weights

| Component | Weight | Score | Evidence |
|---|---|---|---|
| availability | 30% | 100.0 | 425/425 W1 responses successful across post-injection samples |
| self recovery | 25% | 94.8 | SLIs back to baseline 56 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

## 4. Weaknesses, ranked by severity

| Severity | Weakness | Seen in | Evidence | Fixes |
|---|---|---|---|---|
| Critical | `lost_work` | 4× | 1 in-flight requests never produced a row (1 after a 200 OK) | `redis_persistence`, `async_status_callback`, `graceful_shutdown`, `http_retry_backoff`, `postgres_ha` |
| Critical | `ingress_down` | 3× | W2 webhooks failed too in 20 samples: requests were not even accepted | `webhook_processors` |
| High | `sync_errors` | 5× | 130 of 389 W1 requests failed after the start (33%) | `http_retry_backoff`, `http_timeout_budget`, `db_connection_pooling`, `postgres_ha` |
| High | `latency_degradation` | 3× | W1 p95 peaked at 23.7 s vs baseline 4.1 s | `scale_workers`, `stalled_job_tuning`, `http_timeout_budget`, `db_connection_pooling`, `autoscale_workers` |
| High | `backlog_not_drained` | 2× | queue still 34 waiting at end of window; SLIs never returned to baseline | `scale_workers`, `queue_depth_alert`, `autoscale_workers` |
| High | `db_errors` | 2× | 24 database error log lines, e.g. 'connect ECONNREFUSED 10.96.40.51:8080' | `db_connection_pooling`, `disk_usage_alert`, `postgres_ha` |
| High | `dependency_restart` | 2× | n8n-main restarted 1x | `redis_timeout_threshold`, `webhook_processors` |
| High | `silent_async_failure` | 2× | W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed | `async_status_callback` |
| Medium | `queue_backlog` | 3× | waiting jobs rose to 39 while workers were busy (active up to 20): demand exceeded throughput | `autoscale_workers` |
| Medium | `phantom_success` | 2× | 3 clients got an error but the work was done | `idempotency_keys` |

## 5. Recommended fixes (advisory; an engineer applies them)

| # | Fix | Recommended by | Addresses | Effort / impact | Verify |
|---|---|---|---|---|---|
| 1 | Highly available Postgres with automatic failover (`postgres_ha`) | Agent 2 (3×) | db_errors, lost_work, sync_errors | high / high | `re-run postgres_pod_kill: failover in seconds, orders keep being saved` |
| 2 | Give async (W2) clients a way to learn the real outcome (`async_status_callback`) | Agent 2 (3×) | lost_work, silent_async_failure | medium / high | `reconciliation gap (accepted W2 without a row after 30 s) stays 0 during E1` |
| 3 | Keep graceful shutdown for planned worker stops (`graceful_shutdown`) | Agent 2 (3×) | lost_work | low / low | `graceful worker stop loses no jobs` |
| 4 | Worker readiness that checks Redis, not just the process (`worker_health_probe`) | Agent 2 (3×) | - | low / medium | `worker goes NotReady when Redis is unreachable` |
| 5 | Scale workers on queue depth (`autoscale_workers`) | Agent 2 (3×) | backlog_not_drained, latency_degradation, queue_backlog | medium / high | `re-run traffic_spike x5: workers scale out, the backlog drains during the spike` |
| 6 | Raise QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD above typical Redis restart time (`redis_timeout_threshold`) | Agent 2 (3×) | dependency_restart | low / medium | `n8n-main restart count unchanged during E2` |
| 7 | Run at least 2 n8n workers (`scale_workers`) | Agent 2 (3×) | backlog_not_drained, latency_degradation | low / high | `kubectl -n n8n-chaos get deploy n8n-worker  # READY 2/2, then re-run E1` |
| 8 | Bound and pool database connections (`db_connection_pooling`) | Agent 2 (2×) | db_errors, latency_degradation, sync_errors | medium / high | `re-run postgres_overload hold85: n8n keeps saving orders, no 'too many clients' errors` |
| 9 | Retry the downstream HTTP call with backoff (`http_retry_backoff`) | Agent 2 (2×) | lost_work, sync_errors | low / high | `re-run mockapi_http_500 / mockapi_http_abort: W1 errors drop to ~0 for a 30 s outage` |
| 10 | Enable Redis AOF persistence on a PVC (`redis_persistence`) | Agent 2 (2×) | lost_work | medium / high | `redis-cli -p 16379 CONFIG GET appendonly  # yes, then re-run E2` |
| 11 | Run webhook processors separately from n8n-main, more than one (`webhook_processors`) | Agent 2 (1×) | dependency_restart, ingress_down | medium / high | `re-run main_pod_kill: webhooks keep answering` |
| 12 | Give the downstream call a timeout shorter than the client's wait (`http_timeout_budget`) | Agent 2 (1×) | latency_degradation, sync_errors | low / medium | `re-run mockapi_latency past_timeout: W1 gets a fast, explicit error instead of hanging` |
| 13 | Alert on queue depth and on zero active consumers (`queue_depth_alert`) | Agent 2 (1×) | backlog_not_drained | low / medium | `rule fires within 15 s during E1` |
| 14 | Alert on database volume usage and size it with headroom (`disk_usage_alert`) | playbook match | db_errors | low / high | `re-run postgres_disk_full: the alert fires before writes fail` |
| 15 | Make order inserts idempotent on request_id (`idempotency_keys`) | playbook match | phantom_success | low / high | `SELECT request_id, count(*) FROM orders GROUP BY 1 HAVING count(*) > 1  # 0 rows` |
| 16 | Tune lock duration / stalled interval for faster job recovery (`stalled_job_tuning`) | playbook match | latency_degradation | low / medium | `time to last in-flight resolved drops in E1` |

## 6. How the score is computed

R = 0.30·A + 0.25·S + 0.20·L + 0.15·D + 0.10·M, each 0-100 (weights in `chaosctl/scoring.yaml`, formulas in `chaosctl/score.py`).
- A availability: successful W1 responses / all W1 responses after injection.
- S self-recovery: 100 if SLIs are back within 30 s, falling to 70 at the recovery deadline; 60 if pods recovered but SLIs did not within the window; 40 if the watchdog restarted something; 0 if LOCKED.
- L latency: 100 if peak W1 p95 ≤ 1.5× baseline, 0 at the abort limit, linear in between.
- D data integrity: in-flight orders completed exactly once / all in-flight orders.
- M detectability: 0 if n8n-main stayed Ready through a consumer/queue outage, 100 if health checks showed it, n/a if no such outage happened.
Components without data are n/a and the other weights are rescaled (coverage is shown). The score compares experiments on this testbed; it is not a certified reliability measure.
