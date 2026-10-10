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
