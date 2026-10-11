# Progress log

## Gates
| # | Status |
|---|---|
| G1 n8n license | CLOSED: organizers confirmed n8n is fine. List Sustainable Use License in README + team form. |
| G2 K8s go/no-go | OPEN: clock starts at T1a. Team chose the full Kind plan despite 8 GB host RAM (see G3). |
| G3 Resources | Host: macOS 15.7.1, Apple Silicon (arm64), **8 GB RAM total**; Docker Desktop 28.1.1 with 8 CPUs and **3.8 GB memory** at start. Below the 8-12 GB the plan assumes. Decision (2026-10-10): proceed with full Kind plan; raise Docker Desktop memory; trim monitoring (no Alertmanager, short retention). Watch for OOM kills; Compose fallback remains the escape hatch. |
| G5 n8n version | OPEN (T1b). |
| G7 Hosted LLM OK? | OPEN: ask organizers whether a hosted LLM (now Mistral API) is acceptable vs "open-source model". |
| G8 LLM quota | Gemini quota exhausted on 2026-10-10; switched to Mistral (see below). Check limits at console.mistral.ai. |

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

## Final runs today (2026-10-10, host CPU fixed, 2 workers)
| Run | Experiment | Outcome | Recovery | Fates (in-flight) | Agent 2 (blind) |
|---|---|---|---|---|---|
| r1010-155337 | E1 worker kill, 1 worker | backlog never drained | none in 180 s | 10 completed once | CORRECT; fix: 2 workers |
| r1010-155845 | E1 + strict queue abort 20 | **auto-stopped** (`queue_waiting 22 > 20`) | watchdog OK, no manual action | 6 ok, 1 lost silently, 1 lost visibly | - |
| r1010-160741 | E1, 1 worker | **auto-stopped** (`w1_p95 24 s > 20 s` after 90 s) -> E2 stays locked | - | 7 ok, 1 lost visibly | - |
| r1010-163348 | E1 after fix (2 workers) | **passed** | SLIs back in **78.8 s** (pod ready 34 s) | 7 ok, 10 phantom success, 11 lost visibly | CORRECT; found duplicate-key errors on n8n's execution dedup key in Postgres logs; fixes: graceful shutdown, idempotent inserts, async status |
| r1010-163829 | E2 Redis kill (unlocked by the pass) | **auto-stopped** (`w1_p95 25.8 s > 20 s`) | Redis back in 1.2 s, but n8n-main restarted 1x, workers 2x; ~60 s total outage | 5 ok, 14 phantom success, 10 lost visibly | CORRECT; fixes: Redis AOF + PVC, raise QUEUE_BULL_REDIS_TIMEOUT_THRESHOLD, idempotent inserts |

Key findings: (1) a single worker has no headroom: a kill leaves a permanent backlog; (2) W2 async clients keep getting 200 OK while nothing is processed (silent failure, n8n-main stays Ready); (3) a 1 s Redis restart cascades into ~60 s of total outage because n8n processes exit; (4) killed in-flight work is retried, but clients get errors although the order was written (phantom success).
Note: Agent 2 sees ordinary Kubernetes events (e.g. pod Killing), like an on-call engineer would; Chaos Mesh objects/events are stripped.

## Later the same day
- Workflows simplified: W3 heartbeat and the W1/W2 "validate" step removed (unused); re-imported, W1/W2 smoke-tested OK.
- Dashboard: step bar, system map (target + blast radius), Agent 1 menu; `chaosctl demo` (one experiment + Agent 2).
- r1010-214152 (`chaosctl demo --queue-abort 20`, Agent 1 picked E1, 2 workers): passed, no visible symptom, SLIs back
  in 33.2 s; 8 in flight: 7 completed once, 1 W2 order lost silently. Agent 2: correct, fix graceful shutdown.
- Audit: the first diagnosis (r1010-155337) predated the blindness fix (its reasoning cited Chaos Mesh events). Re-run
  blind: still correct (top fix: 2 workers), but from the deterministic fallback because Gemini returned
  429 RESOURCE_EXHAUSTED (quota) and the fallback model 504. Blind diagnoses: 4 of 4 correct.
- README rewritten as onboarding guide and audited against the code; DEMO.md and JUDGES_GUIDE.md updated
  (no Grafana, no `chaosctl report` yet). Repo moved to github.com/fall-of-aeon/chaosctl.

