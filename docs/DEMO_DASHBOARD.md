# Demo roadmap: the dashboard, live for the judges and recorded for the video

One page to rehearse from. The pitch in one breath: **"Agent 1 decides what to break, a human approves, a safety
controller with no AI in it keeps it safe, and Agent 2, which is never told anything, notices by itself and works out
what broke, tool call by tool call."**

Numbers below are from run `r1011-062027` (2026-10-11, Linux host), the first run driven entirely from the page.

---

## 1. Prep checklist (T-30 min)

| | Do | Check |
|---|---|---|
| 1 | `kubectl get pods -n n8n-chaos` | 5 workloads Running/Ready, no restarts climbing |
| 2 | `ls KILL state/LOCKED.json` | neither exists (else `uv run chaosctl recover --ack`) |
| 3 | `uv run chaosctl baseline --duration 120` if the cluster was rebuilt | `state/baseline.json` fresh |
| 4 | `CHAOSCTL_LLM_BUDGET=90 uv run chaosctl report --all` | Act 5 shows the score gauge instead of a command |
| 5 | Stop any old dashboard, then `uv run chaosctl dashboard` | terminal prints `MISTRAL_API_KEY=set, MISTRAL_MODEL=ministral-14b-latest` and "Agent 2 is watching" |
| 6 | Open http://127.0.0.1:8787, pick **LIVE**, press **Presentation**, choose light theme for a projector | agents panel visible at the top; Agent 2 pill says **watching** |
| 7 | Do one dry run of section 3 (it takes 3 min) | Agent 2 triggers and concludes; then wait 60 s so it re-arms |
| 8 | Close the browser's other tabs, set zoom 100 %, 1920x1080 for recording | header fits on one line |

The banner at the top shows harness alerts from the last hour (for example "LLM NOT USED" from older runs). If it is
distracting, rehearse more than an hour before the slot, or point at it: it shows the page never hides a fallback.

## 2. Screen map (what is where)

- **Header:** run picker (LIVE or any recorded run), LIVE/REPLAY badge, campaign state, kill switch state, load,
  time since injection, tabs, **Kill switch** (red, live only), Presentation, Light/dark.
- **Agents panel** (top of Story and Live ops, LIVE only):
  - left, blue: **Agent 1**: *Ask Agent 1 to pick a fault* → plan card → *Approve and inject* / *Pick another fault* /
    *Discard*; then run state, runner output; *Start steady traffic*.
  - right, purple: **Agent 2**: state pill (watching / symptom seen / investigating / diagnosed / re-arming),
    the trigger, the investigation stepper (each step = thought + `tool:N action(service)` chip + one-line result;
    click the chip for the raw JSON), root cause, failure chain, playbook fixes; *Investigate now*, *Pause auto-trigger*.
- **Story tab**, six acts: 0 Start (map, baseline) → 1 Agent 1 decision → 2 Impact (charts, swimlane, timeline, fates)
  → 3 Agent 2 graded diagnosis → 4 Fix summary → 5 Resilience report.
- **Live ops:** live charts, pod swimlane, service tiles, merged error log, Ask Agent 2 chat.
- **Compare:** two runs on one T0 axis. **Report:** score and ranking.

## 3. Live demo for the judges (about 7 min)

Timings from `r1011-062027`: Agent 1 plans in 15-30 s; after approval, 60 s warm-up, injection once 3+ orders are in
flight, fault lasts 30 s; Agent 2 triggers about 3 s after the first unhealthy service and concludes about 45 s later;
the run ends about 2.5 min after approval; the graded diagnosis lands about 20 s after that.

