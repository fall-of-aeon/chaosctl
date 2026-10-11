# Demo: one experiment in 3 minutes (six acts)

For the live agents (Agent 1 button, Agent 2 always on) and the full shooting plan, see `docs/DEMO_DASHBOARD.md`.

Record from the dashboard's **Story** tab. No cluster needed: replay a finished run.

**Before recording:** `uv run chaosctl dashboard --replay r1011-021151` (E2, Redis pod kill), open
http://127.0.0.1:8787, switch on **Presentation**, choose light or dark for the screen. The run opens fully played;
press **Play** at 10x to show it unfold, or drag the scrubber. For the auto-stop moment use
`?run=r1010-155845/exp-01`; for before/after open the **Compare** tab.

| Act | Time | On screen | Say (the narrator line is the script) |
|---|---|---|---|
| 0 Start | 20 s | Map + four baseline numbers | "n8n in queue mode: webhook, Redis queue, workers, Postgres. Healthy: 100 % of sync orders succeed, p95 about 6 s, no backlog." |
| 1 Agent 1 injects | 35 s | Decision card, hover the cited ids (they light up on the map), safety limits, guard verdict | "Agent 1 picked the Redis pod kill from the dependency graph, explained why, and predicted lost jobs. The guard set the stop limits and only allowed this tier-3 fault after a tier-1 pass. Note the badge: this plan came from the rule-based fallback, and the page says so." |
| 2 Impact | 50 s | Play the scrubber; charts, swimlane, timeline, fate bar | "Redis was back in 1.4 s, latency nearly doubled, all 13 in-flight orders completed exactly once; SLIs recovered after 129.8 s. Prediction: refuted, no jobs were lost." Optional: switch to r1010-155845 to show the red AUTO-STOP banner and line. |
| 3 Agent 2 diagnoses | 35 s | Blind badge, reasoning stepper, click an evidence chip (opens the log) | "Agent 2 was not told what we broke. It read the evidence and named a Redis pod restart; graded correct offline." |
| 4 Fix summary | 25 s | Fix card | "What broke, why, the fix from the vetted playbook, how to verify and roll back. Advisory only." |
| 5 Report + stretch | 15 s | Report act or tab, stretch-goal cards, Compare tab | "The report scores and ranks experiments. Re-run after a fix: E1 with one worker never recovered; with two it recovered in 78.8 s." |

If no report exists yet, the Report act shows the `chaosctl report --all` command instead of a score: run it before
recording if you want the gauge on screen.