## LLM switched from Gemini to Mistral (2026-10-10)
- Gemini free-tier quota exhausted (429 / 504). `llm.py` now calls the Mistral chat completions API (`httpx`, strict
  `json_schema` response format, temperature 0.1, `random_seed` 7); `google-genai` removed. Cache, retries, fallback
  model, budget and deterministic fallbacks unchanged.
- The key's plan allows 0 req/min on mistral-small/medium/magistral, so the models are `ministral-14b-latest`
  (30 req/min) with fallback `ministral-8b-latest` (188 req/min).
- Agent 2's prompt now lists the allowed evidence ids: the first Mistral diagnosis cited free text and was rejected by
  the validator.
- Live check (offline graph + synthetic E1/E2 evidence): structured output on both models; Agent 1 plan from the LLM
  (3.5 s); Agent 2 blind diagnosis from the LLM, root cause correct for E1 (11 s) and E2 (10 s); RAG chat and dashboard
  `/api/chat` answer with citations. 59 unit tests pass. Not yet re-run against the live cluster.

## Fault knowledge base, findings report, resilience score (2026-10-10, branch fault-knowledge-base)
- `chaosctl/knowledge/<fault>.yaml` + `fault_kb.py`: one file per catalog fault with hypothesis, expected symptoms,
  blind signature, grading keywords, related fixes. Replaces the hard-coded per-fault dicts in injection.py
  (hypotheses), diagnosis.py (fallback root causes, grading keywords) and cli.py (E1/E2 names); Agent 1's prompt now
  includes each fault's summary. Outputs for E1/E2 unchanged. `chaosctl kb` lists faults and checks consistency.
- `chaosctl report` (report.py) and `score.py`/`scoring.yaml`: findings.md/.json from existing run files; score
  formulas in docs/KNOWLEDGE_BASE.md. 74 unit tests pass (15 new, on synthetic runs shaped like r1010-163348).
  Not yet run on real cluster evidence (no reports/ on this machine).
- Findings report is now **AI-written** by Agent 2 (agents/reporter.py): per-experiment analysis and component
  scores, then summary, weakness ranking and fix priorities; validators + number/claim fact-check with up to 2
  retries; deterministic draft (labelled) only if Mistral fails. First live attempts showed the small model gets
  arithmetic and fate names wrong (called visible losses "silent", sum of weights off), hence: totals computed in
  code from the AI's component scores, plain-sentence key facts, and a zero-count claims check. Then 3/3 live runs
  AI-written (~50-60 s, scores 54-56, no flags). Formula score kept as cross-check. 80 unit tests pass.
- Ranked resilience score: report section "Resilience ranking (least resilient first)" (experiments by score, weakest
  component each; components ranked by mean) in both AI and draft reports and in findings.json; dashboard panel
  "Resilience score and ranking" (overall score + verdict, ranked bars, hover details) reading the newest findings.json,
  and an 8th step "Report & score". Checked visually in light and dark; live AI run on two synthetic runs ranked the
  Redis kill (13) below the worker kill (54). 84 unit tests pass.
- Resilience score is now **AI-judged** (team decision): Agent 2 scores each component from the evidence with the
  formula as a reference it may depart from (with a reason), calibrated on its earlier real judgements
  (state/score_memory.jsonl, in-context calibration, not training), 3 independent judgements (temperature 0.5),
  median per component; total computed in code. Live on two synthetic runs: worker kill 49.0 (reference 48.7),
  Redis kill 37.8 (reference 42.8, self-recovery 40/40/60). 87 unit tests pass.
## Failure catalog finalized (2026-10-11)
- `catalog.yaml`: 15 enabled faults over PodChaos (pod-kill, pod-failure), NetworkChaos (delay, partition) and HTTPChaos
  (replace code, abort, delay) on all 5 injectable nodes, tiers 1-3, each with a ladder (gentlest first).
  StressChaos (CPU, memory) and TimeChaos are present but disabled (`known_issue`, see FACTS).
- `chaosmesh.py`: any ladder/`params` key is a template placeholder (values checked against a safe pattern, may not
  override name/namespace/selector); `peer` resolves to a second catalog selector (partition); `_check` pins the
  network target to `n8n-chaos`, requires every non-pod-kill fault to be reversible, caps CPU workers, memory size
  and clock offset.