| # | Time | Click / show | Say |
|---|---|---|---|
| 1 | 0:00 | Story, Act 0: the map. Hover n8n-main, Redis, worker | "n8n in queue mode: webhooks in, a Redis queue, workers, Postgres, and a downstream API. Two workflows: W1 waits for the answer, W2 is told OK at once. The map is discovered from Kubernetes." |
| 2 | 0:40 | *Start steady traffic*. Baseline numbers | "Two orders a second. Healthy: 100 % success, p95 about 4 s, no backlog. Every order has an id we look up in Postgres afterwards." |
| 3 | 1:10 | Point at Agent 2: **watching** | "Agent 2 is already running. It is never told what we do. Keep an eye on it." |
| 4 | 1:20 | *Ask Agent 1 to pick a fault* | "Agent 1 scores every fault in our catalog against the map: criticality, untested, new information, risk. Mistral picks one and has to cite ids on the map; if it cites something that does not exist, it is sent back to correct itself." |
| 5 | 1:50 | The plan card: hover the cited chips (they light up on the map) | "It chose X on Y, here is why, and here is its prediction, which we will check. The guard, plain code, set these stop limits and this blast radius." |
| 6 | 2:30 | **Approve and inject** | "Nothing ran until a human said yes. It now runs in its own process under the safety controller: preflight, 60 s warm-up, and it only injects while orders are in flight." |
| 7 | 2:40 | Switch to **Live ops** during warm-up (charts, swimlane) | "Steady. The safety controller checks every second, with no AI involved." |
| 8 | ~3:45 | Injection: latency spike, queue climbs, service tile goes red | "Fault is in. Look: W2 customers are still told OK. That is a silent failure." |
| 9 | ~3:50 | Agent 2 pill → **investigating**; read the steps as they arrive | "Agent 2 noticed by itself. Watch it think: it looks at pods, sees mock-api Ready but unreachable, probes it from outside, checks the queue, checks what customers saw. Every claim cites one of its own tool calls." |
| 10 | ~4:35 | Root cause + fixes | "Root cause: mock-api. Fixes from a vetted playbook only: retry with backoff, autoscale workers, a real health probe. Advisory, nothing is applied." |
| 11 | ~5:20 | Run ends; Story Acts 2-4 fill in (stay on LIVE) | "Recovery time, the fate of every in-flight order, Agent 1's prediction marked confirmed or refuted, and Agent 2's diagnosis graded against what was actually injected: correct." |
| 12 | 6:00 | Act 5 / Report tab | "The report scores resilience per component and ranks what to fix first." |
| 13 | 6:30 | Press **Kill switch**, then *Reset kill switch* | "One press: chaos deleted, load stopped, every service checked Ready. Also from the CLI, a file, or Ctrl-C." |

**Party trick (if time, 1 min):** with traffic on, in a terminal:
`kubectl -n n8n-chaos delete pod -l app=redis`. No plan exists, so Agent 2 cannot be peeking: it still investigates
and names Redis. Wait until its pill says **watching** first (it re-arms 30 s after everything is healthy).

**Want the automatic stop on stage?** Pick until Agent 1 proposes a high-impact fault (Redis or Postgres pod kill; use
*Pick another fault*), or run the CLI `uv run chaosctl demo --queue-abort 20` in a terminal: the dashboard follows any
run, and Agent 2 still investigates it. The red AUTO-STOP banner appears in Act 2.

## 4. Shooting the video (3 min cut)

The agents panel only exists in LIVE mode, so record the agent part live and the rest from replays.

**Takes to record**
1. **Live take** (screen record the whole of section 3, steps 4-11, about 5 min of footage). Record two or three
   takes; Agent 1 may pick different faults, keep the cleanest.
2. **Replay inserts** (no cluster needed): `uv run chaosctl dashboard --replay <run-id>`, press **Play** at 10x.
   Runs on this machine:

| Run | Fault | Why use it |
|---|---|---|
| `r1011-062027` | mockapi_http_500 | the agent-driven run; Story with Agent 2's graded diagnosis |
| `r1011-044522` | mockapi_http_500 | auto-stop: `queue_waiting 21 > 20` |
| `r1011-052657` | redis_pod_kill | auto-stop on p95; 3 phantom successes |
| `r1011-053323` | postgres_pod_kill | auto-stop; lost orders (1 silent, 2 visible) |
| `r1011-044913` | worker_pod_kill x3 | each worker kill silently lost 1 W2 order |
| `r1011-051950` | traffic_spike | backlog builds and drains, 79.2 s recovery |

