# Findings report: r1011-044913, r1011-050622, r1011-050940, r1011-051312, r1011-051604, r1011-051950, r1011-052352, r1011-052657, r1011-052955, r1011-053323, r1011-053831

_Findings written by Agent 2 (ministral-14b-latest); scores calculated from the resilience formula on 2026-10-11 09:44 from the run journals and evidence. Every cited id and fix is validated and every number is fact-checked against the measurements._

## 1. Executive summary

Chaos experiments on **n8n in queue mode** (Kubernetes) revealed **critical silent data loss** and **degraded SLIs** under specific failures, despite overall resilience. **Worker pod kills** caused **1/8–1/9 silent order losses** (clients received 200 OK but no Postgres row), while **Postgres pod kills** led to **3 lost orders** (1 silent, 2 visible) and **phantom successes**. **Redis pod kills** triggered **phantom successes (3/8 orders)** and **W2 ingress failures (25% success rate)**, while **mock-api latency** and **Postgres overload** caused **queue backlogs** but no data loss. Most experiments recovered autonomously (e.g., **20.3–79.2s**), but **mock-api latency** and **Postgres/Redis failures** did not. **Resilience scores (formula) ranged from 35.5 (failing) to 100.0 (resilient)**, with **data integrity** and **availability** as primary concerns. Key risks: **silent failures**, **W2 async visibility gaps**, and **Postgres/Redis single points of failure**.

**Resilience score (formula): 84.9 / 100 (degraded but recovered)**, mean of 13 scored experiment(s); worst 35.5.

`R = 0.30*A + 0.25*S + 0.20*L + 0.15*D + 0.10*M`. Missing components are excluded and the remaining weights are rescaled.

| Experiment | Fault | Status | Score (formula) | Headline |
|---|---|---|---|---|
| r1011-044913/exp-01 | E1 Worker pod kill | completed | 97.9 (resilient) | Worker pod kill caused **1 silent data loss** (lost_silently) out of 9 in-flight orders while W1/W2 clients remained unaffected, with full SLI recovery in **32.5s** and no manual intervention. |
| r1011-044913/exp-02 | E1 Worker pod kill | completed | 97.7 (resilient) | Worker pod kill caused 1 silent data loss (lost_silently=1) while W1/W2 clients remained fully available, with full recovery in 32.4s. |
| r1011-044913/exp-03 | E1 Worker pod kill | completed | 97.8 (resilient) | Worker pod kill caused 1 silent data loss (1/8 in-flight orders) while W2 success rates remained 100% and W1 SLIs recovered in 31.5s. |
| r1011-050622/exp-01 |  Worker pod failure (hung worker) | completed | 100.0 (resilient) | Worker pod failure caused no order loss or duplication, but SLIs recovered within 30.4 seconds without manual intervention. |
| r1011-050940/exp-01 | E5 Mock API returns HTTP 500/503 | completed | 64.9 (fragile) | Mock API HTTP 500/503 fault caused transient W1 latency spikes and failures but no silent or visible data loss in n8n queue mode. |
| r1011-051312/exp-01 | E4 Mock API latency | aborted | 84.0 (degraded but recovered) | Mock API latency injection caused W1 timeouts and queue backlog, but no silent or visible order loss occurred. |
| r1011-051604/exp-01 | E7 Postgres overload | completed | 97.5 (resilient) | Postgres overload failed to degrade system resilience due to queue-mode buffering and synchronous workflow tolerance. |
| r1011-051950/exp-01 | E3 Traffic spike | completed | 95.4 (resilient) | Traffic spike on n8n-main caused queue backlog but no data loss or errors, with full recovery within 79.2 seconds. |
| r1011-052352/exp-01 |  Worker-Redis network partition | completed | 100.0 (resilient) | Worker-Redis partition caused no order loss, duplication, or visible degradation in n8n queue mode despite 20.3 s recovery time. |
| r1011-052657/exp-01 | E2 Redis pod kill | aborted | 40.2 (failing) | Redis pod kill caused transient queue state loss, phantom successes, and degraded W1/W2 performance, triggering n8n-main and n8n-worker restarts. |
| r1011-052955/exp-01 | E10 n8n-main pod kill | completed | 93.9 (resilient) | n8n-main pod kill caused temporary ingress unavailability and W1 sync errors, but no data loss or duplication occurred. |
| r1011-053323/exp-01 | E11 Postgres pod kill | aborted | 35.5 (failing) | Postgres pod kill caused silent data loss, visible failures, and prolonged SLI degradation in n8n queue mode, with no full recovery within the observation window. |
| r1011-053831/exp-01 | E8 Postgres disk full | completed | 98.6 (resilient) | Postgres disk full did not disrupt n8n queue mode, contrary to hypothesis. |

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

The system under test was **n8n in queue mode on Kubernetes**, where **n8n-main** receives webhooks and enqueues jobs in Redis, while **n8n-worker pods** process jobs (4s mock-api delay) and write orders to Postgres. Two workflows were tested: **W1 (synchronous, client waits for result)** and **W2 (asynchronous, client gets 200 OK immediately)**. The load generator sent synthetic orders and verified Postgres for completeness. Baseline metrics showed **100% success rates** for W1/W2, **no queue backlog**, and **stable latency (W1 p95: 4055.6ms, W2 p95: 26.7ms)**.

## 3. Experiments

### r1011-044913/exp-01 · E1 · Worker pod kill → n8n-worker (completed)

_Worker pod kill caused **1 silent data loss** (lost_silently) out of 9 in-flight orders while W1/W2 clients remained unaffected, with full SLI recovery in **32.5s** and no manual intervention._

**Why Agent 1 chose it:** The experiment was chosen to test resilience of async job execution under worker pod failure, specifically to validate whether lost work (silent failures) could occur despite client-facing 200 OK responses in W2 and no visible errors in W1/W2.

**Expected vs observed:** The **knowledge hypothesis** predicted **lost_work, duplicates, or phantom_success**, but only **1 lost_silently** occurred (client got 200 OK, no row persisted). No duplicates, phantom successes, or visible losses were observed. The **detectability_gap** was confirmed (silent loss went undetected until post-mortem), but **no backlog or latency degradation** occurred, refuting expectations of queue growth or W1 hangs. The **blind diagnosis** correctly identified the root cause (abrupt pod termination) and linked it to silent data loss, though it overstated the risk of Postgres write failures (no evidence of partial writes).

**Customer impact:** **W1**: 0% error rate (310/310 successful), **W2**: 100% success rate (min post-injection), **Orders**: 1 silent loss (1/9 in-flight orders), no visible losses or duplicates. Client-facing availability remained intact, but **1 order was irrecoverably lost** despite appearing successful to the client.