- Agent 1: hypotheses for every fault; prompt lists kind/reversible and asks for at most 6 experiments with node
  diversity; the deterministic plan lets nodes take turns at each fault's gentlest rung; `--exp` filters candidates
  before planning. Runner: SLIs of a reversible fault are not "recovered" before its duration ends.
- Live: all 26 CRs pass a server dry run; `scripts/chaos_smoke.py` passed for HTTP 500/abort/delay, redis/postgres
  delay and worker-redis partition (effect measured from a worker pod, and restored after delete). StressChaos left
  both workers in deleted cgroups and TimeChaos crashed one worker once; workers restarted (`rollout restart`).
- 103 unit tests pass. Not yet run as full experiments under load; the Mistral path was not exercised (local `.env`
  has no `MISTRAL_API_KEY` yet).

## Dashboard rebuild, step 0: what exists and decisions (2026-10-11)
- Found: `chaosctl/dashboard.py` (stdlib `ThreadingHTTPServer`, one `/api/state` blob polled every 1 s, plus `/api/chat`)
  and `dashboard.html` (vanilla JS, hand-drawn SVG). Real: loadgen SQLite rates, Redis LLEN/DBSIZE, pod readiness.
  Hardcoded in the page: map node positions and edges (`POS`/`FLOWS`, order-flow path only), step captions, the
  "Mistral" label. Stale: SPOF/replicas came from the static `Graph.build([])` (overlay only, replicas 1) while pod
  counts came from the live API, hence "2 pods" next to "SPOF". Restarts were lifetime counts.
- Candidate "duplicates" are not duplicates: `graph.candidates()` emits one row per ladder rung (e.g. `redis_latency`
  l100ms / l500ms / l2000ms); the table hid the rung. Fix is in the UI (variant column), `graph.py` is unchanged.
- LLM: the code calls the **Mistral** API (`llm.py`, commit "Switch LLM from Gemini to Mistral"), not Gemini as
  BUILD_PLAN says; `.env` has only GEMINI_* keys, so every current call is `no_api_key` -> deterministic fallback.
  Older runs (e.g. r1010-214152) recorded Gemini models. The dashboard now shows whatever the journal recorded.
- `graph.json` is stale (worker replicas 1; the cluster runs 2). The dashboard rebuilds the graph from the live
  cluster every 30 s with the same `Graph.build(live_manifests())` as `chaosctl graph`, falling back to `graph.json`.