**Edit (target 3:00)**

| Time | Footage | Voice-over |
|---|---|---|
| 0:00-0:15 | Map, Act 0 | the system and the two workflows |
| 0:15-0:45 | Live: Ask Agent 1 → plan card → Approve | AI picks, human approves, guard sets limits |
| 0:45-1:15 | Live ops: impact (speed up warm-up 4x) | silent failure: W2 told OK |
| 1:15-2:00 | Live: Agent 2 investigating → root cause | not told anything, finds it with tools |
| 2:00-2:20 | Replay `r1011-052657` at 10x: AUTO-STOP banner | safety controller, no AI |
| 2:20-2:45 | Story Acts 3-4: grade "correct", fix summary | graded blind, playbook-only fixes |
| 2:45-3:00 | Report gauge, kill switch press | score, and one-press stop |

**Recording settings:** 1920x1080, Presentation on, light theme, browser zoom 100 %, hide the bookmarks bar, mouse
highlight on. Speed up waits in the edit, never the agent steps (judges should see them arrive).

## 5. Talking points (architecture, 30 s each)

- **Two agents, separated on purpose.** Agent 1 sees the map and the catalog; Agent 2 sees only what an SRE would see
  (pods, events, logs, metrics, order outcomes). Blindness is enforced in code: Chaos Mesh objects and the injector's
  log lines are filtered out of every tool result.
- **AI proposes, code disposes.** The LLM can only choose catalog values (strict JSON schema), must cite real map ids
  (validator, one self-correction), and a deterministic guard sets limits and ordering. A human approves. A safety
  controller with no AI aborts on limits and restores everything.
- **Agent 2 is an investigation, not one prompt.** A bounded loop (at most 6 tool calls) of read-only tools;
  findings must cite its own tool calls, fixes must come from the playbook, or the answer is rejected.
- **Honest about fallbacks.** If Mistral is down or wrong, a rule-based path runs and the page says so with a badge.
- **Measured, not guessed.** Every in-flight order is looked up in Postgres afterwards: completed once, lost silently,
  duplicated, phantom success.

## 6. If something goes wrong on stage

| Symptom | Do |
|---|---|
| Plan card says "Deterministic fallback" | Say so: "Mistral failed validation twice, so the rule-based plan is used, and the page tells you." Continue. |
| Approve is greyed out with a reason | Read the reason: tripped kill switch → *Reset kill switch*; a run still going → wait; LOCKED → `chaosctl recover --ack` |
| Run skipped (`baseline_unstable` / `no_in_flight`) | Traffic was off or a pod was not Ready: start traffic, wait 30 s, ask again |
| Agent 2 stays **re-arming** | It waits for 30 s of all-healthy after the last incident; or press *Investigate now* |
| Agent 2 shows "Rule-based fallback" | LLM down or budget used: the fixed tool sequence still runs and still names the component |
| Something looks stuck or unsafe | **Kill switch**. Then *Reset kill switch*. |
| Cluster dead | Switch to replays (section 4) and tell the story from recorded runs |

## 7. Likely judge questions

- *Could Agent 2 just read what was injected?* No: it has no access to the plan, the journal or Chaos Mesh; its tool
  results are filtered, and the party trick (a pod deleted by hand) has no plan at all.
- *What stops the AI doing something dangerous?* It cannot name a fault outside the catalog, the guard sets limits,
  a human approves, and the safety controller (no AI) aborts and restores. The namespace and context are checked.
- *Does it fix things?* It recommends fixes from a vetted playbook with change / verify / rollback; applying them is a
  human step. The Compare tab shows a before/after re-run.
- *How do you know it was right?* After the run, its root cause is graded against the injected fault: both Agent 2
  diagnoses of `r1011-062027` graded CORRECT.