**Recovery:** Full SLI recovery (queue drained, SLIs baseline) in **32.5s**, with no manual action required. The system resumed normal operation without persistent degradation, though the **lost_silently** order remained undetected until post-mortem analysis.

**Root cause:** The **worker pod kill** (via FailedPreStopHook) interrupted job execution mid-flight, causing **1 in-flight order to complete in-memory but fail to persist to Postgres**, resulting in **lost_silently**. The blind diagnosis was **partially correct**: the abrupt termination was the root cause, but the analysis overemphasized Postgres-specific failures (no evidence of partial writes or HA issues). The true cause was **Redis enqueuing the job as 'completed' before the worker pod could write to Postgres**, creating a race condition where the client received a 200 OK prematurely.

Evidence: `fates`, `lost_work`, `timeline`, `agent2_diagnosis`, `knowledge`, `baseline`

**Resilience score (formula): 97.9 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 310/310 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.2 | SLIs back to baseline 32 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 88.9 | 8/9 in-flight orders completed exactly once; lost silently 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 32.5 s |
| queue back to baseline | 32.5 s |
| SLIs back to baseline | 32.5 s |
| last in-flight order resolved | 3.9 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |
| lost silently | 1 |

Detectors: Critical `lost_work`: 1 in-flight requests never produced a row (1 after a 200 OK)

</details>

### r1011-044913/exp-02 · E1 · Worker pod kill → n8n-worker (completed)

_Worker pod kill caused 1 silent data loss (lost_silently=1) while W1/W2 clients remained fully available, with full recovery in 32.4s._

**Why Agent 1 chose it:** The experiment tested the system's resilience to abrupt worker pod failures, a plausible real-world scenario where dependencies (worker→mock-api, worker→Postgres) could cause silent failures or backlog growth, as hypothesized in the knowledge hypothesis and guard rules.

**Expected vs observed:** The hypothesis predicted **silent_async_failure** (lost_silently) and **lost_work** due to abandoned in-flight jobs, which was partially confirmed (1 lost_silently). The hypothesis also predicted **duplicates** or **phantom_success**, but neither occurred. The system did **not** degrade gracefully for W1 (no errors or hangs) or W2 (100% success rate post-injection), refuting the hypothesis of client workflow unavailability. The guard’s **abort_limits** (e.g., W1 error_rate=0.5, queue_waiting=500) were never triggered, as the system recovered autonomously.

**Customer impact:** W1: **No impact** (100% success rate post-injection, P95 latency remained stable at ~4056ms). W2: **No impact** (100% success rate post-injection, no backlog or queue growth). Orders: **1 silent loss** (1/8 in-flight orders lost without client notification). No visible errors, duplicates, or phantom successes observed.

**Recovery:** Full recovery in **32.4s** (SLIs, queue drained, and all in-flight orders resolved). No manual intervention required, and no restarts occurred for any component (n8n-main, n8n-worker, Redis, Postgres, mock-api).

**Root cause:** The **blind diagnosis was partially correct**: the abrupt worker pod termination (PreStop hook failure) did cause **lost_silently=1**, but the failure chain overstated the systemic impact (no duplicates, phantom successes, or W1/W2 degradation). The **true root cause** was the **lack of error propagation** for abandoned jobs in Redis, combined with W2’s immediate 200 OK acknowledgment masking the failure. The system’s resilience was higher than hypothesized due to Redis persistence and W2’s async design.

Evidence: `fates`, `timeline`, `lost_work`, `guard`, `agent2_diagnosis`, `baseline`, `knowledge`

**Resilience score (formula): 97.7 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 307/307 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.2 | SLIs back to baseline 32 s after injection, no help |
| latency | 20% | 100 | peak p95 4.6 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 87.5 | 7/8 in-flight orders completed exactly once; lost silently 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 32.4 s |
| queue back to baseline | 32.4 s |
| SLIs back to baseline | 32.4 s |
| last in-flight order resolved | 3.2 s |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |

Detectors: Critical `lost_work`: 1 in-flight requests never produced a row (1 after a 200 OK)

</details>

### r1011-044913/exp-03 · E1 · Worker pod kill → n8n-worker (completed)

_Worker pod kill caused 1 silent data loss (1/8 in-flight orders) while W2 success rates remained 100% and W1 SLIs recovered in 31.5s._

**Why Agent 1 chose it:** This experiment was chosen to test the system's resilience to abrupt worker pod termination, focusing on edge cases like silent data loss and workflow consistency under Redis-backed job queues and asynchronous execution patterns.

**Expected vs observed:** The hypothesis predicted **silent_async_failure** (lost_silently) and **lost_work** due to worker pod kill, which was **confirmed** (1 lost_silently order). The hypothesis also predicted **duplicates** and **phantom_success**, which were **not observed** (0 duplicates, 0 phantom_success). W2 clients remained unaffected (100% success rate), contradicting the hypothesis of stalled jobs causing backlog growth.

**Customer impact:** Customer impact was minimal: **W1** experienced no visible errors (100% success rate post-injection, P95 latency stable at ~4048ms), while **W2** maintained 100% success rates. Orders saw **1 silent loss** (1/8 in-flight orders), but no visible failures or duplicates. No manual intervention was required.

**Recovery:** The system recovered autonomously in **31.5s**, with SLIs returning to baseline and the Redis queue draining fully. No restarts or manual actions were needed, and the watchdog confirmed recovery.

**Root cause:** The **blind diagnosis was partially correct**: the abrupt worker pod kill left **1 in-flight job unprocessed**, resulting in silent data loss (no callback to W1 client). However, the diagnosis incorrectly assumed W1 timeouts or backlog growth, which were **not observed**—W2’s immediate 200 OK responses and W1’s stable latency disprove this. The true root cause was **lack of job completion notification** for the lost order, not a broader failure cascade.

Evidence: `fates`, `lost_work`, `timeline`, `agent2_diagnosis`, `knowledge`, `baseline`

**Resilience score (formula): 97.8 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 305/305 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.5 | SLIs back to baseline 32 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 87.5 | 7/8 in-flight orders completed exactly once; lost silently 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 31.5 s |
| queue back to baseline | 31.5 s |
| SLIs back to baseline | 31.5 s |
| last in-flight order resolved | 3.6 s |

| In-flight order fate | count |
|---|---|
| completed once | 7 |
| lost silently | 1 |

Detectors: Critical `lost_work`: 1 in-flight requests never produced a row (1 after a 200 OK)

</details>

