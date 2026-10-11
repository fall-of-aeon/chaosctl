# Dashboard redesign plan (UI.md brief, 2026-10-11)

## What exists today (chaosctl/dash/, replaced chaosctl/dashboard.py/.html in PR #3)
- `sources.py`: live reads (loadgen SQLite, Redis/Postgres via NodePort, Kubernetes API, Chaos Mesh CRs, mock-api
  /admin, journals) and a replay source for one recorded run.
- `hub.py`: one 1 Hz collector; builds `/api/state`, `/api/components`, `/api/metrics`, `/api/events`,
  `/api/agent1`, `/api/experiments/<id>`, `/api/resilience`, `/api/stream` (SSE).
- `model.py`: pure rules (node state, map layout, workflow strip, timeline, candidates, fates, alerts banner).
- `static/`: one page (map, metrics, agents, impact, report card, event log).

## Decisions (where the brief and the repo disagree)
- The brief names `chaosctl/dashboard.py/.html`; they no longer exist. Work goes into `chaosctl/dash/` (same role).
- "87 existing tests" is now 210; all must keep passing.
- Charts: the brief prefers a vendored uPlot/Chart.js. Vendoring means downloading a file, which needs the user's OK.
  The existing hand-written SVG chart code already works offline, so it is extended (bands, markers, limit lines,
  peak labels, recovery bracket, hatched gaps, synchronized crosshair, brush, toggleable legend). No new dependency.
- The interface uses readable text labels and numbered investigation steps, without emoji.
- Replay moves to the browser: a finished run is fetched once (`/api/run/<id>/series`, `/logs`, ...) and played with a
  scrubber at 1x/4x/10x. Live mode keeps the SSE hub. `chaosctl dashboard --replay <id>` still works: it opens with
  that run selected and never touches the cluster.
- Only `chaosctl/dash/*`, a new `chaosctl/fix_summary.py`, the `diagnose` and new `fix-summary` commands in
  `cli.py`, tests, and docs change. Agents, runner, safety, catalog, policy and playbook are not touched.
- Stretch goal 1 "different recorded configurations": the worker replica count is the number of worker pods recorded
  at injection (`evidence/<exp>.plan.json` `at_inject.target_pods`) for worker experiments; other runs show
  "configuration not recorded".

## Design plan (frontend-design pass)
Subject: one chaos experiment on n8n, told to judges on a projector from 4 m. Single job of the page: "what broke,
what happened, what the AI concluded, what to fix" in under 60 s.

**Type.** Fonts must work offline, so only installed faces. Display: **DIN** (`"DIN Alternate", "DIN Condensed",
Bahnschrift, "Roboto Condensed", "Arial Narrow"`): the German engineering-signage face, legible at distance,
condensed so big numbers and act sentences fit; a deliberate nod to a Bosch audience. Body: `"Avenir Next",
system-ui`. Data: `"SF Mono", Menlo, Consolas`. Scale (rem, 1 rem = 16 px): 0.8125 secondary (13 px), 0.9375 body
(15 px), 1.125 card titles, 1.75 narrator sentences, 2.5-3 big numbers. Weights: 400 and 600 (DIN at 700 for numbers).

**Colour** (CSS custom properties, dark and light, AA contrast; light theme for washed-out projectors).

| Token | Dark | Light | Meaning |
|---|---|---|---|
| bg / surface / line | #12171e / #1a212b / #2c3644 | #f5f7fa / #ffffff / #d5dbe3 | page, cards, rules |
| text / muted | #e9eef4 / #a8b2c0 | #16202b / #4a5666 | |
| ok (system) | #4cc38a | #1e8a55 | healthy |
| agent1 | #74a6ff | #2559c7 | Agent 1 |
| fault | #ff6b5f | #c92f2f | injected fault |
| warn | #f2b84b | #9a6500 | blast radius, warning |
| agent2 | #b693ff | #6c3fcf | Agent 2 |
| fix | #3fd0c4 | #0d7c76 | fixes |

**Layout.** Sticky header; tabs Story / Live ops / Compare / Report. Story is one centred column (max 1240 px) of
acts; each act is a section with an eyebrow in its actor colour ("Act 2 · Impact"), then the narrator sentence, then
content. Acts are a real sequence, so they are numbered.

```
[run ▾] REPLAY  state  kill switch  load  T+02:13        [Story|Live ops|Compare|Report]  [present] [theme]
 Start ── Agent 1 injects ── Impact ── Agent 2 diagnoses ── Fix summary ── Resilience report   (rail)
 [scrubber |----o-----------|  1x 4x 10x  ▶]
 ACT 1 · AGENT 1 INJECTS
 "Agent 1 killed the Redis pod while 13 orders were in flight."   <- narrator line (signature)
 [ map (60%) | decision card (40%) ]
```

**Signature.** The *narrator line*: every act opens with one sentence built from real numbers, set large in DIN,
with the numbers in mono and the actor's colour ("**13** orders", "recovered in **129.8 s**"). Read top to bottom,
the six sentences are the demo script. Everything else stays quiet: thin rules, no shadows, colour only for actors
and states.

Self-critique against the brief: the first instinct (dark slate, one blue accent, Inter) is the generic default;
replaced by DIN + actor palette because the brief's actor colours carry meaning and DIN reads from 4 m. Cream/serif
and newspaper looks were not considered: projector contrast and data density rule them out.

## Build order
1. Backend run endpoints (`dash/runs.py`): `/api/runs`, `/api/run/<id>/summary|series|logs|fates|agent1|diagnosis|
   fix-summary|report`, `/api/stretch-goals`, `/api/compare?a=&b=`, `/api/report`, plus LLM settings log at start.
2. `chaosctl/fix_summary.py` + `chaosctl fix-summary --run` + call at the end of `diagnose`; tests (LLM mocked, fallback).
3. Frontend: header/tabs/theme/presentation, Story acts, chart engine, logs, Report, Compare, stretch goals.
4. Verify on r1011-021151, r1010-155845, Compare r1010-155337 vs r1010-163348; screenshots in docs/screenshots/.
