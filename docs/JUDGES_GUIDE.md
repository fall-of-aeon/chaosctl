# How to present chaosctl to the judges

## The one-sentence pitch
"We break a real workflow-automation platform (n8n) on purpose, in a safe sandbox. An AI agent decides **what** to
break using a map of the system, a deterministic safety controller **stops it automatically** if it goes too far,
and a second AI agent, **without being told what we broke**, works out what went wrong and recommends fixes."

## The system under test (what is n8n?)
n8n is an open-source workflow-automation tool (like Zapier). In **queue mode** it is split into services:

| Service | Role |
|---|---|
| **n8n-main** | Receives webhooks (HTTP requests), puts a job on the queue, serves the web UI |
| **Redis** | The job queue between main and worker |
| **n8n-worker** | Takes jobs off the queue and runs the workflow steps |
| **Postgres** | Stores workflows, execution history and our `orders` table |
| **mock-api** | A fake downstream service each workflow calls (4 s delay), so executions are in progress long enough for a fault to hit mid-flight |

Data flow: `client → n8n-main → Redis → n8n-worker → mock-api → Postgres`.

## The 2 workflows
Open them in the n8n UI at http://127.0.0.1:15678 (Workflows list). Both are the same 3 steps:
**receive order (webhook) → call mock-api (4 s) → save order in Postgres**. They differ in one setting only:

| Workflow | When the customer gets an answer | Why it exists |
|---|---|---|
| **W1 sync order** | after the order is saved | The customer **waits**, so failures show up as errors and slow responses (**visible** impact). |
| **W2 async order** | immediately ("200 OK", before any work) | The customer is told "OK" before anything runs, so failures are **silent**: accepted, then lost. |

Our load generator sends 2 orders per second (half W1, half W2), each with a unique `request_id`. Afterwards we look up
every `request_id` in Postgres. That is how we know exactly which orders were **completed, lost, or duplicated**.
(A third "heartbeat" workflow was removed: nothing in the analysis used it.)

## Step-by-step demo (about 6 min live; cut to 3 for the video)

Shortcut: `uv run chaosctl demo` runs steps 3-6 in one go (Agent 1 picks one experiment, it runs once, Agent 2 diagnoses). Add `--queue-abort 20` to force the automatic stop.

Before: `uv run chaosctl dashboard` and open http://127.0.0.1:8787 next to the terminal.

**1. Map (deliverable: services, dependencies, data flows)**: `uv run chaosctl graph`
> "The harness discovers the services from Kubernetes and combines that with a short declared overlay. Every
> service and dependency has an ID. It computes the blast radius: if Redis fails, everything that depends on it is
> affected. Redis and Postgres are single points of failure with the highest criticality."

**2. Normal behaviour (deliverable: baseline error rate, latency, health)**: the first 60 s of any run
> "Steady load: 0 % errors, W1 takes about 4.7 s (p95) because the downstream call takes 4 s, the queue is flat,
> all five services are green. This is our baseline."

**3. Agent 1 decides (deliverable: AI-driven failure injection)**: `uv run chaosctl plan --dry-run`
> "Agent 1 (Gemini) gets the dependency map and the list of allowed experiments. It picks what to break, explains
> why citing the map's IDs, and predicts what will happen. A validator rejects anything not in our catalog, and a
> deterministic policy guard sets the safety limits and the order: the lower-risk worker kill must pass before
> it may touch Redis. If Gemini is down, a deterministic plan is used, so the demo never hangs."

**4. Inject (deliverable: failure injection + visible impact)**: `uv run chaosctl run --exp E1`
> "We only inject while orders are in flight; the harness records exactly which ones. Watch the dashboard: active
> jobs drop to zero, the queue climbs, W1 latency jumps 5-6× above baseline. But W2 clients still get 200 OK.
> They think everything is fine. That is a silent failure."

**5. Automatic stop (deliverable: automatic stop / rollback)**: `uv run chaosctl run --exp E1 --queue-abort 20`
> "Same fault with a strict limit. The safety controller checks once a second, in its own thread, with no AI
> involved. The moment the queue crosses 20 it stops the load, removes the fault and waits for every service to be
> healthy again. If a service didn't come back, it would restart it itself. There is also a manual kill switch
> three ways (command, file, Ctrl-C), each tested three times."

**6. Agent 2 diagnoses, blind (deliverable: findings + fixes)**: `uv run chaosctl diagnose`
> "Agent 2 only sees the evidence: metrics, Kubernetes events, logs, and what happened to each request. It is not
> told what we broke. Watch it think: what it observes, what it suspects, how it checks, what it concludes. It then
> recommends fixes, only from a vetted playbook, and does not change anything itself. At the end we grade it
> against what was actually injected."

**7. Impact in one table**: the dashboard's fate table and timeline
> "For every order that was in flight: completed once, lost after being told OK, lost with an error, done twice, or
> done while the client was told it failed. Plus time to first symptom, time until the new pod was ready."

## The injections and how each is fixed

| | E1: worker pod kill | E2: Redis restart |
|---|---|---|
| **What we do** | Chaos Mesh deletes the only n8n-worker pod (no graceful shutdown) while about 8 executions are running | Chaos Mesh deletes the Redis pod (no persistence) while jobs are queued and running |
| **Risk tier** | 1 (one consumer) | 3 (the queue every workflow depends on), only allowed after E1 passed |
| **What we saw** | No consumer for about 30 s, queue climbs to about 50, W1 p95 4.7 s → about 30 s, W2 still 200 OK. New worker Ready at +34 s. In-flight orders were re-run and completed exactly once (we had predicted duplicates: wrong). The backlog never drained: one worker has no spare capacity | See `docs/PROGRESS.md` / the run report |
| **How it recovers during the test** | Kubernetes replaces the pod automatically; our watchdog checks it is Ready within 120 s and would restart it otherwise | Same: Kubernetes recreates Redis; the n8n services reconnect |
| **Recommended fix (Agent 2, from the playbook)** | Run 2+ workers; alert on "queue growing with zero active jobs"; worker readiness that checks Redis | Redis persistence (AOF on a volume) so a restart keeps the queue; raise the Redis timeout threshold; idempotent inserts |

Important distinction to say out loud: **the safety controller stops and rolls back the experiment** (automatic,
deterministic). **Agent 2 recommends** fixes to the system's design. **An engineer applies them** (advisory only).

## Likely judge questions
- **"Is the AI in control of the safety?"** No. Safety is plain code with fixed limits; the agent's thresholds can
  only be made stricter, never looser. The AI decides and explains; the harness executes and enforces.
- **"What if the LLM returns nonsense?"** Every output is validated against the dependency map, the fault catalog,
  and the playbook; anything invalid is replaced by a deterministic fallback. The same input is cached, so a replay
  is identical.
- **"Can it break something outside the sandbox?"** Chaos Mesh only acts on the annotated `n8n-chaos` namespace (we
  tested that another namespace is ignored), every fault uses `mode: one`, and the harness refuses any kube context
  other than our local Kind cluster.
- **"How do you know a request was lost?"** Every request has a unique ID; we reconcile the client's view with the
  rows in Postgres.
- **"Why is your hypothesis sometimes wrong?"** That's the point of experimenting: we predicted duplicates after a
  worker kill; n8n re-ran the stalled jobs and completed each exactly once. The report says so.