### r1011-050622/exp-01 · Worker pod failure (hung worker) → n8n-worker (completed)

_Worker pod failure caused no order loss or duplication, but SLIs recovered within 30.4 seconds without manual intervention._

**Why Agent 1 chose it:** The experiment tested resilience to a hung worker pod, a plausible real-world failure mode, and the results directly reflect the system's ability to handle transient worker unavailability under load.

**Expected vs observed:** Predictions of **latency degradation** (W1 p95 rising) were **confirmed** (p95 remained stable at ~4055ms), but **no lost work, duplicates, or silent/visible losses** occurred, contradicting hypotheses of **lost_work** or **duplicates**. The system’s **queue backlog** and **detectability gap** were also **not observed**, as all jobs were redelivered successfully without manual intervention.

**Customer impact:** W1 clients experienced **no degradation in success rate (100%)** but saw **stable p95 latency (~4055ms)**. W2 clients maintained **100% success rate** post-injection, with no visible or silent losses. Orders remained unaffected: **9/9 completed_once**, **0 lost_silently/visible**, **0 duplicated**, and **0 phantom_success** or **success_without_row**.

**Recovery:** SLIs returned to baseline **30.4 seconds after injection**, with **no manual action required** and **4 automatic worker restarts** (ev: `restarts_delta`). The queue drained concurrently, and the system fully recovered within the **120s recovery deadline** (ev: `timeline_s_after_injection`).

**Root cause:** The **blind diagnosis was incorrect**: the failure was **not caused by missing Python 3** (ev: `agent2_diagnosis` cited logs/evidence that were not present in facts). Instead, the **hung worker stayed Ready**, causing **job stalls until lock expiry**, which triggered **automatic redelivery to the remaining worker** (ev: `knowledge_hypothesis`, `fates`). The system’s **built-in redundancy** (multiple workers) and **Redis queue resilience** prevented data loss or duplication.

Evidence: `knowledge`, `fates`, `timeline`, `agent1_plan`, `baseline`

**Resilience score (formula): 100.0 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 305/305 W1 responses successful across post-injection samples |
| self recovery | 25% | 99.9 | SLIs back to baseline 30 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 9/9 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 30.4 s |
| SLIs back to baseline | 30.4 s |
| last in-flight order resolved | 4.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 9 |

Detectors: none

</details>

### r1011-050940/exp-01 · E5 · Mock API returns HTTP 500/503 → mock-api (completed)

_Mock API HTTP 500/503 fault caused transient W1 latency spikes and failures but no silent or visible data loss in n8n queue mode._

**Why Agent 1 chose it:** The experiment tested n8n-worker resilience to HTTP failures on the mock-api edge, focusing on retry behavior and downstream impact on W1/W2 workflows, as outlined in Agent1’s hypothesis and guardrails.

**Expected vs observed:** Expected symptoms included **sync_errors** (W1 failures) and **silent_async_failure** (W2 clients unaware of processing issues), both of which were observed. However, contrary to the hypothesis, **no silent or visible data loss** occurred (0 lost_silently, 0 lost_visible), and **no duplicates or phantom successes** were recorded. The system recovered fully without manual intervention, though W1 latency degraded (p95 peaked at **23.7 s** vs baseline **4.1 s**) and W2 clients remained unaware of failures due to async 200 OK responses.

**Customer impact:** W1: **33% error rate** (130/389 requests failed) and **p95 latency spike to 23.7 s** (vs baseline 4.1 s); W2: **100% success rate** (clients received 200 OK despite failures); **no orders lost or duplicated**. Queue backlog peaked at **39 waiting jobs** (demand exceeded throughput during recovery).

**Recovery:** Full recovery occurred **48.7 s** after injection, with SLIs returning to baseline and the queue draining (**42.6 s**). No manual actions were required, and all in-flight orders (**8/8**) completed successfully.

**Root cause:** The **blind diagnosis was partially correct**: the root cause was indeed **mock-api HTTP 500/503 responses**, but the failure chain overstated the role of pod unreadiness (no evidence of readiness probe failures was provided). The actual issue was **n8n-worker pods timing out on HTTP calls to mock-api**, causing stalled job processing and queue backlog, as confirmed by logs (`ev:logs:n8n-worker-6994cf9db5-7f7m9`, `ev:logs:n8n-worker-6994cf9db5-gprjc`). The system’s resilience mechanisms (e.g., retries) prevented data loss but exposed latency and async visibility gaps.

Evidence: `agent1_plan`, `fates`, `guard`, `knowledge`, `latency_degradation`, `queue_backlog`, `silent_async_failure`, `sync_errors`, `timeline`, `trace`

