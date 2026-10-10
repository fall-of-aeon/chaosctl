# Demo runbook (judges, <= 3 min on video, about 6 min live)

**Before (off camera, about 10 min):**
1. Cluster and app are up (README Part 3).
2. Run `rm -f KILL && uv run chaosctl recover --ack` (clean state).
3. Run `uv run chaosctl baseline --duration 120`.
4. In terminal 1, run `uv run chaosctl dashboard` and open http://127.0.0.1:8787.
5. Log in to the n8n UI (http://127.0.0.1:15678) to show the two workflows.
6. Check the Mistral rate limits and usage (README 3.5). If calls fail, the AI panels show the deterministic fallback.

| # | Time | Show | Command (terminal 2) | What to say |
|---|---|---|---|---|
| 1 | 25 s | System map on the dashboard + `chaosctl graph` | `uv run chaosctl graph` | Discovered from Kubernetes + declared overlay. An order flows client → n8n-main → Redis → worker → mock-api → Postgres. Redis and Postgres are single points of failure; blast radius = everything that depends on them. |
| 2 | 15 s | Step bar "Baseline" | (part of the demo run) | 2 orders/s: W1 100 % success, p95 about 5 s, queue flat. |
| 3 | 20 s | Agent 1 panel (menu, ✅ pick, why, prediction, safety limits) | `uv run chaosctl demo --queue-abort 20` | Agent 1 scores every allowed experiment and picks one, citing the map. The guard sets limits and only allows Redis after the worker test passed. |
| 4 | 50 s | Charts + service tiles + markers | (running) | One pod killed, once, while orders are in flight. Latency up, queue piling up, async customers still told OK. The safety controller stops it if a limit is crossed. |
| 5 | 20 s | Fate table + timeline | (running) | What happened to every order in flight; time to recover. |
| 6 | 30 s | Agent 2 panel + terminal reasoning | (runs at the end of `demo`) | Not told what we broke; reasons step by step; root cause; fixes from the vetted playbook, advisory only. |
| 7 | 15 s | "Ask Agent 2" chat | click a suggested question | Answers grounded in our recorded evidence, with sources. |
| 8 | 10 s | Kill switch | `reports/killswitch-test.json`, or `touch KILL` during a run | Three ways (CLI, file, Ctrl-C), each tested three times. |

Recorded results to fall back on: `reports/<run-id>/` (journal, results, evidence, diagnosis); the dashboard shows the newest run.
Findings report / resilience score (`chaosctl report`) is Day-2 work and does not exist yet.
