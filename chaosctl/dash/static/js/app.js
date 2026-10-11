// Controller: run picker, live vs replay, scrubber, tabs, header. The only writes are the agent controls (agents.js).
import {$, esc, DASH, nice, fix1, secs, tplus, tsec, clock, getJSON, store, healthPill} from "./util.js";
import {bus, redrawAll, swimlane} from "./chart.js";
import {buildMap, updateMap, highlight} from "./map.js";
import {LogPanel} from "./logs.js";
import * as story from "./story.js";
import {renderReportFull, renderCompare} from "./report.js";
import {setupAgents, updateAgents} from "./agents.js";
import {setupChat, chatContextChanged} from "./chat.js";

const S = {
  runs: [], sel: null, tab: "story", d: {}, cardSig: {}, cursor: null, playing: false, speed: 1, timer: null,
  live: {state: null, comps: null, points: [], events: [], ok: false, last: 0}, graphV: null,
};
const ACTS = [["start", "Start", "ok"], ["inject", "Agent 1 injects", "a1"], ["impact", "Impact", "fault"],
              ["diagnose", "Agent 2 diagnoses", "a2"], ["fix", "Fix summary", "fix"], ["report", "Resilience report", "ok"]];

// ------------------------------------------------------------------ setup
function setupChrome() {
  const present = () => document.documentElement.classList.contains("present");
  $("present").setAttribute("aria-pressed", present());
  $("present").onclick = () => {
    document.documentElement.classList.toggle("present");
    store.set("chaosctl.present", present() ? "1" : "0");
    $("present").setAttribute("aria-pressed", present());
    requestAnimationFrame(renderAll);
  };
  $("theme").onclick = () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    store.set("chaosctl.theme", document.documentElement.dataset.theme);
    requestAnimationFrame(renderAll);
  };
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", () => redrawAll());
  $("run-picker").onchange = () => { selectRun($("run-picker").value); chatContextChanged(); };
  $("scrubber").oninput = () => { S.cursor = +$("scrubber").value; stop(); renderStory(); };
  $("play").onclick = () => (S.playing ? stop() : play());
  document.querySelectorAll("[data-speed]").forEach((b) => b.onclick = () => {
    S.speed = +b.dataset.speed;
    document.querySelectorAll("[data-speed]").forEach((x) => x.setAttribute("aria-pressed", x === b));
  });
  $("drawer-close").onclick = () => { $("drawer").hidden = true; };
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") $("drawer").hidden = true; });
  // cited graph ids highlight the map; evidence ids open the log
  document.addEventListener("mouseover", (ev) => { const r = ev.target.closest?.("[data-ref]"); if (r) { highlight(r.dataset.ref); paintMap(); } });
  document.addEventListener("mouseout", (ev) => { const r = ev.target.closest?.("[data-ref]"); if (r) { highlight(null); paintMap(); } });
  document.addEventListener("focusin", (ev) => { const r = ev.target.closest?.("[data-ref]"); if (r) { highlight(r.dataset.ref); paintMap(); } });
  document.addEventListener("click", (ev) => {
    const e = ev.target.closest?.("[data-evidence]");
    if (e) openDrawer({evidence: e.dataset.evidence});
    const w = ev.target.closest?.("[data-logwin]");
    if (w) { const [a, b] = w.dataset.logwin.split(",").map(Number); bus.brush = [a, b]; redrawAll(); openDrawer({}); }
    const o = ev.target.closest?.("[data-open-run]");
    if (o) { ev.preventDefault(); location.hash = "#story"; selectRun(o.dataset.openRun); }
  });
}

function syncHeaderHeight() { document.documentElement.style.setProperty("--top-h", document.querySelector(".top").offsetHeight + "px"); }
new ResizeObserver(syncHeaderHeight).observe(document.querySelector(".top"));

function route() {
  S.tab = (location.hash || "#story").slice(1).split("?")[0];
  if (!["story", "live", "compare", "report"].includes(S.tab)) S.tab = "story";
  document.querySelectorAll(".tab").forEach((t) => { t.hidden = t.dataset.tab !== S.tab; });
  document.querySelectorAll(".tabs a").forEach((a) => a.setAttribute("aria-selected", a.dataset.tab === S.tab));
  requestAnimationFrame(renderAll);
}