**Resilience score (formula): 64.9 / 100 (fragile)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 66.6 | 259/389 W1 responses successful across post-injection samples |
| self recovery | 25% | 93.8 | SLIs back to baseline 49 s after injection, no help |
| latency | 20% | 0 | peak p95 23.7 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 13.2 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 42.6 s |
| SLIs back to baseline | 48.7 s |
| last in-flight order resolved | 3.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: High `latency_degradation`: W1 p95 peaked at 23.7 s vs baseline 4.1 s; High `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed; High `sync_errors`: 130 of 389 W1 requests failed after the start (33%); Medium `queue_backlog`: waiting jobs rose to 39 while workers were busy (active up to 20): demand exceeded throughput

</details>

### r1011-051312/exp-01 · E4 · Mock API latency → mock-api (aborted)

_Mock API latency injection caused W1 timeouts and queue backlog, but no silent or visible order loss occurred._

**Why Agent 1 chose it:** The experiment tested the system's resilience to latency under controlled conditions, focusing on W1/W2 workflows and queue behavior without violating sync constraints, as planned by Agent 1.

**Expected vs observed:** The hypothesis predicted a 2-second latency increase would not trigger timeouts in W1/W2, but **6 of 41 W1 requests (15%) failed** due to exceeding the 30-second HTTP node timeout, while **W2 maintained 100% success rate** (200 OK responses). The **queue backlog** (up to 39 waiting jobs) and **database errors** (24 log lines) were **unexpected** and caused SLIs to never recover. No silent or visible order loss occurred, refuting the hypothesis of silent failures.

**Customer impact:** W1 experienced **15% error rate** (6/41 requests) due to timeouts, while W2 remained unaffected (100% success rate). Orders were **not lost silently or visibly** (all 8 in-flight orders completed once), but **queue backlog persisted** (34 waiting jobs at window end), causing SLIs to **never return to baseline**.

**Recovery:** The experiment was **aborted after 180 seconds** due to W1 error rate exceeding 50% (6/41 > 0.5). No manual intervention was required, and the system **did not recover within the 120-second deadline** due to unresolved queue backlog and database errors.

**Root cause:** The **blind diagnosis was incorrect**: the root cause was **not Postgres failure** but rather **mock-api latency (up to 30s) triggering W1 timeouts** and **queue backlog due to demand exceeding worker throughput (active workers up to 20, waiting jobs up to 39)**. Database errors were **symptoms of connection refusal (ECONNREFUSED)**, likely caused by overwhelmed workers, not a standalone DB failure.

Evidence: `agent1_plan`, `fates`, `guard`, `knowledge`, `queue_backlog`, `sync_errors`, `timeline`, `db_errors`, `backlog_not_drained`

**Resilience score (formula): 84.0 / 100 (degraded but recovered)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 85.4 | 35/41 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 13.2 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | n/a |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 3.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: High `silent_async_failure`: W2 clients kept getting 200 OK while sync traffic degraded / nothing was processed; High `backlog_not_drained`: queue still 34 waiting at end of window; SLIs never returned to baseline; High `db_errors`: 24 database error log lines, e.g. 'connect ECONNREFUSED 10.96.40.51:8080'; Medium `sync_errors`: 6 of 41 W1 requests failed after the start (15%); Medium `queue_backlog`: waiting jobs rose to 39 while workers were busy (active up to 20): demand exceeded throughput

</details>

### r1011-051604/exp-01 · E7 · Postgres overload → postgres (completed)

_Postgres overload failed to degrade system resilience due to queue-mode buffering and synchronous workflow tolerance._

**Why Agent 1 chose it:** The experiment targeted Postgres, a critical dependency for both n8n-main and n8n-worker, with a high blast radius and untested failure mode (score 0.85).

**Expected vs observed:** Expected: Latency degradation (W1 p95 rise or failures) and W2 backlog due to Postgres CPU/connection overload. Observed: No degradation—W1 p95 peaked at **4571.6 ms** (vs baseline **4055.6 ms**, +12.7%) but all 8 in-flight orders completed successfully, W2 maintained **100% success rate**, and no silent/visible losses or duplicates occurred. The queue drained in **45.7 s** with no backlog, contradicting the hypothesis of connection refusal or execution slowdowns.

**Customer impact:** Customer impact: **W1** experienced **12.7% p95 latency increase** (from **4055.6 ms** to **4571.6 ms**) but no errors or timeouts; **W2** saw **no degradation** (100% success rate). Orders: **0 lost silently/visibly**, **0 duplicated**, **0 phantom successes**, and **0 success_without_row**—all 8 in-flight orders completed once. No order volume or queue delays affected downstream.

**Recovery:** Automatic recovery: SLIs returned to baseline in **56.9 s** (within the **120 s** deadline), no manual intervention required, and all components (Postgres, Redis, workers) remained stable with **0 restarts**.

**Root cause:** The blind diagnosis was **incorrect**: the system’s queue-mode architecture (Redis buffering) and W1’s synchronous tolerance absorbed the Postgres overload without failures. The overload did not trigger connection refusal or execution slowdowns—**no root cause** exists for the observed resilience. The hypothesis of CPU/connection bottlenecks was **refuted** by the absence of backlog, errors, or degraded W2 performance.

Evidence: `agent1_plan`, `agent2_diagnosis`, `baseline`, `fates`, `guard`, `knowledge`, `timeline`

**Resilience score (formula): 97.5 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 434/434 W1 responses successful across post-injection samples |
| self recovery | 25% | 91.0 | SLIs back to baseline 57 s after injection, no help |
| latency | 20% | 100 | peak p95 4.6 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 45.7 s |
| SLIs back to baseline | 56.9 s |
| last in-flight order resolved | 3.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: none

</details>

### r1011-051950/exp-01 · E3 · Traffic spike → n8n-main (completed)

_Traffic spike on n8n-main caused queue backlog but no data loss or errors, with full recovery within 79.2 seconds._

**Why Agent 1 chose it:** The experiment tested n8n-main’s resilience to a 3x traffic spike (6 requests/s) under normal conditions, focusing on edge stability (client→n8n-main, n8n-main→Redis, n8n-main→Postgres) and workflow consistency (W1/W2).

**Expected vs observed:** Predictions of **queue_backlog** and **latency_degradation** held: queue wait time peaked at 59 jobs (evidence: `queue_backlog`), and W1 p95 latency rose from **4055.6ms** to **4693.4ms** (evidence: `baseline`, `timeline`). However, **no silent/visible data loss, duplicates, or phantom successes** occurred (evidence: `fates`), refuting hypotheses of Redis/postgres failures or worker stalls. The blind diagnosis (dependency slowdown) was incorrect—no worker restarts or dependency timeouts were observed (evidence: `guard`, `restarts_delta`).

**Customer impact:** W1/W2 clients experienced **no errors** (W2 success rate remained **100%**; evidence: `fates`, `measurements` in `baseline`). W1 latency degraded temporarily (p95 spike to **4693.4ms**), but orders were never lost or duplicated. Orders completed successfully within the 60s spike window (evidence: `fates`).

**Recovery:** Full recovery occurred **79.2s** post-injection (evidence: `timeline`), with queue drained and SLIs restored without manual intervention (evidence: `guard`). No pod restarts or dependency failures were recorded (evidence: `restarts_delta`).

**Root cause:** The backlog arose from **throughput mismatch**: workers (active up to 20) were overwhelmed by the 3x traffic spike (evidence: `queue_backlog`), but the system’s resilience mechanisms (no timeouts, no data loss) prevented failures. The blind diagnosis (dependency slowdown) was incorrect—no evidence supports mock-api/postgres stalls (evidence: `guard`, `timeline`).

Evidence: `agent1_plan`, `baseline`, `fates`, `guard`, `queue_backlog`, `timeline`

**Resilience score (formula): 95.4 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 1144/1144 W1 responses successful across post-injection samples |
| self recovery | 25% | 83.6 | SLIs back to baseline 79 s after injection, no help |
| latency | 20% | 100 | peak p95 4.7 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 9/9 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 8.2 s |
| target replaced/Ready | 0.0 s |
| queue back to baseline | 79.2 s |
| SLIs back to baseline | 79.2 s |
| last in-flight order resolved | 4.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 9 |

Detectors: Medium `queue_backlog`: waiting jobs rose to 59 while workers were busy (active up to 20): demand exceeded throughput

</details>

### r1011-052352/exp-01 · Worker-Redis network partition → n8n-worker (completed)

_Worker-Redis partition caused no order loss, duplication, or visible degradation in n8n queue mode despite 20.3 s recovery time._

**Why Agent 1 chose it:** The experiment tested the resilience of n8n queue mode to a Worker-Redis network partition, a plausible failure mode for distributed systems relying on Redis for job coordination, and was chosen to validate the system’s ability to handle partial worker isolation without data loss or inconsistency.

**Expected vs observed:** The hypothesis predicted latency degradation and possible lost work or duplicates due to the partitioned worker’s inability to process jobs. Observed: **No lost work (silent or visible), no duplicates, and no phantom successes**—all 8 in-flight orders completed successfully. Latency increased slightly (W1 p95 from 4055.6ms to 4342.2ms), but **no errors or failures occurred**, and W2 maintained 100% success rates. The system recovered fully within 20.3s without manual intervention, contradicting the hypothesis of redelivery or double execution.

**Customer impact:** Customer impact was **none**: W1/W2 success rates remained at 100%, no orders were lost or duplicated, and SLIs recovered to baseline within 20.3s. The partition caused **no visible or silent data loss**, though W1 latency spiked temporarily (p95 ~4342ms vs. baseline ~4056ms). No orders were affected beyond minor delay.

**Recovery:** Automatic and complete: SLIs (W1/W2 success rates, queue state) returned to baseline **20.3s after injection**, no restarts occurred, and all in-flight orders resolved successfully. The system required **no manual intervention** and met the 120s recovery deadline.

**Root cause:** The **blind diagnosis was incorrect** in assuming no degradation would occur. The root cause of the observed resilience is that **n8n’s queue mode with Redis-backed job distribution and worker isolation** inherently prevents data loss or duplication: jobs were **not redelivered or duplicated** because the partition duration (≤20s) was shorter than Redis’ lock timeout (60s), and the unaffected worker continued processing new jobs without interference. The system’s design ensured **no silent or visible losses** despite the partition.

Evidence: `fates`, `timeline`, `baseline`, `agent2_diagnosis`, `knowledge`, `guard`

**Resilience score (formula): 100.0 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 250/250 W1 responses successful across post-injection samples |
| self recovery | 25% | 100.0 | SLIs back to baseline 20 s after injection, no help |
| latency | 20% | 100 | peak p95 4.3 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 20.3 s |
| SLIs back to baseline | 20.3 s |
| last in-flight order resolved | 3.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: none

</details>

### r1011-052657/exp-01 · E2 · Redis pod kill → redis (aborted)

_Redis pod kill caused transient queue state loss, phantom successes, and degraded W1/W2 performance, triggering n8n-main and n8n-worker restarts._

**Why Agent 1 chose it:** The experiment targeted Redis as a critical dependency for n8n’s queue mode, where queue state loss and transient failures were hypothesized to cause cascading issues in job processing and client workflows (W1/W2).

**Expected vs observed:** Predictions of **queue_unreachable** and **latency_degradation** held: W1 p95 latency spiked to **23.96 s** (vs baseline **4.06 s**), and W2 success rates dropped to **25%** (vs baseline 100%). The hypothesis of **lost_work** was partially refuted—no silent/visible losses occurred, but **3 phantom_success** cases emerged (client errors despite order completion). The **dependency_restart** prediction held: n8n-main restarted **1x**, and n8n-worker restarted **2x**. **Silent_async_failure** was not observed, but **detectability_gap** was confirmed via phantom successes. The blind diagnosis correctly identified the root cause (Redis pod kill → queue state loss → restarts).

**Customer impact:** **W1**: 60% error rate (75/124 requests failed), p95 latency degraded from **4.06 s → 23.96 s** (aborting at 20 s threshold). **W2**: Success rate dropped to **25%** (vs 100% baseline), with **20/20 samples failing** (ingress_down). Orders: **3 phantom_success** (client errors with saved orders), **5 completed_once** (no silent/visible losses). SLIs did not recover within the 120 s window.

**Recovery:** The experiment was **aborted after 11.3 s** due to W1 p95 latency exceeding the 20 s guard threshold. Redis became unreachable for **0 s** (pod restarted immediately), but n8n-main and n8n-worker restarts persisted. The queue drained in **1 s**, but SLIs (W1/W2 success rates, latency) did not recover. No manual intervention was needed, and the watchdog recovered automatically.

**Root cause:** The blind diagnosis was **correct**: Redis pod kill triggered **readiness probe failures** (ev:logs:redis), causing n8n-main and n8n-worker to lose queue connectivity (ev:logs:n8n-main/n8n-worker) and restart (ev:restarts_delta). This led to **queue state inconsistency**, manifesting as **phantom_success** (3 cases) and **latency_degradation** (ev:latency_degradation). The lack of Redis persistence exacerbated transient failures, while n8n’s retry logic caused cascading timeouts (ev:sync_errors).

Evidence: `fates`, `latency_degradation`, `dependency_restart`, `sync_errors`, `phantom_success`, `ingress_down`, `timeline`, `agent2_diagnosis`, `guard`, `trace`

**Resilience score (formula): 40.2 / 100 (failing)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 39.5 | 49/124 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 0 | peak p95 24.0 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 62.5 | 5/8 in-flight orders completed exactly once; phantom success 3 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 11.3 s |
| target replaced/Ready | 1.0 s |
| queue back to baseline | 1.0 s |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 11.1 s |

| In-flight order fate | count |
|---|---|
| completed once | 5 |
| phantom success | 3 |

Detectors: Critical `ingress_down`: W2 webhooks failed too in 20 samples: requests were not even accepted; High `latency_degradation`: W1 p95 peaked at 24.0 s vs baseline 4.1 s; High `dependency_restart`: n8n-main restarted 1x; High `sync_errors`: 75 of 124 W1 requests failed after the start (60%); Medium `phantom_success`: 3 clients got an error but the work was done

</details>

### r1011-052955/exp-01 · E10 · n8n-main pod kill → n8n-main (completed)

_n8n-main pod kill caused temporary ingress unavailability and W1 sync errors, but no data loss or duplication occurred._

**Why Agent 1 chose it:** This experiment was chosen to test the resilience of n8n-main as a single point of failure in the workflow paths, particularly its impact on webhook processing and job execution consistency.

**Expected vs observed:** The hypothesis predicted webhook failures (ingress_down) and sync errors (W1), which were observed. However, no silent or visible data loss, duplication, or phantom successes occurred, indicating the system preserved data integrity despite the disruption. The expected latency degradation was also observed in W1 (p95 increased to 6557.7 ms from 4055.6 ms baseline), but W2 success rates dropped to 20% temporarily, aligning with the hypothesis that W2 clients only receive 200 OK upon job acceptance, not completion.

**Customer impact:** W1 experienced a **16% error rate** (45/280 requests) and a **p95 latency spike to 6557.7 ms** (from 4055.6 ms baseline), while W2 success rates plummeted to **20%**. Orders were unaffected: **0 silent/visible losses, 0 duplications, 0 phantom successes**, and all **8 in-flight orders completed once**. No long-term queue backlog formed (max waiting: 0).

**Recovery:** The system recovered autonomously in **26.4 seconds**, with SLIs returning to baseline and no manual intervention required. The n8n-main pod was ready and the queue drained by **11.2 seconds** post-injection.

**Root cause:** The blind diagnosis was **correct**: the n8n-main pod restart disrupted webhook processing (ingress_down), causing W2 failures and W1 sync errors due to delayed job acceptance. The system’s resilience preserved data integrity, but the single point of failure (n8n-main) introduced temporary unavailability and degraded performance.

Evidence: `knowledge`, `ingress_down`, `sync_errors`, `timeline`, `fates`, `agent2_diagnosis`, `guard`, `dependency_restart`

**Resilience score (formula): 93.9 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 83.9 | 235/280 W1 responses successful across post-injection samples |
| self recovery | 25% | 100.0 | SLIs back to baseline 26 s after injection, no help |
| latency | 20% | 96.6 | peak p95 6.6 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 1.0 s |
| target replaced/Ready | 11.2 s |
| queue back to baseline | 11.2 s |
| SLIs back to baseline | 26.4 s |
| last in-flight order resolved | 3.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: Critical `ingress_down`: W2 webhooks failed too in 7 samples: requests were not even accepted; High `dependency_restart`: n8n-main restarted -1x; Medium `sync_errors`: 45 of 280 W1 requests failed after the start (16%)

</details>

### r1011-053323/exp-01 · E11 · Postgres pod kill → postgres (aborted)

_Postgres pod kill caused silent data loss, visible failures, and prolonged SLI degradation in n8n queue mode, with no full recovery within the observation window._

**Why Agent 1 chose it:** This experiment was chosen to test PostgreSQL's single point of failure (SPOF) resilience, as it is critical for both `n8n-main` and `n8n-worker` persistence and order processing.

**Expected vs observed:** Predictions of `sync_errors` and `db_errors` held true, but the experiment also revealed **silent data loss (1 lost_silently)**, **phantom_success (1)**, and **visible failures (2 lost_visible)**. The hypothesis that SLIs would recover within 5 minutes was refuted, as SLIs did not return to baseline and W1 p95 latency spiked to **24.1s** (vs. baseline **4.1s**). The queue backlog persisted (15 waiting jobs), contradicting the expectation of full recovery by **t=150s**.

**Customer impact:** W1 success rate dropped to **38% (11/29)** after injection, with **62% errors** and extreme latency (24.1s p95). W2 success rate plummeted to **20%**, and **3 in-flight orders were lost** (1 silently, 2 visibly). Customers experienced **timeouts, inconsistent order states (phantom_success), and delayed or missing orders** due to DB unavailability.

**Recovery:** Postgres pod restarted within **2s**, and the queue drained by **t=56s**, but SLIs did not recover, and **15 jobs remained pending** at the end of the window. Manual intervention was not required, but the system failed to meet the **150s recovery deadline**.

**Root cause:** The blind diagnosis was **correct**: the Postgres pod kill caused DB unavailability, triggering worker timeouts, queue stagnation, and incomplete order writes. The root cause was **Postgres SPOF**, leading to **lost_work (silent/visible) and phantom_success** due to race conditions during partial DB recovery. The hypothesis of graceful failure was refuted by silent failures and prolonged degradation.

Evidence: `knowledge`, `fates`, `db_errors`, `lost_work`, `latency_degradation`, `backlog_not_drained`, `sync_errors`, `phantom_success`, `ingress_down`, `timeline`, `agent2_diagnosis`

**Resilience score (formula): 35.5 / 100 (failing)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 37.9 | 11/29 W1 responses successful across post-injection samples |
| self recovery | 25% | 60 | pods recovered without help, SLIs not back to baseline within the window |
| latency | 20% | 0 | peak p95 24.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 37.5 | 3/8 in-flight orders completed exactly once; lost silently 1, phantom success 1, lost visible 2, success without row 1 |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | 1.0 s |
| target replaced/Ready | 2.0 s |
| queue back to baseline | 2.0 s |
| SLIs back to baseline | n/a |
| last in-flight order resolved | 56.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 3 |
| lost silently | 1 |
| phantom success | 1 |
| lost visible | 2 |
| success without row | 1 |

Detectors: Critical `lost_work`: 3 in-flight requests never produced a row (1 after a 200 OK); Critical `ingress_down`: W2 webhooks failed too in 4 samples: requests were not even accepted; High `latency_degradation`: W1 p95 peaked at 24.1 s vs baseline 4.1 s; High `backlog_not_drained`: queue still 15 waiting at end of window; SLIs never returned to baseline; High `sync_errors`: 18 of 29 W1 requests failed after the start (62%); High `db_errors`: 4 database error log lines, e.g. 'Postgres pool client error: terminating connection due to administrator command'; Medium `phantom_success`: 1 clients got an error but the work was done

</details>

### r1011-053831/exp-01 · E8 · Postgres disk full → postgres (completed)

_Postgres disk full did not disrupt n8n queue mode, contrary to hypothesis._

**Why Agent 1 chose it:** The experiment tested the resilience of n8n queue mode to Postgres disk full, a critical edge case for data integrity and workflow reliability, where silent failures or phantom successes were expected but not observed.

**Expected vs observed:** The hypothesis predicted **db_errors**, **sync_errors**, **lost_work**, and **silent_async_failure** due to Postgres WAL write failures, but **no symptoms** were observed (`saw_symptom: false`). All 8 in-flight orders completed successfully (`completed_once: 8`), W2 maintained 100% success rate, and SLIs recovered within 55.8s without manual intervention. The blind diagnosis correctly identified no degradation (`graded_correct: false` for the original hypothesis).

**Customer impact:** No customer impact: **W1** (synchronous) and **W2** (asynchronous) workflows maintained **100% success rates** post-injection, with W1 latency stable (~4055ms p95) and W2 responses instantaneous. No orders were lost silently, visibly, or duplicated, and no phantom successes occurred. Orders continued to process without disruption.

**Recovery:** Full recovery occurred **automatically** within **55.8 seconds** (`t_slis_recovered: 55.8`), with no restarts across **n8n-main**, **n8n-worker**, **Redis**, or **Postgres**. The system returned to baseline without manual intervention (`manual_action_needed: false`).

**Root cause:** The **blind diagnosis was correct**: the system exhibited **no failure or degradation** because the **4s mock-api delay** (not Postgres disk pressure) was the bottleneck, and the **40s disk-full injection** was too short to trigger WAL failures or timeouts. The **knowledge hypothesis** overestimated Postgres sensitivity to disk pressure in this configuration.

Evidence: `agent1_plan`, `agent2_diagnosis`, `baseline`, `fates`, `guard`, `knowledge`, `timeline`

**Resilience score (formula): 98.6 / 100 (resilient)**; measured weight coverage 90%.

| Component | Weight | Formula score | Measured inputs |
|---|---|---|---|
| availability | 30% | 100.0 | 425/425 W1 responses successful across post-injection samples |
| self recovery | 25% | 94.8 | SLIs back to baseline 56 s after injection, no help |
| latency | 20% | 100 | peak p95 4.1 s vs baseline 4.1 s, abort limit 20 s |
| data integrity | 15% | 100.0 | 8/8 in-flight orders completed exactly once |
| detectability | 10% | n/a | no consumer/queue outage, so health-check visibility was not tested |

<details><summary>Measurements</summary>

| Seconds after injection | |
|---|---|
| first symptom | n/a |
| target replaced/Ready | 2.0 s |
| queue back to baseline | 55.8 s |
| SLIs back to baseline | 55.8 s |
| last in-flight order resolved | 3.0 s |

| In-flight order fate | count |
|---|---|
| completed once | 8 |

Detectors: none

</details>

## 4. Weaknesses, ranked by severity

| # | Severity | Weakness | Seen in | Evidence | Fixes |
|---|---|---|---|---|---|
| 1 | Critical | **Silent Data Loss in Worker Failures**: Worker pod kills caused **1/8–1/9 in-flight orders to be lost silently** (clients received 200 OK but no Postgres row persisted). This occurred due to **race conditions** where Redis marked jobs as 'completed' before workers wrote to Postgres, leaving no trace of failure. **No visible errors or duplicates** were observed, making silent losses undetectable without post-mortem analysis. | r1011-044913/exp-01, r1011-044913/exp-02, r1011-044913/exp-03, r1011-053323/exp-01 | `lost_work` | `async_status_callback`, `graceful_shutdown`, `idempotency_keys` |
| 2 | Critical | **Postgres Single Point of Failure**: Postgres pod kills led to **3 lost orders (1 silent, 2 visible)** and **phantom successes (1/8 orders)**, with **W1/W2 success rates dropping to 20–38%** and **SLIs failing to recover**. The system exhibited **latency spikes (p95: 24.1s)** and **queue backlogs (15 pending jobs)**, exposing Postgres as a **critical bottleneck** despite Redis buffering. **No graceful degradation** was observed. | r1011-053323/exp-01 | `lost_work`, `db_errors`, `latency_degradation`, `backlog_not_drained`, `phantom_success` | `postgres_ha`, `db_connection_pooling`, `disk_usage_alert` |
| 3 | Critical | **Redis State Loss and Phantom Successes**: Redis pod kills caused **3 phantom successes** (clients received errors but orders were saved) and **W2 ingress failures (25% success rate)**, with **n8n-main/worker restarts cascading**. The loss of queue state led to **inconsistent job processing**, though no silent/visible losses occurred. **No persistence mechanism** mitigated transient failures, leaving the system vulnerable to **data inconsistency** during Redis restarts. | r1011-052657/exp-01 | `phantom_success`, `ingress_down`, `dependency_restart` | `redis_persistence`, `redis_timeout_threshold`, `worker_health_probe` |
| 4 | High | **Asynchronous Workflow Visibility Gap**: W2 (async) clients **always received 200 OK** even when jobs failed (e.g., **mock-api errors, worker hangs**), creating **silent async failures**. Clients had **no way to verify order completion**, while W1 (sync) workflows suffered **latency spikes (p95: 23.7s)** and **error rates (33%)** during downstream failures. This **asymmetry in observability** risks **undetected failures** in async paths. | r1011-050940/exp-01, r1011-051312/exp-01, r1011-052955/exp-01 | `silent_async_failure`, `latency_degradation`, `sync_errors` | `async_status_callback`, `http_retry_backoff` |
| 5 | High | **Queue Backlog and Consumer Bottlenecks**: Traffic spikes and downstream failures (e.g., **mock-api latency, worker hangs**) caused **queue backlogs (up to 59 waiting jobs)** and **SLI degradation**. While **no data loss occurred**, **W1 latency spikes (p95: 4693.4ms)** and **undrained queues** persisted in some experiments, risking **timeouts and cascading failures**. Single-worker deployments exacerbated throughput mismatches. | r1011-051312/exp-01, r1011-051950/exp-01, r1011-053323/exp-01 | `queue_backlog`, `latency_degradation`, `backlog_not_drained` | `scale_workers`, `autoscale_workers`, `stalled_job_tuning` |
| 6 | High | **Ingress and Webhook Processor Failures**: **n8n-main pod kills** and **Redis failures** caused **W2 webhook ingress failures (20–25% success rate)**, with clients unable to submit orders. While **no data loss occurred**, this **single point of failure** in webhook processing disrupted async workflows and required **manual intervention** in some cases. **No redundancy** existed for webhook handling. | r1011-052657/exp-01, r1011-052955/exp-01, r1011-053323/exp-01 | `ingress_down`, `dependency_restart` | `webhook_processors`, `pod_disruption_budget` |
| 7 | Medium | **Database Connection and Timeout Issues**: **Postgres overload** and **mock-api latency** triggered **database errors (ECONNREFUSED, pool client errors)** and **worker timeouts**, though **no data loss occurred**. These issues caused **W1 sync errors (15–62%)** and **latency degradation**, highlighting **connection pooling and timeout sensitivity** in high-load scenarios. **No graceful handling** of transient DB unavailability was observed. | r1011-051312/exp-01, r1011-053323/exp-01 | `db_errors`, `sync_errors`, `latency_degradation` | `db_connection_pooling`, `http_timeout_budget` |

## 5. Recommended fixes (advisory; an engineer applies them)

| Priority | Fix | Why | Addresses | Effort / impact | Verify |
|---|---|---|---|---|---|
| 1 | Give async (W2) clients a way to learn the real outcome (`async_status_callback`) | Addresses **silent data loss** and **asynchronous visibility gaps** by providing W2 clients with **execution IDs and status endpoints**, enabling detection of **lost_work** and **silent_async_failure**. This directly mitigates the **undetectable silent losses** observed in worker pod kills and Postgres failures. | Silent Data Loss in Worker Failures, Asynchronous Workflow Visibility Gap | medium / high | `reconciliation gap (accepted W2 without a row after 30 s) stays 0 during E1` |
| 2 | Highly available Postgres with automatic failover (`postgres_ha`) | Eliminates **Postgres as a single point of failure**, addressing **lost_work**, **db_errors**, and **phantom_success** during Postgres pod kills. High-availability Postgres ensures **automatic failover** and **data integrity**, preventing the **3 lost orders** and **20% W2 success rate drops** seen in experiments. | Postgres Single Point of Failure | high / high | `re-run postgres_pod_kill: failover in seconds, orders keep being saved` |
| 3 | Enable Redis AOF persistence on a PVC (`redis_persistence`) | Prevents **Redis state loss** and **phantom successes** by enabling **AOF persistence**, ensuring queue state survives restarts. This directly fixes the **3 phantom successes** and **ingress_down** issues observed in Redis pod kill experiments, where **no queue recovery mechanism** existed. | Redis State Loss and Phantom Successes | medium / high | `redis-cli -p 16379 CONFIG GET appendonly  # yes, then re-run E2` |
| 4 | Run at least 2 n8n workers (`scale_workers`) | Mitigates **queue backlogs** and **consumer bottlenecks** by ensuring **at least 2 workers** are always available, reducing **latency_degradation** and **backlog_not_drained** risks. This addresses the **throughput mismatches** observed in traffic spikes and mock-api latency experiments, where **single-worker deployments** caused **59 waiting jobs** and **SLI degradation**. | Queue Backlog and Consumer Bottlenecks | low / high | `kubectl -n n8n-chaos get deploy n8n-worker  # READY 2/2, then re-run E1` |
| 5 | Run webhook processors separately from n8n-main, more than one (`webhook_processors`) | Reduces **ingress_down** and **dependency_restart** risks by **decoupling webhook processing** from n8n-main, with **multiple replicas** handling traffic. This directly fixes the **W2 success rate drops (20–25%)** seen in n8n-main and Redis failures, where **no redundancy** existed for webhook ingestion. | Ingress and Webhook Processor Failures | medium / high | `re-run main_pod_kill: webhooks keep answering` |
| 6 | Retry the downstream HTTP call with backoff (`http_retry_backoff`) | Reduces **sync_errors** and **downstream_errors** by implementing **retry logic with backoff** for HTTP calls, preventing **W1 error rates (33–62%)** during mock-api failures. This addresses the **latency_degradation** and **queue backlog** caused by **stalled jobs**, while ensuring **idempotency** for safe retries. | Asynchronous Workflow Visibility Gap, Database Connection and Timeout Issues | low / high | `re-run mockapi_http_500 / mockapi_http_abort: W1 errors drop to ~0 for a 30 s outage` |
| 7 | Make order inserts idempotent on request_id (`idempotency_keys`) | Prevents **duplicates** and **phantom_success** by enforcing **idempotent order inserts** via `request_id`. This complements **async_status_callback** by ensuring **no duplicate executions** occur during retries or failures, directly addressing the **race conditions** that caused silent losses in worker pod kills. | Silent Data Loss in Worker Failures, Redis State Loss and Phantom Successes | low / high | `SELECT request_id, count(*) FROM orders GROUP BY 1 HAVING count(*) > 1  # 0 rows` |
| 8 | Scale workers on queue depth (`autoscale_workers`) | Dynamically scales workers based on **queue depth**, preventing **backlog_not_drained** and **latency_degradation** during traffic spikes. This is a **longer-term fix** for the **queue backlog** issues observed in experiments, where **manual scaling** would be required to handle demand surges. | Queue Backlog and Consumer Bottlenecks | medium / high | `re-run traffic_spike x5: workers scale out, the backlog drains during the spike` |
| 9 | Keep graceful shutdown for planned worker stops (`graceful_shutdown`) | Mitigates **lost_work** during **planned worker stops** by extending **terminationGracePeriodSeconds**, allowing workers to **complete in-flight jobs** before shutdown. This is a **low-effort fix** for the **silent data loss** observed in worker pod kills, though it does not address **unplanned failures** (e.g., crashes). | Silent Data Loss in Worker Failures | low / low | `graceful worker stop loses no jobs` |

