# Final end-to-end test: prompt for an AI coding agent

Use this on the machine that will host the demo, **after** the fault catalog (E3/E4 + their
`chaosctl/knowledge/*.yaml` files) and the `fault-knowledge-base` branch are merged. Open a Claude Code session inside the
`chaosctl` repo and paste everything in the block below. It runs every test on the real system, records real data in
`docs/E2E_RESULTS.md`, and stops at anything risky. Expect about 2-3 hours (setup ~25 min, ~6 min per experiment,
kill-switch test ~10 min).

```text
You are running the final end-to-end acceptance test of chaosctl (AI-driven chaos engineering for n8n on Kind),
the system we will demo at the hackathon. Goal: run every test case on the REAL system, collect REAL data, and
produce an honest results document. Read README.md, docs/KNOWLEDGE_BASE.md, docs/JUDGES_GUIDE.md and
docs/PROGRESS.md first, then follow the phases below in order.

## Ground rules (never break these)
- Real data only. Never write, edit or "fix" files in reports/ or state/ by hand, and never invent numbers. If a
  measurement looks wrong, investigate and record it as a finding.
- Safety first. Only the Kind context `kind-chaos` and namespace `n8n-chaos`. If a safety test fails (kill switch,
  auto-stop, namespace protection, context refusal), STOP injecting faults and report to me.
- Do not edit chaosctl/catalog.yaml or chaosctl/knowledge/*.yaml; teammates own them. If an entry is wrong or can't
  run, record it as a gap.
- Code fixes are allowed only in harness code, each with a unit test, and only after explaining the root cause to me.
  Re-run the failed test afterwards and record both attempts.
- Secrets: MISTRAL_API_KEY lives only in .env. Never print it, log it or commit it.
- Ask me before anything outside the cluster: installing tools, sudo, deleting files outside the repo, pushing to git.
- Git: work on a new branch `e2e-results`. Commits are authored by me, with no AI co-author or "generated with"
  lines. Never force-push. Ask before pushing.

## How to record every test
Create docs/E2E_RESULTS.md with one row per test case:
| ID | Area | Command | Expected | Observed (real numbers) | Evidence (run id / file) | Result: PASS / FAIL / GAP |
Fill it in as you go, not at the end. Results: PASS = met the criterion; FAIL = didn't (say why); GAP = the
feature doesn't exist or can't run (say what's missing). Note the time and duration of each phase.

## Phase 0: Preflight (no cluster yet)
T0.1 Tools: docker (≥ 6 GB available to containers), kind, kubectl, helm, uv. Record versions.
T0.2 inotify limits ≥ 512 instances / 524288 watches; ports 15678 16379 15432 18080 8787 free.
T0.3 `uv sync` then `uv run pytest`: all pass (record the count).
T0.4 `uv run chaosctl kb`: lists every catalog fault with its alias; "knowledge base consistent", or record each gap.
T0.5 `PYTHONPATH=. uv run python scripts/llm_smoke.py`: Mistral answers with valid JSON (model ministral-14b-latest).

## Phase 1: Clean build from scratch (proves the README works)
T1.1 `kind delete cluster --name chaos` (only this cluster), then `uv run chaosctl cluster-up`: all pods Running;
     Chaos Mesh ready; namespace n8n-chaos annotated. Record the duration.
T1.2 `uv run chaosctl up` + `uv run chaosctl bootstrap`: postgres, redis, mock-api, n8n main + worker Ready; W1 returns 200
     and writes an order row; W2 returns 200 and its row appears later.
T1.3 Apply the README's post-setup steps exactly (worker count, Grafana/Prometheus scaled to 0). Record the final
     worker count; every later result depends on it.
T1.4 `uv run chaosctl graph`: all 5 services, edges via ConfigMap env + overlay; redis and postgres are SPOFs.

## Phase 2: Baseline
T2.1 `uv run chaosctl baseline --duration 120`: state/baseline.json shows W1 success ≥ 99 %, W1 p95 recorded,
     queue wait trend about 0. Record W1 p50/p95, W2 p95, queue mean/max.

## Phase 3: Safety (must all pass before any experiment)
T3.1 Kubectl context refusal: with another context active, a chaosctl command refuses to run. Restore kind-chaos.
T3.2 Namespace protection: a fault aimed outside n8n-chaos selects nothing (see docs/FACTS.md).
T3.3 `uv run chaosctl killswitch-test`: 3 ways × 3 repeats; the chaos resource is removed ≤ 3 s every time and the worker
     is Ready ≤ 60 s. Record 9 rows from reports/killswitch-test.json. Afterwards
     `kubectl -n n8n-chaos rollout restart deploy/n8n-worker` if CrashLoopBackOff appears (known behaviour).
T3.4 Auto-stop: `uv run chaosctl demo --queue-abort 20`: the controller aborts on its own, removes the fault, and the
     watchdog confirms every service is Ready with no manual action.
T3.5 LOCKED state: if any run ends LOCKED, `uv run chaosctl recover --ack` clears it; record whether it happened.

## Phase 4: Agent 1 (AI decides)
T4.1 `uv run chaosctl plan --dry-run`: "Plan source: llm"; every reason cites real graph ids; the guard orders tier 1
     before tier 3 and gates tier 3 on a passed tier 1; every enabled catalog fault appears as a candidate.
T4.2 `uv run chaosctl plan --dry-run --no-llm`: the deterministic fallback plan is valid.

## Phase 5: Every fault, end to end (the real data)
For EVERY enabled fault listed by `chaosctl kb` (E1, E2, and the new E3/E4/...), in tier order, with a cooldown
of at least 60 s between runs:
T5.x.1 `uv run chaosctl run --exp <alias>`: status completed or aborted (both are valid results; skipped is a FAIL
       unless the reason is explained). Record run id, status, abort reason, recovery time, timeline, restarts.
T5.x.2 reports/<run>/ contains journal.jsonl, results.json, evidence/exp-01.json and evidence/exp-01.plan.json.
T5.x.3 Fate table: count of each fate for the in-flight orders. The total equals the in-flight count at injection.
T5.x.4 Visible impact matches the fault (e.g. queue grows for a worker kill, Redis unreachable for a Redis kill).
       Compare with the knowledge file's expected symptoms.
T5.x.5 The cluster is healthy again before the next fault (all pods Ready, no managed chaos resources left).
If a fault cannot run (e.g. a load spike needs a non-Chaos-Mesh executor in runner.py), mark it GAP and explain.
Do not hack around it.

## Phase 6: Agent 2 (blind diagnosis)
T6.x For each run: `uv run chaosctl diagnose --run <id> --exp exp-01`: source llm (not fallback), all fixes from
     playbook.yaml, and "Grading (after the fact)" says CORRECT. Record the root cause text and fixes.
T6.y Blindness: the diagnosis input contains no Chaos Mesh event or log line (check evidence vs. what Agent 2 cites).

## Phase 7: AI-written findings report and ranked score
T7.1 `uv run chaosctl report --all`: reports/findings.md is AI-written ("Written by Agent 2", not "Deterministic
     draft"); every experiment present; "Resilience ranking (least resilient first)" lists every scored experiment.
T7.2 findings.json: the "unverified_numbers" fact-check flags are empty, or list each flag and check it by hand
     against the evidence.
T7.3 Cross-check notes (Agent 2 vs formula differing by more than 20 points): list each one and judge which is
     more reasonable from the evidence.
T7.4 Read the report against the evidence: no claim contradicts results.json (fate names, recovery times, counts).
     Record every factual error found.
T7.5 `uv run chaosctl report --all --no-llm`: the labelled deterministic draft is generated.

## Phase 8: Dashboard and chat
T8.1 `uv run chaosctl dashboard` → http://127.0.0.1:8787, during one live `chaosctl demo` run: the step bar
     advances through all 8 steps; the map shows the target and blast radius; charts move; the fate table, Agent 2
     panel and "Resilience score and ranking" panel fill in. Take screenshots (light and dark) into docs/results/.
T8.2 "Ask Agent 2" chat: 3 questions (e.g. "Why were orders lost when Redis restarted?"); answers come from
     Mistral and cite sources that exist.

## Phase 9: Demo rehearsal
T9.1 Run the README/JUDGES_GUIDE demo sequence exactly as it will be shown, timed. Record the total time and anything
     slow or confusing. The video must fit in 3 minutes, so note which parts need cutting or pre-recording.

## Phase 10: Wrap-up
- Copy the final reports/findings.md, findings.json and the screenshots into docs/results/ (reports/ is gitignored).
- Update README Part 4 (results) and docs/PROGRESS.md with the real numbers and run ids.
- Summarise docs/E2E_RESULTS.md: totals of PASS/FAIL/GAP, the list of FAILs and GAPs with the next action for each,
  and the final ranked resilience scores.
- `uv run pytest` once more. Commit on branch e2e-results (authored by me, no AI attribution) and ask before pushing.
- Finally, tell me in plain words: is the system demo-ready? If not, what exactly blocks it?
```