// ------------------------------------------------------------------ runs + selection
async function loadRuns() {
  const d = await getJSON("/api/runs");
  S.runs = d.runs;
  const label = (r) => `${r.ref} · ${r.alias ? r.alias + " " : ""}${r.title} · ${r.target} · ${new Date(r.started * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"})} · ${r.status}${r.auto_stopped ? " (auto-stop)" : ""}`;
  $("run-picker").innerHTML = `<option value="live">LIVE: follow the cluster</option>` +
    S.runs.filter((r) => r.has_evidence || r.status === "running").map((r) => `<option value="${esc(r.ref)}">${esc(label(r))}</option>`).join("");
}

async function selectRun(ref) {
  stop();
  S.sel = ref;
  $("run-picker").value = ref;
  bus.brush = null;
  if (ref === "live") {
    S.d = {};
    $("scrub").hidden = true;
    S.cursor = null;
    await refreshLiveRun();
  } else {
    const [run, exp] = ref.split("/");
    const q = `?exp=${exp}`;
    const [summary, series, agent1, diag, fix, rep] = await Promise.all(["summary", "series", "agent1", "diagnosis", "fix-summary", "report"]
      .map((w) => getJSON(`/api/run/${run}/${w}${q}`).catch(() => null)));
    S.d = {ref, run, exp, summary, series, agent1, diag, fix, rep};
    const t = series?.cols?.t || [0];
    const last = Math.max(t[t.length - 1], ...(summary?.markers || []).map((m) => m.rel_s ?? -1e9));
    $("scrubber").min = t[0]; $("scrubber").max = Math.ceil(last);
    S.cursor = Math.ceil(last);                 // a finished run opens fully played; press Play to replay from T-60
    $("scrubber").value = S.cursor;
    $("scrub").hidden = false;
    S.logPanel = null;
  }
  renderAll();
}

/** live mode: the Story shows the active experiment (or the newest one) from disk, charts from the 1 s live buffer */
async function refreshLiveRun() {
  const st = S.live.state;
  const x = st && (st.experiment || st.last_experiment);
  if (!x) { S.d = {}; return; }
  const run = x.run_id, exp = x.id, q = `?exp=${exp}`;
  const [summary, agent1, diag, fix, rep] = await Promise.all(["summary", "agent1", "diagnosis", "fix-summary", "report"]
    .map((w) => getJSON(`/api/run/${run}/${w}${q}`).catch(() => null)));
  S.d = {ref: `${run}/${exp}`, run, exp, summary, agent1, diag, fix, rep, live: true};
}

function play() {
  S.playing = true; $("play").textContent = "Pause"; $("play").setAttribute("aria-label", "Pause replay");
  if (S.cursor >= +$("scrubber").max) S.cursor = +$("scrubber").min;
  let last = performance.now();
  S.timer = setInterval(() => {
    const now = performance.now();
    S.cursor = Math.min(+$("scrubber").max, S.cursor + (now - last) / 1000 * S.speed);
    last = now;
    $("scrubber").value = S.cursor;
    renderStory();
    if (S.cursor >= +$("scrubber").max) stop();
  }, 250);
}
function stop() {
  S.playing = false; clearInterval(S.timer);
  $("play").textContent = "Play"; $("play").setAttribute("aria-label", "Play replay");
}

