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
`dependency_restart`, `detectability_gap`, and (2026-10-11, for the new faults) `sync_errors` (≥ 10 % of W1 requests
failed), `ingress_down` (W2 webhooks refused too), `queue_backlog` (waiting jobs grew while workers were busy),
`db_errors` and `downstream_errors` (from the experiment-window logs).

Grading is strict: whole words in the root cause only, and the first component it names (or one of the fault's
`grading_keywords`, if that comes first) must be the injected target. So `grading_keywords` should describe the
fault's nature ("traffic", "packet loss", "disk full"), not generic words like "latency" that fit every fault. A new fault whose symptoms none of these catch needs a new detector
in `diagnosis.py` (add its id to `DETECTOR_IDS`) and, ideally, a playbook fix that `addresses` it.

Faults that are not Chaos Mesh faults have executors too: `driver: job` (a Kubernetes Job from an allow-listed image,
e.g. pgbench or the disk ballast) and `driver: loadgen` (the traffic spike). See README 2.6.

## Findings report (AI-written)
`uv run chaosctl report` (newest run), `--run <id>` (repeatable) or `--all`. Writes `reports/<run>/findings.md`
(one run) or `reports/findings.md` (several), plus a `.json` twin.

**Agent 2 writes it** (`chaosctl/agents/reporter.py`, Mistral), in two steps:
1. Per experiment: headline, why Agent 1 chose it, expected vs observed, customer impact, recovery, root cause
   (and whether the blind diagnosis was right), cited evidence ids, and the formula-calculated component scores with measured inputs.
2. Across experiments: executive summary, environment, weaknesses ranked by severity, fix priorities, overall
   score, limitations.

The facts it gets are what the run recorded: timeline, order fates, trace measurements, detectors, Agent 1's plan,
the guard verdict, the blind diagnosis and the knowledge entry (plus the key outcomes as plain sentences).
The AI writes the narrative, the harness calculates the scores, and code checks the output:
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

## Resilience score (calculated from recorded measurements)

Every resilience report and dashboard uses the deterministic component values from `chaosctl/score.py`.
Agent 2 receives those fixed scores when writing its findings and recommendations. Report generation no
longer makes model scoring calls or writes new score-calibration memory.

```text
R = 0.30*A + 0.25*S + 0.20*L + 0.15*D + 0.10*M
```

| Component | Weight | Formula and measured inputs |
|---|---|---|
| A: availability | 30% | 100 × successful W1 responses / all W1 responses across post-injection trace samples |
| S: self-recovery | 25% | 100 for recovery within 30 s, decreasing linearly to 70 at the deadline; 60 when SLIs remain degraded, 40 for watchdog intervention, 0 for failed recovery |
| L: latency | 20% | 100 at peak p95 ≤ 1.5 × baseline; 0 at the abort limit; linear interpolation between them |
| D: data integrity | 15% | 100 × completed-once orders / all recorded in-flight orders |
| M: detectability | 10% | 0 for a detectability gap; 100 when health checks reflect a consumer/queue outage; unavailable otherwise |

Each component ranges from 0 to 100. Unavailable components are excluded, rather than counted as zero:
`R = sum(weight × component) / sum(available weights)`. For example, if M is unavailable, the weighted
sum of A, S, L and D is divided by 0.90. The report shows measured weight coverage and the explanation
for each component. The overall score is the arithmetic mean of scored experiments; skipped experiments
are unscored. Bands: ≥85 resilient, ≥70 degraded but recovered, ≥50 fragile, below 50 failing.

JSON includes `score_method: formula`, `score_formula`, `score_weights`, and each experiment's `score`.
AI-written and deterministic-draft reports use identical scores for the same recorded measurements.
Older reports are normalized on dashboard reads using their saved formula values or recorded run evidence.
The historical AI scores remain in archived reports as historical results, not the current scoring method.

Availability uses recorded trace samples, which can include overlapping rolling windows. This is the
existing formula's sampled measurement, not a deduplicated count of all client requests. The score compares
experiments on this testbed; show order fates alongside it because aggregate scores can hide integrity loss.
