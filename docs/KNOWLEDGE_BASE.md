# Fault knowledge base, findings report and resilience score

## Why
Every fault used to be described in several places in the code (Agent 1's hypotheses, Agent 2's fallback root
causes and grading words, the CLI's `E1`/`E2` names). Now each fault has **two files and nothing else**:

| File | Owner | Says |
|---|---|---|
| `chaosctl/catalog.yaml` → `faults.<name>` | fault catalog | **how** to inject it: template, tier, limits, enabled |
| `chaosctl/knowledge/<name>.yaml` | knowledge base | **what it means**: hypothesis, expected symptoms, blind signature, grading words, related fixes |

Agent 1, Agent 2, the score and the report read faults only through `chaosctl/fault_kb.py`. Adding or removing
a fault is a data change. A catalog fault without a knowledge file still works with generic defaults; the report
marks it "no knowledge file", and `chaosctl kb` lists the gap.

## Adding a fault (checklist)
1. Add it to `catalog.yaml` (how to inject).
2. Add `chaosctl/knowledge/<name>.yaml` (file name = fault name), format below.
3. Run `uv run chaosctl kb`. It must say "knowledge base consistent".
4. Run `uv run pytest`.

Removing a fault: delete both entries. Nothing else references it.

Tests and the catalog: unit tests use a frozen copy (`tests/fixtures/catalog.yaml`), so editing the live catalog
never breaks them. One test, `test_live_catalog_is_safe`, runs the safety checks (namespace, `mode: one`, enabled
rungs only) over every fault in the live catalog: if it fails, the new entry is unsafe, not the test.

## Knowledge file format
```yaml
fault: worker_pod_kill          # must equal the catalog key and the file name
alias: E1                       # optional short name: `chaosctl run --exp E1`; must be unique
title: Worker pod kill          # shown in the report and `chaosctl kb`
category: compute_loss          # free grouping: compute_loss, dependency_loss, load, network, ...
summary: Delete one n8n-worker pod with no graceful shutdown while executions are running.
                                # shown to Agent 1 ("what each fault does") and in the report
hypothesis: >-                  # default prediction (Agent 1's fallback plan uses it)
  in-flight W1 requests fail or hang while W2 keeps returning 200 ...
symptoms:                       # detector ids (list: chaosctl/agents/diagnosis.py DETECTOR_IDS)
  expected: [consumers_down, latency_degradation]   # report: "expected seen / expected but not seen"
  possible: [lost_work, duplicates]                 # anything outside expected+possible is "not anticipated"
signature:                      # optional; blind symptom pattern for Agent 2's deterministic fallback
  any_of: [consumers_down]      # matches if one of these fired ...
  all_of: []                    # ... and all of these fired
  root_cause: "The queue consumer (worker) stopped: ..."
  priority: 10                  # higher wins when several signatures match
grading_keywords: [worker, consumer]   # after-the-fact grading: is one of these in Agent 2's root cause?
related_fixes: [scale_workers, graceful_shutdown]   # playbook.yaml ids
```
Blindness: the signature is matched against the detectors that fired, across **all** knowledge files. Agent 2
is never told which fault was injected; grading happens only afterwards.

Current detector ids: `consumers_down`, `queue_unreachable`, `queue_state_lost`, `latency_degradation`,
`silent_async_failure`, `backlog_not_drained`, `lost_work`, `duplicates`, `phantom_success`,
`dependency_restart`, `detectability_gap`. A new fault whose symptoms none of these catch needs a new detector
in `diagnosis.py` (add its id to `DETECTOR_IDS`) and, ideally, a playbook fix that `addresses` it.

Note for faults that are not Chaos Mesh pod faults (e.g. a load spike): the runner currently injects only through
Chaos Mesh and assumes a target pod. Such faults need an executor in the runner; the knowledge base, score and
report already handle them.

## Findings report (AI-written)
`uv run chaosctl report` (newest run), `--run <id>` (repeatable) or `--all`. Writes `reports/<run>/findings.md`
(one run) or `reports/findings.md` (several), plus a `.json` twin.

**Agent 2 writes it** (`chaosctl/agents/reporter.py`, Mistral), in two steps:
1. Per experiment: headline, why Agent 1 chose it, expected vs observed, customer impact, recovery, root cause
   (and whether the blind diagnosis was right), cited evidence ids, and a 0-100 score per component with a reason.
2. Across experiments: executive summary, environment, weaknesses ranked by severity, fix priorities, overall
   score, limitations.

The facts it gets are what the run recorded: timeline, order fates, trace measurements, detectors, Agent 1's plan,
the guard verdict, the blind diagnosis and the knowledge entry (plus the key outcomes as plain sentences).
Same pattern as the other agents, the AI writes and judges and code checks:
- **Validators** (hard; up to 2 retries with the errors as feedback): evidence ids must exist, fixes must be in
  `playbook.yaml`, all five components scored 0-100 or null, weaknesses ordered by severity, overall score between
  the lowest and highest experiment score.
- **Fact-check** (soft; retried, then flagged in the report): every number in the prose must be traceable to the
  measurements (ms→s and count→% conversions allowed), and no claim may contradict a zero fate count (e.g. "lost
  silently" when 0 were).
- **Arithmetic** stays in code: an experiment's total is the weighted mean of the AI's component scores.

If Mistral is unreachable or the answers stay invalid, a deterministic draft is written instead, labelled at the
top as **"Deterministic draft, not AI-written"** (README rule: the LLM can never hang the demo). `--no-llm` forces
that draft. Measured on a synthetic E1 run: 3/3 runs AI-written, ~50-60 s each, scores 54-56, no fact-check flags.

## Resilience ranking
Every report (AI-written or draft) has a **"Resilience ranking (least resilient first)"** table in section 1, also in
`findings.json` under `ranking`: experiments sorted by score (unscored ones listed last, without a rank), each with its
weakest component, plus the five components ranked weakest first by their mean over all experiments. The dashboard's
"Resilience score and ranking" panel shows the newest `findings.json`. No fix needs to be applied: the ranking compares
faults against the same, unfixed system (before/after a fix is the on-hold Agent 3 idea).

## Resilience score
Agent 2 scores each component using this rubric (also given to it verbatim, see `RUBRIC` in reporter.py):

| | Component (weight) | What it judges |
|---|---|---|
| A | availability (30 %) | share of W1 requests that succeeded after injection |
| S | self-recovery (25 %) | recovered without help, and how fast (~30 s excellent; watchdog restart poor; LOCKED 0) |
| L | latency (20 %) | how far peak W1 p95 rose above baseline relative to the abort limit |
| D | data integrity (15 %) | share of in-flight orders completed exactly once |
| M | detectability (10 %) | did health checks show the problem (green during an outage = 0) |

Total = weighted mean of the non-null components (weights in `chaosctl/scoring.yaml`). Bands: ≥ 85 resilient,
≥ 70 degraded but recovered, ≥ 50 fragile, below failing.

`chaosctl/score.py` computes the same components with fixed formulas. It is used for the deterministic draft and
as a cross-check: findings.json always records it, and the report shows a note when it differs from Agent 2's
score by more than 20 points. Formulas: A = successful/all W1 responses; S = 100 within 30 s → 70 at the deadline,
60 if SLIs never recovered, 40 watchdog restart, 0 LOCKED; L = 100 at ≤ 1.5× baseline p95 → 0 at the abort limit;
D = completed once / in-flight; M = 0 if `detectability_gap`, 100 if a consumer/queue outage was visible, else n/a.
The score compares experiments on this testbed; it is not a certified reliability measure.