// ------------------------------------------------------------------ live transport (SSE, polling fallback)
function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = (m) => { S.live.ok = true; onLive(JSON.parse(m.data)); };
  es.onerror = () => { es.close(); S.live.ok = false; pollOnce(); setTimeout(connect, 3000); };
}
async function pollOnce() {
  try {
    const [state, comps, mt, ctl] = await Promise.all([getJSON("/api/state"), getJSON("/api/components"), getJSON("/api/metrics?window=10"),
      getJSON("/api/control").catch(() => ({}))]);
    S.live.ok = true;
    onLive({state, components: comps, points: mt.points, control: ctl.control, agent2: ctl.agent2});
  } catch (e) { S.live.ok = false; renderHeader(); }
}
async function backfill() {
  try {
    const [mt, ev] = await Promise.all([getJSON("/api/metrics?window=900"), getJSON("/api/events?since=0")]);
    S.live.points = mt.points; S.live.events = ev.events;
  } catch (e) { /* the header shows the connection state */ }
}
let lastRunRef = null;
async function onLive(p) {
  const L = S.live;
  if (p.state) { if (!L.state || p.state.collected_at !== L.state.collected_at) L.last = Date.now() / 1000; L.state = p.state; }
  if (p.components) L.comps = p.components;
  for (const pt of p.points || (p.point ? [p.point] : [])) {
    const last = L.points[L.points.length - 1];
    if (last && pt.t <= last.t) { if (pt.t < last.t - 5) L.points = []; else continue; }
    L.points.push(pt);
  }
  if (L.points.length > 900) L.points.splice(0, L.points.length - 900);
  if (p.events_tail) {
    const seen = new Set(L.events.map((e) => e.ts + e.message));
    for (const e of p.events_tail) if (!seen.has(e.ts + e.message)) L.events.push(e);
    L.events = L.events.slice(-600);
  }
  if (p.versions && p.versions.graph !== S.graphV) { S.graphV = p.versions.graph; loadGraph(); }
  if (S.sel === null) {        // first message: replay server -> that run, otherwise live
    await loadRuns();
    const want = new URLSearchParams(location.search).get("run");     // ?run=<run-id>[/<exp>] opens a recorded run
    const r = want ? S.runs.find((x) => x.ref === want || x.run_id === want)
      : L.state.mode === "replay" && L.state.replay ? S.runs.find((x) => x.run_id === L.state.replay.run_id) : null;
    await selectRun(r ? r.ref : "live");
  }
  const x = L.state && (L.state.experiment || L.state.last_experiment);
  const ref = x ? `${x.run_id}/${x.id}:${x.active}:${x.phase}` : null;
  if (S.sel === "live" && ref !== lastRunRef) { lastRunRef = ref; await refreshLiveRun(); await loadRuns(); $("run-picker").value = "live"; }
  renderHeader();
  updateAgents(p, L.state, S.sel === "live" && ["story", "live"].includes(S.tab));
  if (S.tab === "live") renderLiveOps();
  if (S.sel === "live" && S.tab === "story") renderStory();
}
async function loadGraph() {
  try { const g = await getJSON("/api/graph"); buildMap(g); S.graph = g; paintMap(); } catch (e) { /* map shows its own empty state */ }
}

// ------------------------------------------------------------------ header + alerts
function renderHeader() {
  const L = S.live, st = L.state, age = Date.now() / 1000 - L.last;
  const replay = S.sel && S.sel !== "live";
  const mode = $("mode");
  if (replay) { mode.className = "mode replay"; mode.textContent = "REPLAY"; }
  else if (!L.ok) { mode.className = "mode down"; mode.textContent = "OFFLINE"; }
  else if (age > 5) { mode.className = "mode stale"; mode.textContent = `STALE ${Math.round(age)} s`; }
  else { mode.className = "mode live"; mode.textContent = "LIVE"; }
  const s = S.d.summary;
  if (replay && s) {
    $("h-state").textContent = s.status || DASH;
    $("h-kill").textContent = s.markers.some((m) => m.key === "kill_switch") ? "tripped" : "armed during the run";
    $("h-load").textContent = S.d.series?.loadgen_rows ? "replayed from loadgen.sqlite" : "no loadgen rows for this run";
    $("h-tplus").textContent = tplus(S.cursor);
  } else if (st) {
    $("h-state").textContent = st.campaign.state + (st.campaign.exp_id ? ` ${st.campaign.exp_id}` : "");
    const ks = st.kill_switch;
    $("h-kill").textContent = ks.status === "armed" ? "armed" : ks.status === "triggered" ? "tripped" : "disarmed";
    $("h-load").textContent = st.loadgen.on ? `${st.loadgen.profile}, ${fix1(st.loadgen.rate)} req/s` : "off";
    const x = st.experiment;
    $("h-tplus").textContent = x && x.active && x.t_inject ? tplus(st.now - x.t_inject) : DASH;
  }
  const al = (st && st.alerts) || [];
  $("alerts").hidden = !al.length;
  $("alerts").innerHTML = al.slice(0, 2).map((a) => `<div><b>${esc(a.label)}</b> ${esc(a.component || "")}: ${esc(a.reason)} <span class="muted">${a.count > 1 ? a.count + "x, " : ""}${clock(a.last_ts)}</span></div>`).join("")
    + (al.length > 2 ? `<div class="muted">${al.length - 2} more in the log</div>` : "");
}

