# Demo runbook (judges, <= 3 min)

Pre-demo (not on camera, ~10 min before): cluster up, stack up, `chaosctl bootstrap`, `chaosctl baseline --duration 120`,
`rm -f KILL`, `chaosctl recover --ack`. Open Grafana (`kubectl -n monitoring port-forward svc/monitoring-grafana 13000:80`)
and the Chaos Mesh dashboard (`kubectl -n chaos-mesh port-forward svc/chaos-dashboard 2333:2333`).

| # | Time | Show | Command | What to say |
|---|---|---|---|---|
| 1 | 25 s | App + dependency graph | `uv run chaosctl graph` (and `graph.mmd` rendered) | Discovered from Kubernetes + declared overlay. Every node and edge has an ID. Redis and Postgres are the most critical single points of failure; blast radius = everything that depends on them. |
| 2 | 15 s | Baseline | `cat state/baseline.json` / Grafana | 2 req/s steady: W1 100 % success, p95 about 4.7 s, queue flat, about 8 executions always in flight. |
| 3 | 20 s | AI plan | `uv run chaosctl plan --dry-run` | Gemini picks and explains, citing graph IDs; a validator rejects anything off-catalog; the deterministic guard orders E1 before E2 (tier rule) and sets abort limits. If Gemini is down, a deterministic plan is used. |
| 4 | 50 s | E1 worker kill during execution | `uv run chaosctl run --exp E1` | Injection waits until requests are in flight. Show: in-flight set, W1 errors vs W2 still 200, queue climbing, pod replaced, fate table, recovery time. |
| 5 | 40 s | E2 Redis restart | `uv run chaosctl run --exp E2` | Prediction vs outcome; queue state lost; fate table (lost silently / lost visibly / phantom). |
| 6 | 10 s | Kill switch | `reports/killswitch-test.json` or live `touch KILL` during a run | Three ways (CLI, file, SIGINT), each tested three times; reversible faults restore immediately. |
| 7 | 20 s | Day 2: findings | `uv run chaosctl report` | Blind diagnosis, ranked fixes, resilience score. |

Fallback if live injection misbehaves on stage: show the recorded run in `reports/<run-id>/` (journal, results, evidence).