- W3 (heartbeat) was removed from `overlay.yaml` earlier; the workflow strip shows the workflows the graph has (W1, W2).
- Decisions: keep the stack (stdlib server, vanilla JS + SVG, **no new dependencies**). Layout uses fixed zones from
  node `kind` (external, app, queue/db, mock, obs) plus optional hints in `chaosctl/dash/layout.yaml` (dashboard-only
  file; `overlay.yaml` is untouched so Agent 1's prompt does not change). One collector thread samples at 1 Hz and
  all browsers share it (SSE + polling fallback). Rates are trailing 5 s windows sampled every 1 s (1 s buckets at
  2 req/s are too bursty to read). The chat panel is not part of the page structure in the brief, so it is removed
  from the page; `POST /api/chat` stays for the CLI/README.

## All 6 organizer failure types + no silent fallbacks (2026-10-11)
- **No silent degradation** (`chaosctl/alerts.py`): every LLM fallback (no key/model, quota, timeout, invalid
  answer, `--no-llm`), incomplete evidence, safety-thread error, chaos clean-up problem and unconfirmed injection is
  printed as a red panel when it happens, journaled as `degraded`, appended to `state/degraded.jsonl` (dashboard red
  banner), and repeated in a summary at exit. Sources are labelled `llm:api:<model>` / `llm:cache:<model>` /
  `fallback:<reason>`; a fallback diagnosis is graded with "does not count as an Agent 2 result". `--require-llm` /
  `CHAOSCTL_REQUIRE_LLM=1` turns any LLM fallback into exit code 2. Previously `run`/`demo` never printed the plan source.
- **New faults**: `worker_container_kill`, `mockapi_container_kill` (PodChaos container-kill), `packet_loss`
  (NetworkChaos loss on redis/postgres/mock-api), `postgres_overload` (pgbench Job: CPU or 85 held connections),
  `traffic_spike` (load generator 2 -> 6/10 req/s), `postgres_disk_full` (ballast Job on a real 512 MiB loop disk,
  `scripts/pg_small_disk.sh`; IOChaos does not inject on this Kind setup). New drivers: Kubernetes Jobs (allow-listed
  image/PVC, no privileges) and LoadSpike.
- **Every injection is verified** (`ChaosMesh.verify_injected`: Chaos Mesh AllInjected / Job pod running / the
  ballast Job's own "disk now ..." line); unconfirmed -> `inject_failed`. Runs whose SLIs never return -> `unrecovered`.
- **Smoke test** (`scripts/chaos_smoke.py`): 11/11 enabled fault types PASS with a measured effect and full restore.
- **End-to-end runs** (`chaosctl run --exp <fault>`, load + safety controller + fates). All plans and diagnoses were
  the deterministic fallback (local `.env` has no `MISTRAL_MODEL`/`MISTRAL_API_KEY`), and every one said so loudly.

| Run | Fault (rung) | Result | Recovery | In-flight fates |
|---|---|---|---|---|
| r1011-013023 | worker_container_kill | first run exposed a runner bug: recovery of a same-pod restart was never detected but the run said "completed" (history corrected to unrecovered) | - | 5 ok, 4 lost visibly |
| r1011-015806 | worker_container_kill (re-run after fix) | completed | 33.6 s | 5 ok, **4 W2 lost silently** |
| r1011-013500 | mockapi_http_500 (500 for 30 s) | completed; W1 failed for ~30 s, W2 kept answering 200 | 69.1 s | 2 ok, **3 lost silently**, 4 lost visibly |
| r1011-013938 | packet_loss 10 % on postgres / mock-api / redis | completed x3; p95 up to 7.3 s, below the 1.5x symptom threshold | 46-48 s | all completed once |
| r1011-014710 | traffic_spike (x3, 6 req/s for 60 s) | completed; queue to 153 waiting, active capped at 20, drained after the spike, no errors | 137.5 s | 20/20 completed once |
| r1011-015135 | postgres_overload (cpu4) | completed; p95 5.5 s max, absorbed | 45.6 s | all completed once |
| r1011-020233 | postgres_disk_full (nearly_full, verified "99% used, 4184 KB free") | completed; n8n unaffected with 4 MiB free (the `full` rung gives `No space left on device` in the smoke test) | 55.1 s | all completed once |

- Also fixed from these runs: `t_queue_drained` was recorded at t=0 for reversible faults (before the backlog built).
- Not done: Agent 2 on the new faults with the real LLM (needs the Mistral key); the harsh rungs (`full`, `hold85`,
  `x5`, `loss30`) only ran in the smoke test, not as full experiments (`--exp` uses the gentlest rung).

### Real LLM check (Mistral key added, 2026-10-11)
- `scripts/llm_smoke.py`: ministral-14b-latest answers structured JSON in ~1 s. `chaosctl --require-llm plan`: Agent 1 plan
  from the LLM (`llm:api:ministral-14b-latest`), 6 experiments over 5 nodes, all validated and approved by the guard.
- Running Agent 2 with the real LLM exposed **two silent harness bugs**, both fixed:
  1. **Contaminated evidence**: bundles took each pod's last 200 log lines regardless of time, so postgres's
     `No space left on device` lines from the disk-full smoke test were in every later run's evidence and the LLM blamed
     "Postgres disk full" for the HTTP 500, spike and container-kill runs. Logs are now limited to the experiment window
     (kubelet timestamps, `since_seconds`), plus the previous container's lines when a container restarted.
     `scripts/refresh_evidence_logs.py` re-collected the window for the 8 runs (backups kept).
  2. **Lenient grader**: substring matching graded "Postgres disk full ... Worker restarts" as a correct worker diagnosis
     and "remained" as naming n8n-main. Now word-boundary patterns on the root cause only, and the first component named
     (or the fault's nature, e.g. "traffic burst") must be the target. New verdict `NO_IMPACT` when there was no SLI
     symptom AND every in-flight order ended normally (silent loss counts as impact).
- Also: more evidence ids allowed (`ev:restarts_delta`, ...); when no High/Critical detector fired the LLM may answer
  "no degradation" with no fix instead of inventing a playbook id (3 of 8 first answers failed validation; `--require-llm`
  stopped those runs as intended).
- **Agent 2 accuracy on the new faults (real LLM, clean evidence): 1 of 4 runs with impact correct**
  (worker_container_kill CORRECT; mockapi_http_500, packet_loss on redis, traffic_spike MISSED); 4 runs NO_IMPACT.
  Likely causes: the evidence has no offered-load (requests/s) signal, so a traffic spike looks like a slow dependency;
  n8n does not log the mock's HTTP 500s at the levels we keep, so the 500 run has no log evidence.

## Dashboard rebuild: result (2026-10-11)
- New package `chaosctl/dash/` (sources live/replay, hub collector, pure `model.py`, stdlib server, static page). Old
  `dashboard.py/.html` removed. No new dependencies. `chaosctl dashboard [--port] [--replay <run-id>]`.
- Live acceptance runs: E1 r1011-020611 (recovery 71.1 s, 9 completed once + 1 lost visibly) and E2 r1011-021151
  (recovery 129.8 s, 13 completed once). The API's recovery time and fate counts equal the evidence JSON for both.
- E1 on the map: fault marker on n8n-worker with the real pod (Chaos Mesh record), blast radius n8n-main only, worker
  Degraded at T+1.6 s (1 of 2 Ready), W2 "silent", W1 client errors at T+60 (client timeout). The harness's own
  `t_first_symptom` is T+26.5 (it ignores a missing replica while another is Ready); the dashboard shows both.
- E2: DBSIZE collapse 41 -> 0 shown on the redis node, blast radius n8n-main + n8n-worker; redis never sampled Down
  (replacement Ready in 1.4 s).
- Disconnect test: chip shows Disconnected within ~1 s of killing the API, values greyed, recovers on restart.
- A 0 s grace pod kill deletes the pod in < 1 s, so it is never sampled as Terminating; the detail panel lists it as
  "Killed (deleted)" from the Chaos Mesh record instead.
- Coordination: two other sessions shared this checkout and cluster; experiments were serialized with them. The runner
  leaves `state/active_run.json` behind when an experiment is skipped before inject (not fixed: runner is out of scope).
- Degraded banner (alerts.py records + dashboard blind spots) added at the catalog session's request.
- Not done: loadgen-off-within-10 s check was not timed separately (the off state renders "Load generator off" with "-"
  values, seen in the after-recovery screenshots); W3 is not shown because the graph no longer has it.

## Catalog merged with the knowledge base (2026-10-11, branch error-catalog)
- Merged origin/main (PR #1 knowledge base + AI report, PR #2 AI resilience score) into the catalog work. Hypotheses
  moved from code into `chaosctl/knowledge/<fault>.yaml`; the strict grader now takes each fault's words from its
  knowledge file.
- **18 new knowledge files** (15 enabled faults + 3 disabled), aliases E3-E11; `chaosctl kb`: "knowledge base consistent"
  (22 catalog faults, 22 knowledge files).
- **5 new detectors**: `sync_errors`, `ingress_down`, `queue_backlog`, `db_errors`, `downstream_errors`; signatures for
  the new faults (ingress down > database > downstream errors > queue backlog > sync errors > latency only).
- **7 new playbook fixes** (17 total): http_retry_backoff, http_timeout_budget, db_connection_pooling,
  disk_usage_alert, autoscale_workers, webhook_processors, postgres_ha.
- Agent 2 with the real LLM on the 8 saved runs: **2 of 4 runs with impact correct** (was 1 of 4): mockapi_http_500 now
  CORRECT, worker_container_kill CORRECT; traffic_spike ("workers were overloaded") and packet_loss on redis MISSED;
  4 runs NO_IMPACT.
- The old dashboard's resilience-score panel (from PR #1/#2) has to be ported to chaosctl/dash; its test is skipped
  with that reason until then.
- Follow-up after the merge of origin/main (2026-10-11): ported main's "Resilience score and ranking" panel into
  `chaosctl/dash` (`GET /api/resilience`, newest `reports/**/findings.json`; replay reads only the replayed run's
  report) and its 8th phase step "Report and score" (done when the run is in the newest report). The brief asked for 7
  steps; main's merged PR added the 8th, so main wins. `test_dashboard_reads_newest_report` moved to
  `tests/test_dashboard.py`. The panel shows "No report yet" until `chaosctl report` runs.
- E1/E2 evidence re-collected with `scripts/refresh_evidence_logs.py` (a stale postgres "No space left on device" line
  from another session's smoke test had leaked in; originals backed up outside the repo) and re-diagnosed: both blind
  diagnoses now come from the LLM (Mistral `ministral-14b-latest`, live API) and grade CORRECT. The page passes the
  bundle to `grade()` so NO_IMPACT runs are labelled as such. The E1 after-recovery screenshots predate this refresh.


## End-to-end acceptance test, run 1: E1/E2 only (2026-10-11, branch e2e-results)
- Run per `docs/E2E_TEST_PROMPT.md` on a Linux host (Arch, 16 cores, 14 GiB RAM, Docker 29.9, kind 0.33, Helm 4.3),
  cluster deleted and rebuilt from scratch (cluster-up 7 min 9 s, up 1 min 11 s, bootstrap 64 s). Results with every
  number: `docs/E2E_RESULTS.md` (**37 PASS, 5 FAIL, 1 GAP**); reports and screenshots in `docs/results/`.
- Baseline (2 workers, 2 req/s): W1 105/105, p50 4.05 s, p95 4.06 s; W2 p95 24 ms; queue 0.
- Safety: context refusal, namespace protection, kill switch 9/9 (one cli row showed 10.4 s, a test-harness measurement
  artifact: the CR was deleted in 0.66 s), auto-stop (`queue_waiting 4 > 3`, r1011-024619; E2 tripped p95 by itself),
  watchdog never needed manual action, no LOCKED.
- E1 r1011-025233 (2 workers): completed, queue 0, SLIs 70.0 s, 8/8 completed once. E2 r1011-025735 (`--no-llm`):
  auto-stopped on p95 23.4 s; Redis back in 1.0 s but n8n-main 1× and workers 2× restarted; 3 phantom successes.
  Agent 2 (Mistral) correct on both. AI report: overall 55/100, Redis kill 35, worker kill 88.
- Failures: scripted auto-stop demo (`--queue-abort 20`) never trips on this host; E1 symptoms with 2 workers do not
  match the knowledge file; `run --exp E2` with the LLM silently runs nothing (Agent 1 is not told the requested
  fault); the AI report had 7 factual errors. Gap: no E3/E4 in the catalog yet.

## End-to-end acceptance test, run 2: 11-fault catalog (2026-10-11, branch e2e-results)
- Cluster rebuilt from scratch on the Linux host (cluster-up 7 min 50 s). `chaosctl up` failed on Linux:
  `scripts/pg_small_disk.sh` needs `/dev/loopN` inside the Kind node, which exists on Docker Desktop but not on a Linux
  host; worked around with `mknod` in the node. Results with every number: `docs/E2E_RESULTS.md` (**39 PASS, 4 FAIL**).
- Safety: kill switch 9/9 (CR removed ≤ 1.6 s in every row), `demo --queue-abort 20` auto-stopped on its own
  (mockapi_http_500, `queue_waiting 21 > 20` at 19.8 s), watchdog never needed manual action.
- 7 weaker faults disabled for the demo (user request); 11 remain over all 6 failure types. `chaosctl run
  --max-experiments N` added (with `--exp`, Agent 1 planned worker_pod_kill 3 times; 15 min for one fault).
- All 11 faults run end to end (04:49-05:42). Agent 2 (live Mistral) 5/7 correct on runs with impact (missed
  mockapi_latency, traffic_spike). AI report (needed `CHAOSCTL_LLM_BUDGET=90`; 30 gives the deterministic draft):
  overall 83.4; Postgres kill 31.2, Redis kill 37.2, HTTP 500 65.1, latency 75.6, the rest 93-100.
- New findings: HTTPChaos on mock-api also makes its readiness probe time out (mock-api leaves the Service, so
  "+2 s latency" fails 100 % of W1); every worker kill lost exactly 1 W2 order silently (3/3); a Postgres restart told
  one W1 client "OK" for an order that was never saved.

## Dashboard redesign: story, live ops, compare, report (UI.md brief, 2026-10-11, branch dashboard-story)
- Plan and design decisions: docs/UI_PLAN.md. Files: chaosctl/dash/{runs.py, static/js/*}, fix_summary.py, the
  `fix-summary` command and the step at the end of `diagnose`, tests/test_dash_runs.py. Agents, runner, safety,
  catalog, policy and playbook untouched.
- Finished runs replay in the browser from reports/ + loadgen.sqlite (scrubber, 1x/4x/10x); no cluster needed.
  Verified on r1011-021151 (E2), r1010-155845 (auto-stop) and Compare r1010-155337 vs r1010-163348.
  Screenshots: docs/screenshots/ (1920x1080, 1366x768, 1280x720, light theme, presentation mode, mid-replay).
- Deviations: charts are our own SVG, not a vendored uPlot/Chart.js (vendoring needs a download; offline already
  works). The interface now uses readable text labels and numbered investigation steps without emoji.
- Peak p95 now comes from the client-side records (loadgen), not the safety trace, which stops at an abort:
  r1010-155845 peaks at 26.0 s (5.5x), r1010-155337 at 29.8 s (6.3x, as in README 4.2).
- LLM settings check: the dashboard prints which MISTRAL_* settings it sees at start. This process sees
  MISTRAL_MODEL=ministral-14b-latest, so Agent 1's "MISTRAL_MODEL not set" in r1011-* came from runs started before
  .env had the model, not from a code path.
- Open: no findings.json exists yet, so the Report tab and Act 5 show the `chaosctl report --all` empty state;
  combined-failure stretch goal is Planned (no run with two faults). Live ops not re-checked against a live cluster
  from this laptop (cluster work happens on the other machine).

## Agents in the dashboard: Agent 1 button with approval, Agent 2 always on (2026-10-11, branch e2e-results)
- New: chaosctl/dash/control.py (plan / approve / kill / traffic), chaosctl/dash/watcher.py (always-on Agent 2),
  chaosctl/agents/investigator.py (blind tool-using investigation), hidden `chaosctl execute` (runs one approved plan),
  campaign `preplanned` path, js/agents.js, tests/test_agents_live.py. The page's only writes are these POSTs.
- Agent 1 self-correction: a plan that fails validation is sent back to Mistral once with the errors; if it is still
  wrong, only the invalid experiments are dropped. Seen live: Mistral cited `edge:n8n-main->mock-api` (not on the map),
  which used to discard the whole LLM plan; after the retry the card showed Mistral's pick.
- Verified live on the Linux host (dashboard on :8788): Agent 1 picked mockapi_http_500 (Mistral), approved in the UI,
  run r1011-062027 completed (recovery 53.8 s, 8/8 in-flight orders completed once). Agent 2 was not told: it
  triggered itself 3 s after mock-api went unhealthy (inc-1011-062133), made 6 tool calls, concluded "mock-api
  unreachable" with http_retry_backoff / autoscale_workers / worker_health_probe; graded CORRECT. The evidence
  diagnosis it then ran on its own (Mistral) also graded CORRECT and the Story filled in. Kill switch and reset from
  the UI checked (no chaos to delete; all Ready in 0.06 s).

## Agent 2 faster, charts readable (2026-10-11)
- Live investigation (`agents/investigator.py`): the 4 "look around" tools every investigation started with (pods,
  client SLIs, queue, events) plus the Postgres silent-loss check now run as a **parallel triage with no LLM call**
  (0.01 s instead of ~20 s of LLM turns); Mistral then gets at most 3 targeted follow-ups; tool choice and the written
  diagnosis run on `ministral-8b-latest` (2-3× faster than 14b on our prompts; the graded evidence diagnosis after a
  run keeps `MISTRAL_MODEL`; `CHAOSCTL_AGENT2_STEP_MODEL` / `CHAOSCTL_AGENT2_CONCLUDE_MODEL` override).
- Same live mockapi_http_500 incident, old vs new code side by side: **47.9 s → 25.9 s**, both root causes correct
  (new one names the readiness-probe failures). Healthy system on an operator check: **35-50 s → 0.01 s**, and it no
  longer invents an incident (old and 8b/14b LLMs named a "transient worker failure" with 0 detectors firing): a
  rule-based "no incident" answer when every triage check is normal. That shortcut is never used when the monitor saw a
  service Down/Degraded (a live test caught it answering "healthy" while mock-api's probe failed but its pod was Ready).
- Charts: redraw at their real width (they were drawn at a 600 px fallback while hidden, so half the card was empty);
  event labels in their own lane above the plot instead of over the data; W1 errors as a filled area; a one-line
  takeaway under each chart title (slowest orders and × baseline, failed orders and how long W1 completed nothing
  before an auto-stop, possible silent failure, backlog peak and drain time).