// ------------------------------------------------------------------ map at the current moment
function paintMap() {
  if (!S.graph) return;
  if (S.sel === "live" || !S.d.series) { paintMapLive(); return; }
  const s = S.d.summary, se = S.d.series, c = S.cursor ?? 0;
  const i = Math.max(0, se.cols.t.findIndex((t) => t >= c));
  const at = (k) => { const v = se.cols[k] || []; for (let j = i; j >= 0; j--) if (v[j] != null) return v[j]; return null; };  // last recorded value
  const nodeState = {};
  for (const r of se.pods || []) {
    // after the last sample, keep the last recorded state (the trace ends when observation ends)
    const seg = r.segments.find((x) => x.from <= c && c <= (x.to ?? 1e9)) || (c > 0 && r.segments.length && r.segments[r.segments.length - 1].state !== "Terminating" ? r.segments[r.segments.length - 1] : null);
    if (!seg && c > 0 && r.segments.length) continue;   // a deleted pod no longer counts
    const st = seg ? seg.state : null;
    const h = !st ? "none" : st === "Ready" ? "ok" : st === "NotReady" || st === "Pending" ? "warn" : "bad";
    const prev = nodeState[r.node];
    nodeState[r.node] = !prev ? h : (prev === "bad" || h === "bad") && r.node === r.row ? "bad" : prev === "ok" && h === "ok" ? "ok" : prev === "none" ? h : "warn";
  }
  const key = {"n8n-main": at("w1_p95") != null ? `W1 p95 ${secs(at("w1_p95"))} s` : "W1 p95 -", "n8n-worker": at("q_active") != null ? `active ${at("q_active")}` : "",
               redis: at("q_wait") != null ? `queue ${at("q_wait")}` : "queue -", client: at("w2_ok") != null ? `${fix1((at("w1_ok") || 0) + (at("w2_ok") || 0))} orders/s` : ""};
  const nodes = {};
  for (const n of Object.keys(S.graph.layout?.nodes || {})) nodes[n] = {health: n === "client" ? "client" : nodeState[n] || "none", key: key[n] || ""};
  const faultBand = s.phases.find((p) => p.kind === "fault");
  const active = faultBand && c >= 0 && c <= (faultBand.to ?? 1e9);
  updateMap({nodes, fault: {target: s.target, edge: s.edge, active, label: s.title}, blast: s.guard?.blast_radius?.map((b) => b.replace("node:", "")) || []});
}
function paintMapLive() {
  const st = S.live.state, comps = S.live.comps?.nodes || {};
  const H = {Healthy: "ok", Degraded: "warn", Down: "bad", Recovering: "rec", Unknown: "none"};
  const nodes = {};
  for (const [n, c] of Object.entries(comps)) {
    const m = c.metric || {};
    const key = c.kind === "external" ? (m.load_on ? `${fix1(m.rate)} orders/s` : "load off") :
      n === "redis" ? (m.reachable ? `queue ${m.waiting}` : "unreachable") : n === "postgres" ? (m.recon_gap != null ? `gap ${m.recon_gap}` : "") :
      n === "mock-api" ? (m.delay_s != null ? `delay ${fix1(m.delay_s)} s` : "") : n === "n8n-main" ? (m.w1_p95 != null ? `W1 p95 ${secs(m.w1_p95)} s` : "") :
      n === "n8n-worker" ? `${c.ready ?? "-"}/${c.desired ?? "-"} pods` : "";
    nodes[n] = {health: c.kind === "external" ? "client" : H[c.state] || "none", key};
  }
  const x = st && st.experiment;
  updateMap({nodes, fault: x && x.active && x.t_inject ? {target: x.target, edge: x.edge, active: true, label: x.fault_label} : null,
             blast: x ? x.blast_radius : []});
}

