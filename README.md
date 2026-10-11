# chaos-n8n / `chaosctl`

**AI-driven chaos engineering for a real open-source app (n8n) running on a local Kubernetes cluster.**
Built for the Bosch Hackathon "AI-Driven Chaos Engineering for Cloud-Native Resilience".

> **New to the team? Read this file top to bottom once.** It explains what we are building and why (Part 1),
> how it works (Part 2), how to run it yourself (Part 3), what we have done so far (Part 4), and what is still
> left (Part 5). Deeper material: `docs/JUDGES_GUIDE.md` (how to present), `docs/PROGRESS.md` (detailed log of
> every step and result), `docs/FACTS.md` (verified technical facts), `docs/DEMO.md` (short demo runbook).

---

## Contents
- [Part 1: What we are building, and why](#part-1-what-we-are-building-and-why)
- [Part 2: How it works](#part-2-how-it-works)
- [Part 3: Run it yourself (step by step)](#part-3-run-it-yourself-step-by-step)
- [Part 4: What we have done so far](#part-4-what-we-have-done-so-far)
- [Part 5: What is left (Day 2)](#part-5-what-is-left-day-2)
- [Part 6: Reference](#part-6-reference)

---

# Part 1: What we are building, and why

## 1.1 The hackathon task in plain words
**Chaos engineering** means breaking parts of a system *on purpose*, in a controlled way, to find weaknesses before
real outages do. The hackathon asks for a system that:

1. Runs a real, open-source, multi-service app (we chose **n8n**).
2. **Maps** its services and how they depend on each other.
3. Lets **AI decide** what to break, when, and how badly.
4. **Injects failures safely**, with automatic safety limits and a kill switch.
5. **Measures the impact**: errors, slowdowns, recovery time.
6. Produces a **report**: weaknesses ranked by severity, with recommended fixes (and ideally a resilience score).

Everything runs in **our own local Kubernetes cluster** on one laptop. We never touch anything outside it.

## 1.2 What the judges expect

**Day 1 (shortlisting), all done:**
- App running end to end
- Services, dependencies and data flows mapped
- Normal error rate, latency and health measured (the "baseline")
- 1-2 failure injections chosen by AI (Agent 1)
- Automatic stop / rollback
- Visible impact

**Day 2 (final):**
- **At least 3 different failure types**. We have only 1 type so far (pod crash); see Part 5.
- AI-ranked experiments with reasons
- AI-written findings report with fixes
- Resilience score
- Clean run from scratch
- Demo video (3 min or less)
- One-page architecture
- Findings report
- Team form
- Submit 30 minutes early

**5 deliverables:** source repo + README; demo video; architecture overview; findings report; team form.

## 1.3 Key ideas and words you will hear

| Term | Meaning |
|---|---|
| **n8n** | Open-source workflow-automation tool (like Zapier). Our "victim" app. |
| **Queue mode** | n8n split into services: `n8n-main` takes requests and puts jobs in a **Redis** queue; `n8n-worker` takes jobs and runs them; results go to **Postgres**. |
| **Kubernetes (k8s) / Kind** | Kubernetes runs containers ("pods") and restarts them if they die. **Kind** = Kubernetes inside one Docker container on the laptop. |
| **Chaos Mesh** | Open-source tool that injects faults into Kubernetes (kill a pod, break the network, ...). We drive it from Python. |
| **Pod kill** | Delete a running pod instantly. Kubernetes then creates a replacement. |
| **SPOF** | Single point of failure: one copy only, and the app needs it. |
| **Blast radius** | Everything that breaks when one service breaks (all services that depend on it, directly or indirectly). |
| **p95 latency** | 95 % of requests are faster than this. A good "how slow is it for customers" number. |
| **Baseline** | The normal numbers before we break anything. |
| **In-flight** | Requests that are being processed at the moment we inject. We only inject while work is in flight. |
| **Fate table** | For every in-flight request: completed once / lost silently / lost with an error / duplicated / phantom success. |
| **Phantom success** | The customer got an error, but the order was actually saved. |
| **Silent failure** | The customer was told "OK", but the work never happened. |
| **Agent 1** | AI (Mistral) that chooses which failure to inject and explains why. |
| **Agent 2** | AI (Mistral) that is *not told* what was broken, works out what went wrong from evidence, and recommends fixes. |
| **Safety controller** | Plain Python code (no AI) that watches the system every second and stops the experiment if limits are crossed. |

## 1.4 Big design decisions (and why)
1. **Target app = n8n in queue mode** on Kubernetes (main + worker + Redis + Postgres + a mock downstream API). The organizers confirmed n8n's license is fine.
2. **Agents decide and explain; the harness executes and enforces.** The AI never runs commands. It returns JSON that we validate. Only our code creates or deletes failures, and only from a fixed catalog.
3. **Safety is never AI-controlled.** Limits, aborts, kill switch and recovery are deterministic code. The AI can only make limits *stricter*, never looser.
4. **The LLM can never hang the demo.** Mistral calls are cached by input, have timeouts, retries and a fallback model. If anything fails, a deterministic fallback answer is used.
5. **We measure like a customer.** Our own load generator and probes are the ground truth, not n8n's internal health checks (which stay green during some failures; that is one of our findings).
6. **Agent 2 is blind and advisory.** It never sees what was injected and it never changes the system. Engineers apply fixes.
7. **Host access via NodePorts, not `kubectl port-forward`.** A port-forward is tied to one pod and dies when that pod is killed, which would break our measurements.
8. **LLM = Mistral API** (`ministral-14b-latest`, fallback `ministral-8b-latest`), called over HTTPS with `httpx` using strict JSON-schema structured output. Orchestration with **LangGraph**. (Switched from Gemini on 2026-10-10 after its free-tier quota ran out.)

---

# Part 2: How it works

## 2.1 Architecture

```
                 ┌──────────────────────── chaosctl (Python, runs on the laptop) ─────────────────────────┐
                 │  dependency graph ─► AGENT 1 (score ► Mistral picks ► validate ► safety guard)           │
                 │       ─► wait for in-flight orders ─► inject (Chaos Mesh) ─► observe ─► recover          │
                 │       ─► fate table + evidence ─► AGENT 2 (blind diagnosis, recommends fixes)            │
                 │  safety controller (own thread, 1×/s)  ·  kill switch  ·  journal  ·  dashboard + chat   │
                 └──────────┬───────────────────────────┬───────────────────────────┬─────────────────────┘
                            │ Kubernetes API            │ Redis / Postgres probes   │ load generator
                            ▼                           ▼ (NodePorts on 127.0.0.1)   ▼ (2 orders/s)
   ┌────────────────────────────── Kind cluster "chaos" (inside Docker) ──────────────────────────────────┐
   │  ns chaos-mesh : Chaos Mesh (injects faults, only into annotated namespaces)                          │
   │  ns monitoring : Prometheus + Grafana (installed, currently scaled to 0 to save CPU)                  │
   │  ns n8n-chaos  : n8n-main ─► redis ─► n8n-worker (×2) ─► mock-api                                     │
   │                      └──────────────► postgres ◄─────────┘                                            │
   └───────────────────────────────────────────────────────────────────────────────────────────────────────┘
   Mistral API (internet): receives only graph summaries, plan metadata, evidence summaries. No secrets.
```

## 2.2 The app we break

| Service | What it does | Host port (127.0.0.1) |
|---|---|---|
| `n8n-main` | Receives webhooks (orders), enqueues jobs, serves the n8n web UI | **15678** (UI + webhooks) |
| `redis` | Job queue (Bull). **No persistence**: a restart empties it | 16379 |
| `n8n-worker` | Takes jobs from the queue and runs the workflow | none (no Service) |
| `postgres` | n8n's database + our `orders` table | 15432 (read-only `probe` user) |
| `mock-api` | Fake downstream service, waits **4 s** per call so work is "in flight" when we break things | 18080 |

**Data flow of one order:** `client ─► n8n-main ─► Redis queue ─► n8n-worker ─► mock-api (4 s) ─► Postgres (save order)`

## 2.3 The 2 workflows (in `workflows/`)
Both have the same 3 steps: **receive order (webhook) → call mock-api → save order in Postgres**.

| | W1 sync order | W2 async order |
|---|---|---|
| URL | `POST http://127.0.0.1:15678/webhook/chaos-w1` | `POST http://127.0.0.1:15678/webhook/chaos-w2` |
| Customer gets an answer... | **after** the order is saved | **immediately** ("200 OK"), before any work |
| Shows | **visible** failures (errors, slowness) | **silent** failures (told OK, then lost) |

Every request carries a unique `request_id`. The `orders` table deliberately has **no unique constraint**, so
afterwards we can count rows per id: 0 rows = lost, 1 = completed, 2+ = duplicated.
(A third "heartbeat" workflow and a "validate" step were removed as unused.)

## 2.4 Lifecycle of one experiment (what `chaosctl run` / `chaosctl demo` does)
Each experiment injects **exactly one fault, exactly once**. Nothing runs unless you start a command.

1. **Load the dependency graph** from Kubernetes plus `chaosctl/overlay.yaml`.
2. **Agent 1 plans.**
   - (a) Python scores every allowed experiment in `catalog.yaml`:
     `score = 0.4×criticality + 0.3×untested + 0.2×new-info − 0.1×risk-tier`.
   - (b) Mistral picks, with a reason that cites graph IDs and a hypothesis.
   - (c) A validator rejects anything not in the catalog or citing unknown IDs. On rejection, a deterministic plan is used.
   - (d) The **policy guard** computes the blast radius, sets abort limits (stricter only), and enforces the risk-tier
     rule: **a tier-3 experiment (Redis/Postgres/n8n-main kill) only after a tier-1 experiment has passed.**
   - At most 6 experiments per plan (`policy.yaml` `max_experiments_per_plan`); the deterministic plan lets nodes take
     turns and uses each fault's gentlest rung. `--exp <fault id>` restricts the candidates instead.
3. **Pre-flight checks.** The experiment is skipped if any one fails:
   - The right cluster (`kind-chaos`)
   - All pods Ready
   - Chaos Mesh daemon Ready
   - No leftover chaos
   - Fault and target are in the catalog
   - Redis and Postgres probes reachable
   - Baseline exists
   - Safety controller healthy
   - Guard verdict = approve
   - No `./KILL` file
   - Not LOCKED
4. **Warm-up:** 60 s of steady load (2 orders/s). Must be healthy, or the experiment is **skipped**.
5. **In-flight gate:** wait until ≥ 3 W1 requests are in flight and ≥ 1 job is active, then record exactly which requests
   are in flight. Timeout after 90 s means the experiment is skipped (we never inject blind).
6. **Inject:** create one Chaos Mesh resource (`PodChaos`, `NetworkChaos` or `HTTPChaos`, always `mode: one`) from the
   catalog template. See **2.6** for the full catalog. Reversible faults end on their own after their duration; the
   run does not count SLIs as recovered before that.
7. **Observe** (up to 175 s): every second, record latency, errors, queue and pod health. The safety controller checks its limits
   every second in its own thread:
   - error rate
   - p95 latency
   - queue length
   - total time cap
   - any service outside the blast radius going down
   - kill switch

   Error/latency limits for tier 1 only arm after 90 s (the outage is expected first). Tier 3 has no error-rate limit (an outage is by design).
8. **Recover:** delete the chaos resource. The **watchdog** waits up to 120 s for every pod to be Ready; if not, it restarts
   the workload itself and records `manual_action_needed`. If even that fails, the system goes **LOCKED** (no more
   experiments until `chaosctl recover --ack`).
9. **Fate table:** wait up to 180 s for in-flight requests, then classify each one using Postgres row counts.
10. **Evidence bundle** written to `reports/<run-id>/evidence/`. The chaos details are kept in a separate file Agent 2 never sees.
11. **History** updated (`state/history.json`). This decides "untested" scores and the tier rule next time.
12. **Agent 2** (`chaosctl diagnose`) reads the evidence:
    - deterministic detectors
    - Mistral's step-by-step reasoning (observe → hypothesize → check → conclude)
    - root cause
    - fixes from `playbook.yaml` only

    Afterwards it is graded against what was really injected.

## 2.5 Safety, in one list
- Chaos Mesh only acts on namespace **`n8n-chaos`** (annotated); we tested that other namespaces are ignored.
- Our Chaos Mesh client builds resources **only from catalog templates** and re-checks the result:
  - namespace `n8n-chaos`
  - `mode: one`
  - capped duration for reversible faults; every fault except `pod-kill` must be reversible
  - a NetworkChaos peer (`target`) also pinned to `n8n-chaos`
  - ladder values checked before they are spliced into YAML; CPU/memory/clock-offset caps
  - Jobs: allow-listed image only, run once, no host namespaces, no privileges, no service-account token, only
    allow-listed PVCs, deadline ≤ max duration, pgbench clients capped; load spike capped at 10 req/s
  - labels `chaosctl=managed`
- **Nothing degrades silently** (`chaosctl/alerts.py`). Whenever the LLM is not used (no key, quota, timeout, invalid
  answer) or the harness cannot do something (evidence collection, safety check, chaos clean-up), it prints a red
  panel at that moment, writes a `degraded` event to the journal and `state/degraded.jsonl`, shows a red banner on the
  dashboard, and repeats everything in a summary when the command exits. Plans and diagnoses are labelled
  `llm:api:<model>`, `llm:cache:<model>` (replayed answer to an identical prompt) or `fallback:<reason>`; a fallback
  diagnosis is never graded as an Agent 2 result. **`chaosctl --require-llm ...`** (or `CHAOSCTL_REQUIRE_LLM=1`)
  makes any LLM fallback stop the command with exit code 2 instead.
- The harness refuses to run if `kubectl`'s current context is not **`kind-chaos`**.
- **Kill switch, 3 ways:**
  - `chaosctl kill`
  - creating a file named `KILL` in the repo root
  - Ctrl-C / SIGTERM

  Each one stops the load, deletes all managed chaos, and runs the watchdog.
- Active run info is saved in `state/active_run.json`, so `chaosctl recover` works even if the runner crashed.
- Every decision and action is written to an append-only journal: `reports/<run-id>/journal.jsonl`.


## 2.6 Failure catalog (`chaosctl/catalog.yaml`)
Chaos Mesh provides most fault *types*; the catalog decides which n8n component each one may hit, how hard (a ladder
of rungs, gentlest first), its risk tier, and its maximum duration. Agent 1 only picks `(fault, target, rung)` from
this list; it never writes YAML. Three drivers: **Chaos Mesh** CRs, Kubernetes **Jobs** we run from an allow-listed
image (load and disk ballast), and our own **load generator** (traffic spike).

| Failure type (organizers' list) | Fault id | How | Target | Rungs | Tier |
|---|---|---|---|---|---|
| Service / container crash | `worker_pod_kill` | PodChaos pod-kill | n8n-worker | grace 0 s | 1 |
| | `worker_graceful_kill` | PodChaos pod-kill | n8n-worker | grace 30 s | 1 |
| | `worker_container_kill` | PodChaos container-kill | n8n-worker | restart in place | 1 |
| | `worker_pod_failure` | PodChaos pod-failure | n8n-worker | 30 s, 60 s | 1 |
| | `main_pod_kill`, `redis_pod_kill`, `postgres_pod_kill` | PodChaos pod-kill | SPOFs | grace 0 s | 3 |
| Slow or lossy network | `redis_latency`, `postgres_latency` | NetworkChaos delay | redis / postgres | 100 ms - 2 s for 45 s | 2 |
| | `packet_loss` | NetworkChaos loss | redis, postgres or mock-api | 10 %, 30 % for 45 s | 2 |
| | `mockapi_latency` | HTTPChaos delay | mock-api `/work` | +2 s, +10 s, +30 s (past the 30 s timeout) | 2 |
| Full disk | `postgres_disk_full` | Job: ballast file fills the **real** postgres disk (512 MiB loop disk, see below) | postgres | 4 MiB left, completely full; 40 s | 3 |
| Overloaded database | `postgres_overload` | Job: `pgbench` in the cluster | postgres | 4 or 8 CPU-heavy clients; 85 of 100 connections held; 45 s | 2 |
| Failed dependency | `mockapi_http_500` | HTTPChaos replace code | mock-api `/work` | 500 for 30 s, 503 for 60 s | 1 |
| | `mockapi_http_abort` | HTTPChaos abort | mock-api `/work` | 30 s | 1 |
| | `mockapi_container_kill` | PodChaos container-kill | mock-api | restart in place | 1 |
| | `worker_redis_partition` | NetworkChaos partition | one worker ↔ redis | 20 s, 60 s (reaches the 60 s Bull lock) | 2 |
| Sudden traffic spike | `traffic_spike` | our load generator | n8n-main (webhooks) | 2 → 6 or 10 req/s for 60 s | 2 |
| *(off)* | `worker_cpu_stress`, `worker_memory_stress` | StressChaos | n8n-worker | disabled: Kind cgroup v2 issue (3.5) | - |
| *(off)* | `worker_clock_skew` | TimeChaos | n8n-worker | disabled: flaky ptrace into Node.js (3.5) | - |
| *(test)* | `pod_failure` | PodChaos pod-failure | worker | kill-switch test only | - |

**Trimmed for the demo (11 Oct):** `worker_graceful_kill`, `worker_container_kill`, `mockapi_container_kill`, `mockapi_http_abort`, `redis_latency`, `postgres_latency` and `packet_loss` are now `enabled: false` (weaker duplicates or no measurable impact in the E2E runs); 11 faults stay enabled and still cover all 6 failure types. Set `enabled: true` to bring one back.

**Full disk without risking the laptop.** The postgres volume is a hostPath on the Kind node, i.e. on the laptop's disk
(~1 TB free), so filling it directly is not an option, and Chaos Mesh's IOChaos (fake `ENOSPC`) does not work on this
Kind setup. `scripts/pg_small_disk.sh` (run by `up.sh`) puts postgres's data on a 512 MiB loop-mounted ext4 file inside
the Kind node. The fault's Job writes a ballast file until that disk is full, holds it, and deletes it when it ends;
`delete_all` also runs a cleanup Job. Three guards: preflight refuses unless postgres's filesystem is ≤ 1 GiB, the Job
itself refuses on a disk > 1 GiB, and the Job may only mount that one PVC. After a Kind node restart, run the script
again (it remounts the existing disk image).

**Every injection is verified.** After creating a fault the runner checks it really took effect (Chaos Mesh
`AllInjected` / the Job's pod running). If not, the experiment is marked `inject_failed` (never "passed") and an alert
is raised.

Left out on purpose: DNSChaos (Chaos Mesh installed without its DNS server), JVMChaos (n8n is Node.js), IOChaos (its
injector dies on this Kind setup), Kernel/Block chaos (on a single-node Kind cluster they can take down the node),
cloud and physical-machine chaos.

## 2.7 The dashboard (`chaosctl dashboard` → http://127.0.0.1:8787)

Read-only: it has no button that injects a fault or triggers the kill switch. `chaosctl dashboard --replay <run-id>`
plays a recorded run from `reports/<run-id>/` instead (header chip "Replay"); the page code is the same in both modes.

**Top to bottom (the order of the demo):**
1. **Top bar**: run id, connection (Live / Stale / Disconnected / Replay), campaign state, kill switch (armed / triggered),
   load generator, chart window (2 / 3 / 5 min).
2. **Phase strip**: Map, Baseline, Agent 1 decides, Inject, Impact and auto-stop, Recover, Agent 2 diagnoses, driven by the
   run journal.
3. **System map** (built from the dependency graph, nothing hardcoded): zones Clients, n8n application, Queue and data,
   External dependency, plus a muted platform lane (namespace, Chaos Mesh, Prometheus, Grafana). Arrows point to what a
   component depends on. Each node shows one derived state (Healthy, Degraded, Down, Recovering; rule in
   `chaosctl/dash/model.py`), pods Ready and its own live metric. During an experiment: a fault marker on the target with
   the pod Chaos Mesh hit, T+seconds and phase, an arrow from Chaos Mesh, an amber outline on the blast radius and
   "first symptom T+N s" under each node when the harness observed it. Under the map: a workflow strip (W1 / W2 in plain
   words) and the BUILD_PLAN 11.1 timeline with the recovery time. Click a node for its detail panel.
4. **Live metrics**: five tiles and three charts on one time axis with inject / symptom / recovered / auto-stop markers
   and a band while the fault is active. "-" means no data; "Load generator off" is never shown as 0.
5. **Agent 1 decision**: chosen experiment, reason, cited graph ids, hypothesis, guard verdict, and where the decision
   came from (LLM model from the journal, cached, or rule-based fallback), plus the candidates table.
6. **Impact and result**: summary and fate table of the in-flight requests.
7. **Agent 2 blind diagnosis**: reasoning, root cause, ranked fixes with runbooks, offline grade.
8. **Event log**: Kubernetes events and journal events, merged and redacted.

**API** (all GET, one collector thread samples once per second and serves every browser): `/api/graph`, `/api/state`,
`/api/components`, `/api/metrics?window=s`, `/api/events?since=ts`, `/api/agent1`, `/api/experiments/<id>`,
`/api/stream` (Server-Sent Events; the page falls back to polling every 2 s). `POST /api/chat` (the RAG chat) is
still served for scripts but no longer shown on the page.

**Where the dashboard's data comes from:**

| On screen | Source | Stored |
|---|---|---|
| Orders, errors, latency, load on/off | our load generator records every request | `state/loadgen.sqlite` |
| Queue waiting/active, key count | Redis via NodePort, live | not stored |
| Reconciliation gap | Postgres (read-only probe user) via NodePort, live | not stored |
| mock-api delay / error rate | mock-api `GET /admin` via NodePort | not stored |
| Pods, restarts, events, Chaos Mesh resources | Kubernetes API, live | not stored |
| Map nodes and edges, SPOF, blast radius | live cluster via `Graph.build`, every 30 s (else `graph.json`) | `graph.json` |
| Phases, fault marker, Agent 1 choice | run journal | `reports/<run-id>/journal.jsonl` (+ `reports/plans/`) |
| Fate table, timeline, recovery time | written when an experiment ends | `reports/<run-id>/evidence/<exp-id>.json` |
| Agent 2 reasoning | written by `diagnose` | `reports/<run-id>/evidence/*.diagnosis.json` |

The dashboard keeps 15 minutes of 1 s points and the newest 600 events in memory.

---

# Part 3: Run it yourself (step by step)

## 3.1 Prerequisites
- **macOS or Linux, 8 GB RAM minimum.**
  - Give Docker Desktop **at least 6 GB** of memory (Settings → Resources → Memory).
  - With less, the cluster will be killed. This happened to us once at 3.8 GB.
- Docker, `kind` (we used v0.33.0), `kubectl`, `helm` (v4.3.0; v3.12+ also works), and [`uv`](https://docs.astral.sh/uv/).
  On a Mac: `brew install kind helm uv`.
- A **Mistral API key** from https://console.mistral.ai (API Keys). Optional: without it, every AI step uses its deterministic fallback.
- Internet access for image downloads. The first setup takes **~25 minutes**, mostly image pulls.

## 3.2 First-time setup

```bash
git clone https://github.com/fall-of-aeon/chaosctl.git && cd chaosctl
cp .env.example .env                 # put your MISTRAL_API_KEY in .env (never commit .env)
uv sync                              # Python environment + dependencies
uv run pytest                        # 87 unit tests, no cluster needed: all should pass
```

Optionally, check that Mistral works:
```bash
PYTHONPATH=. uv run python scripts/llm_smoke.py
```

## 3.3 Create the cluster and the app

| Step | Command | What it does | Time |
|---|---|---|---|
| 1 | `uv run chaosctl cluster-up` | Kind cluster `chaos` with NodePorts on 127.0.0.1; installs kube-prometheus-stack 92.3.0 and Chaos Mesh 2.8.4; creates + annotates namespace `n8n-chaos` | 10-15 min |
| 2 | `uv run chaosctl up` | Generates local passwords into `state/secrets.env`; loads images into Kind; deploys Postgres, Redis, mock-api and n8n 2.42.4 (Helm chart 1.15.0, queue mode) | 5-10 min |
| 3 | `uv run chaosctl bootstrap` | Imports the Postgres credential + 2 workflows into n8n, publishes them, restarts n8n, smoke-tests W1 and W2 | 2-3 min |
| 4 | `kubectl -n monitoring scale deploy/monitoring-grafana --replicas=0` and `kubectl -n monitoring patch prometheus monitoring-kube-prometheus-prometheus --type merge -p '{"spec":{"replicas":0}}'` | **Frees CPU.** Grafana + Prometheus used ~1 core and made results invalid on our laptop; the demo uses our own dashboard | seconds |
| 5 | `kubectl -n n8n-chaos scale deploy/n8n-worker --replicas=2` | Applies the fix Agent 2 recommended (see Part 4). The baseline profile deploys 1 worker | 1 min |
| 6 | Open http://127.0.0.1:15678 | n8n web UI. **First visit asks you to create an owner account** (local only; any email/password) | |

Then capture a baseline:
```bash
uv run chaosctl baseline --duration 120     # 2 min of steady load → state/baseline.json
```
Expect W1 100 % success, p95 around 5-6 s, queue at about 0.

## 3.4 Running experiments

```bash
uv run chaosctl dashboard               # terminal 1, keep running; open http://127.0.0.1:8787
uv run chaosctl graph                   # dependency map table; also writes graph.json and graph.mmd (Mermaid)
uv run chaosctl plan --dry-run          # Agent 1's plan with reasons and guard verdicts. Never injects.
uv run chaosctl demo                    # judge demo: Agent 1 picks ONE experiment, runs it once, Agent 2 diagnoses
uv run chaosctl demo --queue-abort 20   # same, with a strict queue limit to show the automatic stop
uv run chaosctl run --exp E1            # E1 = worker pod kill (add --max-experiments 1: Agent 1 may plan repeats)
uv run chaosctl run --exp E2            # E2 = Redis pod kill (only allowed after a passed E1)
uv run chaosctl run --exp redis_latency # any catalog fault id (2.6); repeatable
uv run python scripts/chaos_smoke.py    # live check of every fault driver, no load (~4 min)
uv run chaosctl --require-llm demo      # fail instead of falling back if Mistral cannot be used
uv run chaosctl diagnose                # Agent 2 on the newest run (or: --run r1010-163829)
uv run chaosctl report                  # AI-written findings report + score (newest run; --run ID, --all)
uv run chaosctl kb                      # fault knowledge base: E1/E2/... and consistency check
uv run chaosctl kill                    # KILL SWITCH (or: touch KILL, or Ctrl-C in the run)
uv run chaosctl recover --ack           # clean up after a crash; clears LOCKED
uv run chaosctl killswitch-test         # kill switch acceptance test, 3× each way (about 10 min)
```

**One experiment takes about 5-6 minutes**: 60 s warm-up, ~3 min observation, up to 3 min to resolve in-flight requests.

## 3.5 Gotchas we hit (read these)
- **`kind load docker-image` fails** with Docker Desktop's containerd image store ("content digest not found").
  `scripts/up.sh` works around it by streaming images in with `docker save --platform ... | ctr import`.
- **Image pulls from ghcr.io inside Kind are slow** (~10 min for Chaos Mesh the first time).
- **n8n 2.x** activates workflows with `n8n publish:workflow --id=...`, then needs a restart of main + worker.
  `bootstrap.sh` handles both.
- **The first request after an n8n restart can time out** (n8n-main still connecting to Postgres). Retry after a minute.
- **CPU starvation makes results invalid.** If the laptop is busy (load average far above the core count), the
  pre-flight may skip the experiment (`baseline_unstable`), or results are garbage. Keep Grafana/Prometheus at 0 and close heavy apps.
- **Repeated faults on the same pod** push it into CrashLoopBackOff (Kubernetes back-off), and recovery gets slower.
  Fix with `kubectl -n n8n-chaos rollout restart deploy/n8n-worker`.
- `chaosctl up` resets the worker count to 1 (Helm values). Re-apply step 5 afterwards.
- **StressChaos and TimeChaos are unreliable on Kind (cgroup v2)**: after one StressChaos the target container is left
  in a deleted cgroup and the next Stress/TimeChaos on that pod fails; TimeChaos ptrace into Node.js is flaky and once
  crashed a worker (SIGSEGV). These faults are in the catalog but disabled (`known_issue`). Restart the worker
  Deployment after trying them.
- `kubectl` must point at `kind-chaos` (`kubectl config use-context kind-chaos`), or the harness refuses to run.
- **Mistral rate limits** (HTTP 429) on the free "Experiment" plan. Calls are retried with back-off, then the fallback
  model is tried; if both fail, Agent 1, Agent 2 and the chat silently use their deterministic fallbacks (the dashboard
  labels them "deterministic fallback"). The "answered by" / source label tells you which one you got. Before a demo,
  check limits and usage at console.mistral.ai. Identical questions are answered from the cache (`state/llm_cache.sqlite`)
  without using quota.

## 3.6 Repository layout

```
chaosctl/                  the Python package (the "harness")
  cli.py                   all commands (typer)
  graph.py                 dependency graph: k8s discovery + overlay + blast radius, SPOF, criticality, Mermaid
  overlay.yaml             declared dependencies and workflow paths (what manifests can't show)
  catalog.yaml             the ONLY faults that may be injected (templates, tiers, enabled flags)
  knowledge/<fault>.yaml   what each fault means: hypothesis, expected symptoms, signature, fixes (docs/KNOWLEDGE_BASE.md)
  fault_kb.py              loads + checks the knowledge base; the only way agents/report/score see faults
  policy.yaml              safety limits, tier rules, warm-up, in-flight gate
  playbook.yaml            the ONLY fixes Agent 2 may recommend (with source, verify, rollback)
  agents/injection.py      Agent 1: scorer, Mistral select, validator, policy guard (LangGraph)
  agents/campaign.py       campaign graph: plan → execute → update history → loop (LangGraph)
  agents/diagnosis.py      Agent 2: blind filter, detectors, Mistral reasoning, playbook validator, grading
  chaos/chaosmesh.py       creates/deletes/pauses Chaos Mesh resources from catalog templates only
  runner.py                one experiment end to end (pre-flight, warm-up, gate, inject, observe, recover, fates)
  safety.py                state machine, 1 Hz abort monitor, kill switch, recovery watchdog, LOCKED
  loadgen.py               load generator (2 orders/s) + SQLite request log + in-flight queries
  probes.py                Redis/Postgres/Kubernetes probes + fate classification
  llm.py                   Mistral wrapper: cache, retries, fallback model, budget, secret redaction
  rag.py                   "Ask Agent 2" chat: corpus, BM25 retrieval, cited answers
  report.py                `chaosctl report`: findings.md/.json (AI-written; deterministic draft if Mistral is down)
  agents/reporter.py       Agent 2 report mode: writes + scores the report; validators and number fact-check
  score.py, scoring.yaml   reference formula + weights (and the draft's score)
  score_memory.py          Agent 2's earlier score judgements, used to calibrate new ones
  dash/                    live dashboard: sources (live/replay), hub (collector), model (pure rules), server, static page
  alerts.py                loud warnings: every fallback/harness error printed, journaled, in state/degraded.jsonl
  journal.py, kube.py      audit journal; Kubernetes client pinned to kind-chaos
scripts/                   cluster-up/down, up/down, bootstrap, kind-config, llm_smoke
k8s/                       namespace, postgres, redis (+ hardened: AOF + PVC), mock-api, n8n NodePort
helm-values/               n8n baseline, kube-prometheus-stack (trimmed), chaos-mesh (containerd)
mock_api/                  FastAPI mock downstream (delay/error rate settable via POST /admin)
workflows/                 W1 sync, W2 async (n8n JSON)
tests/                     57 pytest tests (+ recorded manifests in tests/fixtures)
docs/                      PROGRESS (log), FACTS (verified facts), JUDGES_GUIDE, DEMO
state/   (gitignored)      secrets.env, baseline.json, history.json, loadgen.sqlite, LLM cache, active run
reports/ (gitignored)      one folder per run: journal, results, evidence, diagnoses
```

---

# Part 4: What we have done so far
(Full detail with every number: `docs/PROGRESS.md`.)

## 4.1 Build log, in order

| # | Step | Result |
|---|---|---|
| 0 | **Phase 0 smoke tests** | Tools installed (kind, helm). Docker raised from 3.8 GB to 6 GB. Gemini structured output works. The Interactions API took 45 s per call, so `llm.py` uses `generate_content` (6-13 s). |
| 1 | **T1a cluster** | Kind + monitoring + Chaos Mesh up. Accepted: pod-failure auto-restored after its duration; a non-annotated namespace was protected. |
| 2 | **T1b app** | n8n queue mode, Postgres, Redis, mock-api; W1/W2 return 200 and write orders. Docker Desktop crashed once (memory) during deploy; recovered. |
| 3 | **T2 dependency graph** | Discovered from the live cluster (including hostnames hidden in a ConfigMap) + overlay. Blast radius, SPOFs and criticality match the plan. |
| 4 | **T3 fault catalog + Chaos Mesh client** | Worker kill, Redis kill, and a reversible pod-failure for kill-switch tests; refuses anything else. Later extended to 15 enabled faults over PodChaos/NetworkChaos/HTTPChaos (2.6). |
| 5 | **T4a baseline** | 3 orders/s overloaded one worker (queue kept growing), so steady load is **2 orders/s**: W1 100 %, p95 4.7 s, queue flat. |
| 6 | **T4b safety + kill switch** | Kill-switch test 9 runs: chaos removed in 1-3 s **every time**; 7/9 within the 60 s worker-Ready target. The 2 slow ones were caused by CrashLoopBackOff from 9 faults in a row on one pod. |
| 7 | **T5/T6 Agent 1 + guard** | Gemini plan cites real graph IDs; the guard puts E1 before E2 and gates E2 on a passed E1. |
| 8 | **T7 experiments** | See 4.2. |
| 9 | **Dashboard** | Live charts, markers, fate table, then step bar, system map and Agent 1 menu. |
| 10 | **Agent 2** | Blind diagnosis with visible reasoning; **4 of 4 root causes correct**. Fixes advisory only. A blindness leak (Chaos Mesh events were visible to it) was found and fixed; the one diagnosis made before the fix was re-run blind. |
| 11 | **RAG chat** | "Ask Agent 2" on the dashboard with cited answers. |
| 12 | **LLM switched to Mistral** | Gemini's free-tier quota was exhausted (429s, 504s on the fallback). `llm.py` now calls the Mistral chat completions API with strict JSON-schema output; everything else is unchanged. |
| 12 | **Cleanup** | Removed unused heartbeat workflow and redundant validate step; ranking tie-break; one-command `demo`. |

## 4.2 Experiment results (real runs, 10 Oct 2026)

| Run | Experiment | What happened | Agent 2 (blind) |
|---|---|---|---|
| r1010-155337 | **E1** worker kill, 1 worker | Active jobs → 0, queue 0 → ~52, **W1 p95 4.7 s → 29.8 s**, **W2 still "200 OK"** (silent). New worker Ready at +34 s. All 10 in-flight orders completed exactly once (we had predicted duplicates: wrong). Backlog never drained | ✅ correct, fix: run 2 workers (re-run blind after the leak fix; Gemini was over quota, so this one is the deterministic fallback) |
| r1010-155845 | E1 + strict queue limit 20 | **Auto-stopped** ("queue_waiting 22 > 20"); load stopped, chaos removed, watchdog OK, no manual action | - |
| r1010-160741 | E1, 1 worker | **Auto-stopped** (p95 24 s > 20 s after the 90 s grace) → E2 stays locked | - |
| | *Engineer applied Agent 2's fix: 2 workers* | | |
| r1010-161435 | E1 | **Skipped** by pre-flight: unhealthy warm-up (n8n-main DB connection timeouts) | - |
| r1010-162355 | E1 | **Discarded**: laptop CPU saturated (load 46 on 8 cores) → turned Grafana/Prometheus off | - |
| r1010-163348 | **E1** after fix | **Passed**: back to baseline in **78.8 s**. But 10 phantom successes + 11 lost-with-error among 28 in-flight | ✅ correct; found duplicate-key errors in Postgres logs |
| r1010-163829 | **E2** Redis kill (unlocked) | Redis back in **1.2 s**, but n8n-main + workers restarted → **~60 s total outage**; auto-stopped (p95 25.8 s) | ✅ correct, fixes: Redis persistence, higher Redis timeout, idempotent inserts |
| | *Workflows simplified (W3 + validate step removed), dashboard story added* | | |
| r1010-214152 | **`chaosctl demo --queue-abort 20`**: Agent 1 picked **E1** | 2 workers: **no visible symptom** (no auto-stop needed), replacement Ready and SLIs back in **33.2 s**; of 8 in-flight orders, 7 completed once, **1 async order lost silently** | ✅ correct, fix: graceful shutdown |

### Final end-to-end test (11 Oct 2026, Linux host, cluster rebuilt from scratch)
Two full runs of `docs/E2E_TEST_PROMPT.md`. **Run 2 (current, 11 enabled faults): 39 PASS, 4 FAIL, 0 GAP**; every
number, failure and next action: `docs/E2E_RESULTS.md`; reports and screenshots: `docs/results/` (run 1 with E1/E2
only: `docs/results/run1/`). Host: Arch Linux, 16 cores, 14 GiB RAM, 2 workers, live Mistral for Agent 1, Agent 2
and the report. Baseline: W1 105/105, p95 **4.06 s**, queue 0.

AI findings report over the 11 runs: overall **83.4 / 100 (degraded but recovered)**. Least resilient first:

| Rank | Fault | Run | Score | What happened |
|---|---|---|---|---|
| 1 | E11 Postgres pod kill | r1011-053323 | **31.2** failing | auto-stopped; of 8 in-flight orders 1 lost silently, 2 lost with error, 1 phantom success, 1 "success without row" |
| 2 | E2 Redis pod kill | r1011-052657 | **37.2** failing | 1 s Redis restart → n8n-main 1×, workers 2× restarted, W2 errors, 3 phantom successes; auto-stopped |
| 3 | E5 Mock API HTTP 500 | r1011-050940 | 65.1 fragile | W1 failures, back in 48.7 s |
| 4 | E4 Mock API latency +2 s | r1011-051312 | 75.6 | auto-stopped, 100 % W1 errors (fault proxy also breaks mock-api's health probe) |
| 5-13 | n8n-main kill, traffic spike, Postgres overload, worker kill ×3, disk full, hung worker, worker-Redis partition | | 93-100 | absorbed or recovered in 20-79 s; **every worker kill lost 1 async order silently** |

The 7 weakest/duplicate faults were disabled for the demo (`enabled: false` in `catalog.yaml`, see 2.6). The AI
report's prose has 8 factual errors against the evidence (E2E_RESULTS T7.4): review it before showing it.

## 4.3 Findings (weaknesses found)
1. **One worker has no headroom.** After a worker kill the backlog never drains under normal load.
2. **Silent failure:** async (W2) customers keep getting "200 OK" while nothing is processed, and n8n-main stays "Ready".
   Health checks alone would not notice.
3. **A 1-second Redis restart becomes a ~60-second total outage**, because n8n processes exit and restart.
4. **Phantom successes:** after a worker dies, its jobs are retried, but the waiting customers get errors although
   their order was saved.
5. **Repeated faults on one pod slow recovery** (Kubernetes back-off).
6. **Wrong prediction (good to show):** we expected duplicates after a worker kill. n8n re-ran stalled jobs and completed
   each exactly once.
7. **Even with 2 workers, a worker kill still loses work silently:** customers saw nothing (no errors, latency normal,
   back in 33 s), but 1 async order that was told "200 OK" was never saved. Health and latency alone would call this a clean pass.
8. **Jobs held by a killed worker fail instead of being retried** (E2E test, 11 Oct): every E1 run shows 2 W1 errors at
   ~61 s, matching n8n-main's "job stalled more than maxStalledCount" (the chart sets `QUEUE_WORKER_MAX_STALLED_COUNT=1`).
9. **The Redis cascade reproduces on a faster host:** a 1.0 s Redis restart still restarts n8n-main and both workers and
   stops W2 too (11 Oct, r1011-025735).

## 4.4 Current state of the cluster (as of the last session)
- Kind cluster `chaos` running.
- n8n-worker scaled to **2**. Grafana and Prometheus scaled to **0**.
- 2 workflows published. Dashboard at :8787.
- Run history in `state/history.json`: E1 passed twice, E2 tested once. Both experiments are now "tested", so the
  scorer ranks the worker kill first. The guard still orders tier 1 before tier 3. **To make Agent 1's choice interesting
  again, enable more faults (Part 5).**
- After the 11 Oct end-to-end tests (Linux host): see `docs/E2E_RESULTS.md`. Run 1 history: E1 passed 4×,
  aborted 1× (test limit), E2 aborted 1×. Open issues before the demo: `docs/E2E_RESULTS.md` → "Is the system demo-ready?".
- **LLM is now Mistral** (`ministral-14b-latest`, fallback `ministral-8b-latest`). Gemini was dropped after its quota
  ran out. If Mistral is unreachable, everything still works through the deterministic fallbacks. See the gotcha in 3.5.

---

# Part 5: What is left (Day 2)

| Priority | Task | Notes |
|---|---|---|
| ~~Must~~ Done | ~~2+ more failure types~~ | 18 enabled faults covering all 6 failure types on the organizers' list (2.6), each smoke-tested live; see PROGRESS.md for the end-to-end runs. Agent 2's detectors were written for pod kills and may need tuning for the new faults |
| Done (needs a real run) | **AI-written findings report** (`chaosctl report` → `reports/<run>/findings.md` + `.json`) | Agent 2 writes it, validated and fact-checked; tested live with Mistral on synthetic runs (3/3 AI-written), not yet on real cluster evidence. See docs/KNOWLEDGE_BASE.md |
| Done (needs a real run) | **Resilience score** (AI-judged) | Agent 2 scores each component from the evidence with the formula (`chaosctl/score.py`) as a reference, calibrated on its earlier real judgements (`state/score_memory.jsonl`), median of 3 judgements; weights availability 30 %, self-recovery 25 %, latency 20 %, data integrity 15 %, detectability 10 %. See docs/KNOWLEDGE_BASE.md |
| **Must** | Architecture one-pager, demo video (≤ 3 min), team form | The team form lists Mistral (model names), LangGraph, Chaos Mesh, Prometheus, Grafana, Kind, n8n; data = synthetic only |
| **Must** | **Clean run from scratch** on a fresh clone, following only this README | `kind delete cluster --name chaos` first. Full test plan as an AI-agent prompt: docs/E2E_TEST_PROMPT.md |
| Should | Re-run E2 after the Redis fix (`k8s/redis-hardened.yaml`) to show before/after | Stretch goal "re-run after a fix" |
| Should | Detectability: alert rules (PrometheusRule) and when they fired | Needs Prometheus back on (CPU cost) |
| **Must** | Mistral rate limits for demo day (see 3.5) | Check usage in console.mistral.ai, or pre-run the demo once so answers are cached |
| Open question | Organizers: is a hosted LLM (Mistral API) OK, or must the model be open source? (gate G7) | Mistral's open-weight models (e.g. Mistral Small) can be self-hosted; `llm.py` is one function |
| Nice | Combined failures; Grafana dashboard; SQLite checkpointer for LangGraph resume | |

Rules while working (from the original plan):
- Feature freeze at deadline - 4 h.
- Clean run at deadline - 3 h.
- Video at deadline - 2 h.
- Submit 30 min early.
- Never present a hypothesis as a fact.

---

# Part 6: Reference

## Configuration
- `.env`: `MISTRAL_API_KEY`, `MISTRAL_MODEL` (ministral-14b-latest), `MISTRAL_FALLBACK_MODEL` (ministral-8b-latest),
  `CHAOSCTL_LLM_BUDGET` (30 calls per campaign), `CHAOSCTL_KUBE_CONTEXT` (kind-chaos).
- `chaosctl/policy.yaml`:
  - abort limits: W1 error rate 50 %, p95 20 s, queue 500
  - tier caps (180 s)
  - recovery deadline 120 s
  - warm-up 60 s
  - in-flight gate (3 requests, 90 s)
  - fate wait 180 s

## Versions
| Component | Version |
|---|---|
| Kind node | Kubernetes 1.37.0 |
| Chaos Mesh | 2.8.4 |
| kube-prometheus-stack | 92.3.0 |
| n8n | 2.42.4 (chart 1.15.0) |
| Redis | 7.4.5 |
| Postgres | 17.6 |
| Python | 3.14 |
| langgraph | 1.2.14 |
| LLM | Mistral API (`ministral-14b-latest` / `ministral-8b-latest`) |

## Third-party components and licenses

| Component | License |
|---|---|
| n8n (target app) | Sustainable Use License (source-available); Helm chart repo n8n-io/n8n-hosting is MIT. Organizers confirmed n8n is acceptable |
| Chaos Mesh | Apache-2.0 |
| kube-prometheus-stack (Prometheus, Grafana) | Apache-2.0 (Grafana: AGPL-3.0) |
| Kind | Apache-2.0 |
| LangGraph | MIT |
| Mistral API | hosted service, Mistral AI terms of use |
| Redis 7.4 | RSALv2 / SSPLv1 dual license |
| PostgreSQL | PostgreSQL License |
| FastAPI, pydantic, typer, httpx, rich | MIT / BSD |

**Data used:** synthetic orders generated by our load generator only. Sent to Mistral: graph summaries, plan metadata
and evidence summaries (secrets redacted). No personal or real business data.