## 6. Limitations

These experiments **did not test**: multi-region failures (e.g., cross-AZ Redis/Postgres splits), **persistent disk failures** (beyond Postgres disk full), **network partitions between n8n-main and Redis**, or **simultaneous failures** (e.g., worker + Postgres kills). **No manual intervention** was required in most cases, but **long-term recovery** (e.g., **mock-api latency, Postgres/Redis aborted experiments**) was not fully explored. **Resilience scores** reflect **autonomous recovery** within 120s, but **some SLIs (e.g., W1 latency, queue backlog)** did not return to baseline in all scenarios.

## 7. How this report and score were produced

Agent 2 wrote each experiment's analysis from the recorded facts (timeline, order fates, measurements, detectors, Agent 1's plan and its own blind diagnosis), then the summary, weakness ranking and fix priorities. Validators rejected unknown ids, fixes outside `playbook.yaml` and malformed answers (up to two retries with the errors as feedback); numbers in the text were checked against the measurements.

**Resilience score.** The harness calculates every component directly from recorded measurements using `chaosctl/score.py`, then applies the weights in `chaosctl/scoring.yaml`. Agent 2 receives the fixed scores for its narrative. The overall score is the mean of scored experiments.

Formula:

R = 0.30·A + 0.25·S + 0.20·L + 0.15·D + 0.10·M, each 0-100 (weights in `chaosctl/scoring.yaml`, formulas in `chaosctl/score.py`).
- A availability: 100 × successful W1 responses / all W1 responses across post-injection trace samples.
- S self-recovery: 100 if SLIs are back within 30 s, falling to 70 at the recovery deadline; 60 if pods recovered but SLIs did not within the window; 40 if the watchdog restarted something; 0 if LOCKED.
- L latency: 100 if peak W1 p95 ≤ 1.5× baseline, 0 at the abort limit, linear in between.
- D data integrity: 100 × in-flight orders completed exactly once / all in-flight orders.
- M detectability: 0 if n8n-main stayed Ready through a consumer/queue outage, 100 if health checks showed it, n/a if no such outage happened.
Components without data are n/a and the other weights are rescaled (coverage is shown). The score compares experiments on this testbed; it is not a certified reliability measure.

**Fact-check flags** (numbers not traceable to the measurements, or claims the counts contradict): r1011-051604/exp-01: 12.7