// ------------------------------------------------------------------ story
function liveSeries(t0) {
  const P = S.live.points;
  const col = (k) => P.map((p) => p[k] ?? null);
  return {t: P.map((p) => Math.round(p.t - t0)), w1_p50: col("w1_p50"), w1_p95: col("w1_p95"), w1_ok: col("w1_ok"), w1_err: col("w1_err"),
          w2_ok: col("w2_ok"), q_wait: col("q_wait"), q_active: col("q_active"), wall_t0: t0, gaps: []};
}
function endRel(s) { return s ? (s.markers.find((m) => m.key === "end")?.rel_s ?? 1e9) : 1e9; }

function renderStory() {
  if (S.tab !== "story") return;
  const d = S.d, s = d.summary;
  if (!s) {
    for (const [k] of ACTS) story.narrate(`n-${k}`, k === "start" ? "Waiting for the first experiment. The map and the live charts below are live." : "", false);
    paintMap(); renderRail(); renderStretchOnce();
    return;
  }
  const replay = !d.live;
  const c = replay ? S.cursor : null, end = endRel(s);
  $("scrub-t").textContent = replay ? `${tsec(c)} · ${clock((s.t_inject || 0) + c)}` : "";
  const after = !replay || c >= end;
  const n = s.narration;
  story.narrate("n-start", n.start);
  story.narrate("n-inject", n.inject, replay && c < 0);
  story.narrate("n-impact", after ? n.impact : `Impact unfolding: ${tsec(c)} since injection.`, replay && c < 0);
  story.narrate("n-diagnose", after ? n.diagnose : "Agent 2 diagnoses after the run ends.", !after);
  story.narrate("n-fix", after ? n.fix : "The fix summary follows the diagnosis.", !after);
  story.narrate("n-report", after ? n.report : "The report scores finished runs.", !after);
  // live: the run's files grow as it goes (baseline, then diagnosis, fix summary, score), so repaint each card
  // when its own data changes; an unchanged card is left alone so open details and scroll survive
  const running = !!d.live && !!S.live.state?.experiment?.active;
  const cards = {baseline: [() => story.renderBaseline(s), s.baseline], agent1: [() => story.renderAgent1(d.agent1, s), d.agent1, s.limits, s.hypothesis_check],
                 agent2: [() => story.renderAgent2(d.diag), d.diag], fix: [() => story.renderFix(d.fix, s), d.fix, s.fates, s.recovery_time_s],
                 report: [() => story.renderReportMini(d.rep, running), d.rep, running]};
  if (S.cardsRef !== d.ref) { S.cardsRef = d.ref; S.cardSig = {}; }
  for (const [k, [paint, ...data]] of Object.entries(cards)) {
    const sig = JSON.stringify(data);
    if (S.cardSig[k] !== sig) { S.cardSig[k] = sig; paint(); }
  }
  story.renderAutostop(s, c);
  const data = replay ? {...d.series.cols, gaps: d.series.gaps, wall_t0: d.series.wall_t0} : liveSeries(s.t_inject || S.live.state?.now || 0);
  const x = replay ? [data.t[0], data.t[data.t.length - 1]] : [Math.min(-60, ...(data.t.length ? [data.t[0]] : [-60])), Math.max(60, ...(data.t.length ? [data.t[data.t.length - 1]] : [60]))];
  const empty = replay ? (d.series.loadgen_rows ? "No data in this range" : "No load-generator rows recorded for this run")
    : (S.live.state?.loadgen?.on ? "Waiting for samples" : "Load generator off: start a run to see data");
  story.impactCharts($("story-charts"), "st", data, s, x, c, {empty, now: replay ? c : undefined});
  if (!$("st-swim")) $("story-charts").insertAdjacentHTML("beforeend", `<div><div class="chart-head"><span class="title">Pods over time: readiness per service</span></div><div id="st-swim"></div></div>`);
  swimlane($("st-swim"), replay ? d.series.pods : livePods(s.t_inject || S.live.state?.now || 0), x, {cursor: c, empty: "No pod data recorded for this run."});
  story.renderTimeline(s, c);
  story.renderFates($("fates"), after ? s.fates : null, {pending: !after});
  paintMap(); renderRail(); renderStretchOnce();
}

function renderRail() {
  const s = S.d.summary, replay = S.d && !S.d.live, c = S.cursor;
  const end = endRel(s);
  const reached = {start: true, inject: !!s && (!replay || c >= 0), impact: !!s && (!replay || c >= 0),
                   diagnose: !!S.d.diag?.available && (!replay || c >= end), fix: !!S.d.fix?.available && (!replay || c >= end),
                   report: !!S.d.rep?.available && (!replay || c >= end)};
  if (!replay && S.live.state) {
    const steps = S.live.state.phases?.steps || [];
    const st = (n) => steps.find((x) => x.name === n)?.status;
    reached.inject = st("Inject") !== "todo"; reached.impact = ["now", "done"].includes(st("Impact and auto-stop")) || reached.impact;
  }
  let cur = 0; ACTS.forEach(([k], i) => { if (reached[k]) cur = i; });
  $("rail").innerHTML = ACTS.map(([k, label, actor], i) => `<li class="${i === cur ? "now" : reached[k] && i < cur ? "done" : ""}" style="--actor:var(--${actor})">
    <a href="#act-${k}" data-scroll="act-${k}">${esc(label)}<span class="st">${i === cur ? "now" : reached[k] ? "done" : "not yet"}</span></a></li>`).join("");
  $("rail").querySelectorAll("[data-scroll]").forEach((a) => a.onclick = (ev) => { ev.preventDefault(); $(a.dataset.scroll).scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"}); });
}

let stretchData = null;
async function renderStretchOnce() {
  if (!stretchData) { try { stretchData = await getJSON("/api/stretch-goals"); } catch (e) { stretchData = null; } }
  story.renderStretch($("stretch"), stretchData);
  story.renderStretch($("stretch2"), stretchData);
}

// ------------------------------------------------------------------ live ops
function livePods(t0) {
  const rows = {};
  const P = S.live.points;
  P.forEach((p, i) => {
    for (const [node, pods] of Object.entries(p.pods || {})) for (const [name, phase, ready, restarts] of pods) {
      const r = rows[name] || (rows[name] = {row: name, node, segments: [], marks: [], lastR: restarts});
      const st = phase === "Terminating" ? "Terminating" : phase === "Pending" ? "Pending" : ready ? "Ready" : "NotReady";
      const t = Math.round(p.t - t0), seg = r.segments[r.segments.length - 1];
      if (seg && seg.state === st) seg.to = t; else { if (seg) seg.to = t; r.segments.push({from: t, to: t, state: st}); }
      if (restarts != null && r.lastR != null && restarts > r.lastR) r.marks.push({t, kind: "restart", label: `restart ${name}`});
      r.lastR = restarts;
    }
  });
  return Object.values(rows).sort((a, b) => a.node.localeCompare(b.node) || a.row.localeCompare(b.row));
}

function renderLiveOps() {
  const st = S.live.state;
  if (!st) return;
  const x = st.experiment || st.last_experiment;
  const t0 = x && x.t_inject && st.now - x.t_inject < 900 ? x.t_inject : st.now;
  const data = liveSeries(t0);
  const xr = [st.now - t0 - 300, st.now - t0 + 5];
  $("live-caption").textContent = t0 === st.now ? "Seconds before now (no injection in the last 15 min); the last 5 minutes." : `Seconds since ${x.id} injection (${x.fault_label}); the last 5 minutes.`;
  story.impactCharts($("live-charts"), "lv", data, S.d.live ? S.d.summary : null, xr, null,
    {empty: st.loadgen.on ? "Waiting for samples" : "Load generator off: start a run to see data"});
  swimlane($("live-swim"), livePods(t0), xr, {id: "lv-swim", empty: st.sources?.cluster === "ok" ? "Collecting pod data" : "Cluster unreachable: no pod data"});
  const comps = S.live.comps?.nodes || {};
  $("tiles").innerHTML = `<div class="tiles">${Object.values(comps).filter((c) => c.kind !== "external").map((c) =>
    `<div class="svc"><b>${esc(c.name)}</b>${healthPill(c.state)}<span class="muted">${[`${c.ready ?? DASH}/${c.desired ?? DASH} Ready`,
      c.restarts_delta != null ? `${c.restarts_delta} restarts` : null, c.pods?.[0]?.age_s != null ? `age ${Math.round(c.pods[0].age_s / 60)} min` : null,
      c.pods?.find((p) => p.last_termination) ? `last exit: ${esc(c.pods.find((p) => p.last_termination).last_termination)}` : null].filter(Boolean).join(" · ")}</span></div>`).join("")}</div>`;
  if (!S.liveLog) S.liveLog = new LogPanel($("live-log"), {rows: liveLogRows}, {x: () => xr, pageSize: 100});
  else if (S.live.events.length !== S.liveLogN) { S.liveLogN = S.live.events.length; S.liveLog.render(); }
}
function liveLogRows() {
  const st = S.live.state, x = st && (st.experiment || st.last_experiment);
  const t0 = x && x.t_inject ? x.t_inject : null;
  return S.live.events.map((e, i) => ({id: i, ts: e.ts, rel_s: t0 ? Math.round(e.ts - t0) : null, severity: e.level === "fault" ? "error" : e.level === "warn" ? "warn" : "info",
    source: e.source, component: e.object ? (["n8n-main", "n8n-worker", "redis", "postgres", "mock-api"].find((n) => e.object.startsWith(n)) || "-") : e.source,
    message: e.message, evidence: null, request_id: null}));
}

// ------------------------------------------------------------------ drawer + tabs
function openDrawer({evidence}) {
  if (!S.d.run || S.d.live) {
    $("drawer").hidden = false;
    $("drawer-log").innerHTML = `<p class="muted">The full log with evidence links is available for recorded runs. Pick a run in the header.</p>`;
    return;
  }
  $("drawer").hidden = false;
  const fault = S.d.summary.phases.find((p) => p.kind === "fault");
  const panel = new LogPanel($("drawer-log"), {url: (p) => `/api/run/${S.d.run}/logs?exp=${S.d.exp}&${p}`},
    {x: () => [S.d.series.cols.t[0], S.d.series.cols.t[S.d.series.cols.t.length - 1]], faultWindow: () => fault ? [fault.from, fault.to] : null,
     cursor: () => (S.cursor != null && S.cursor < +$("scrubber").max ? S.cursor : null), pageSize: 200, onClear: redrawAll});
  if (evidence) panel.focusEvidence(evidence);
  bus.onBrush = () => panel.render();
}

function renderAll() {
  renderHeader();
  updateAgents({}, S.live.state, S.sel === "live" && ["story", "live"].includes(S.tab));
  if (S.tab === "story") { S.cardsRef = null; renderStory(); }
  if (S.tab === "live") renderLiveOps();
  if (S.tab === "compare") renderCompare(S.runs, $("cmp-a").value || null, $("cmp-b").value || null);
  if (S.tab === "report") renderReport();
}
async function renderReport() {
  try { renderReportFull(await getJSON("/api/report"), S.runs); } catch (e) { $("report-full").innerHTML = `<p class="muted">Report not available: ${esc(e.message)}</p>`; }
  renderStretchOnce();
}
$("cmp-a").onchange = $("cmp-b").onchange = () => renderCompare(S.runs, $("cmp-a").value, $("cmp-b").value);
setInterval(() => { renderHeader(); if (S.d.live && S.tab === "story" && !S.live.ok) renderStory(); }, 1000);
setInterval(() => { if (S.sel === "live") refreshLiveRun(); }, 5000);

setupChrome();
setupAgents();
setupChat(() => (S.sel && S.sel !== "live" ? S.sel : lastRunRef || S.runs[0]?.ref || null));
syncHeaderHeight();
route();
backfill().then(connect);
loadGraph();
