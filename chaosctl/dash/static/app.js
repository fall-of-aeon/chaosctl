"use strict";
// chaosctl live impact page. Every value comes from the read-only API (/api/*); nothing is
// generated here. Missing data is shown as "-", never as 0.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const DASH = "-";
const MAXPTS = 900;          // 15 min of 1 s points (same bound as the server)
const STALE_S = 5;

const S = {
  graph: null, layout: null, roles: {}, conc: {}, state: null, comps: null, points: [], events: [],
  agent1: null, exp: null, versions: {graph: -1, agent1: null, experiment: null},
  lastData: 0, transportOk: false, es: null, pollTimer: null, needBackfill: true, polls: 0,
  win: 180, hoverT: null, selected: null, userSelected: false, hl: null,
  filters: {k8s: true, harness: true, safety: true}, eventsKey: "",
};

// ------------------------------------------------------------------ formatting
const clock = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], {hour12: false}) : DASH);
const tplus = (s) => (s == null ? DASH : `T+${Math.round(s)} s`);
const fix = (v, d = 1) => (v == null || Number.isNaN(+v) ? DASH : (+v).toFixed(d));
const secs = (ms) => (ms == null ? DASH : (ms / 1000).toFixed(1) + " s");
const pct = (r) => (r == null ? DASH : Math.round(r * 100) + " %");
const nice = (s) => String(s ?? "").replaceAll("_", " ");
const nodeOf = (ref) => String(ref || "").replace(/^node:/, "");
const idChip = (ref) => `<span class="id" tabindex="0" data-ref="${esc(ref)}">${esc(ref)}</span>`;
const term = (word, tip) => `<span class="term" tabindex="0" title="${esc(tip)}">${esc(word)}</span>`;
const TIPS = {
  spof: "Single point of failure: only one replica, and at least one workflow passes through it.",
  blast: "Blast radius: every component that depends on the target, directly or through others.",
  inflight: "In flight: requests sent before the fault whose outcome was not known yet at injection.",
  fate: "Fate: what finally happened to each in-flight request, from the client result and the rows in Postgres.",
  recon: "Reconciliation gap: async (W2) requests accepted more than 30 s ago that still have no order row.",
};

// ------------------------------------------------------------------ transport: SSE, polling fallback
async function j(url) {
  const r = await fetch(url, {cache: "no-store"});
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

function connect() {
  if (S.es) S.es.close();
  const es = new EventSource("/api/stream");
  S.es = es;
  es.onmessage = (m) => {
    S.transportOk = true;
    stopPolling();
    apply(JSON.parse(m.data));
  };
  es.onerror = () => {
    es.close();
    S.es = null;
    S.needBackfill = true;
    startPolling();
    setTimeout(connect, 3000);
  };
}
function startPolling() { if (!S.pollTimer) { poll(); S.pollTimer = setInterval(poll, 2000); } }
function stopPolling() { clearInterval(S.pollTimer); S.pollTimer = null; }
async function poll() {
  try {
    const [st, cp, mt, ev] = await Promise.all([j("/api/state"), j("/api/components"), j("/api/metrics?window=10"),
      j(`/api/events?since=${S.events.length ? S.events[S.events.length - 1].ts : 0}`)]);
    S.transportOk = true;
    S.polls += 1;
    apply({state: st, components: cp, points: mt.points, events_tail: ev.events,
           versions: S.polls % 5 === 1 ? {graph: -2, agent1: "poll" + S.polls, experiment: "poll" + S.polls} : null});
  } catch (e) {
    S.transportOk = false;
    renderConn();
  }
}

async function backfill() {
  S.needBackfill = false;
  try {
    const [mt, ev] = await Promise.all([j("/api/metrics?window=900"), j("/api/events?since=0")]);
    S.points = [];
    addPoints(mt.points);
    S.events = [];
    mergeEvents(ev.events);
    render();
  } catch (e) { S.needBackfill = true; }
}

function apply(p) {
  if (p.state) {
    if (!S.state || p.state.collected_at !== S.state.collected_at) S.lastData = Date.now() / 1000;
    S.state = p.state;
  }
  if (p.components) S.comps = p.components;
  if (p.point) addPoints([p.point]);
  if (p.points) addPoints(p.points);
  if (p.events_tail) mergeEvents(p.events_tail);
  if (p.versions) checkVersions(p.versions);
  else if (!S.graph) loadGraph();
  if (S.needBackfill) backfill();
  render();
}

function addPoints(pts) {
  for (const p of pts || []) {
    const last = S.points[S.points.length - 1];
    if (last && p.t < last.t - 5) S.points = [];           // replay looped back to its start
    else if (last && p.t <= last.t) continue;
    S.points.push(p);
  }
  if (S.points.length > MAXPTS) S.points.splice(0, S.points.length - MAXPTS);
}

function mergeEvents(evs) {
  const seen = new Set(S.events.map((e) => `${e.ts}|${e.message}`));
  for (const e of evs || []) if (!seen.has(`${e.ts}|${e.message}`)) S.events.push(e);
  S.events.sort((a, b) => a.ts - b.ts);
  if (S.events.length > 600) S.events.splice(0, S.events.length - 600);
}

function checkVersions(v) {
  if (v.graph !== S.versions.graph) { S.versions.graph = v.graph; loadGraph(); }
  if (v.agent1 !== S.versions.agent1) { S.versions.agent1 = v.agent1; loadAgent1(); }
  if (v.experiment !== S.versions.experiment) { S.versions.experiment = v.experiment; loadExp(); }
}
async function loadGraph() {
  try {
    const g = await j("/api/graph");
    S.graph = g.graph; S.layout = g.layout; S.roles = g.roles || {}; S.conc = g.concurrency || {}; S.graphSource = g.source;
    buildMap(); render();
  } catch (e) { /* shown as "graph missing" */ }
}
async function loadAgent1() { try { S.agent1 = await j("/api/agent1"); renderAgent1(); } catch (e) {} }
async function loadExp() { try { S.exp = await j("/api/experiments/current"); renderImpact(); renderAgent2(); } catch (e) {} }

// ------------------------------------------------------------------ connection chip
function connInfo() {
  const age = Date.now() / 1000 - S.lastData;
  if (!S.transportOk || !S.state) return {text: "Disconnected", lvl: "fault", live: false};
  if (age > STALE_S) return {text: `Stale (last update ${Math.round(age)} s ago)`, lvl: "warn", live: false};
  if (S.state.mode === "replay") return {text: "Replay", lvl: "accent", live: true};
  return {text: "Live", lvl: "ok", live: true};
}
function renderConn() {
  const c = connInfo();
  $("conn").className = `conn lvl-${c.lvl}`;
  $("conn").innerHTML = `<span class="dot"></span><span>${esc(c.text)}</span>`;
  document.body.classList.toggle("stale", !c.live);
  $("metrics-caption").textContent = c.live ? "rates over the last 5 s, sampled every second"
    : (c.lvl === "fault" ? "disconnected: values below are not live" : c.text.toLowerCase() + ": values below are not live");
  return c;
}

// ------------------------------------------------------------------ degraded banner
function renderAlerts() {
  const items = S.state.alerts || [];
  const el = $("alerts");
  el.hidden = !items.length;
  document.documentElement.style.setProperty("--alerts-h", items.length ? `${el.offsetHeight}px` : "0px");
  if (!items.length) return;
  const shown = items.slice(0, 2);
  el.innerHTML = shown.map((a) => `<div class="row ${a.kind === "error" ? "" : "warn"}"><b>${esc(a.label)}</b><span>${esc(a.component || "")}: ${esc(a.reason)}</span>
    <span class="t">${a.count > 1 ? `${a.count}x, last ` : ""}${clock(a.last_ts)}</span></div>`).join("") +
    (items.length > 2 ? `<div class="caption">${items.length - 2} more in the event log (last hour)</div>` : "");
  document.documentElement.style.setProperty("--alerts-h", `${el.offsetHeight}px`);
}

// ------------------------------------------------------------------ top bar + phases
function renderTop() {
  const st = S.state;
  $("run-id").textContent = st.mode === "replay" ? `REPLAY ${st.replay.run_id}` : (st.campaign.run_id || "no run yet");
  $("campaign").textContent = st.campaign.state + (st.campaign.exp_id ? ` (${st.campaign.exp_id})` : "");
  const ks = st.kill_switch;
  $("killswitch").innerHTML = `<span class="state lvl-${ks.status === "triggered" ? "fault" : ks.status === "armed" ? "ok" : "none"}"><span class="dot"></span>${esc(ks.text)}</span>`;
  const lg = st.loadgen;
  $("loadgen").textContent = lg.on ? `${lg.profile}, ${fix(lg.rate)} req/s` : (st.sources.loadgen === "no data" ? "no loadgen data" : "Load generator off");
}

const PHASE_TARGET = ["sec-map", "sec-metrics", "sec-agent1", "sec-map", "sec-impact", "sec-map", "sec-agent2"];
function renderPhases() {
  const ph = S.state.phases;
  $("phases").innerHTML = ph.steps.map((s, i) => `<li class="${s.status}"><a href="#${PHASE_TARGET[i]}" tabindex="-1">
    <span class="n">${i + 1}</span>${esc(s.name)}<span class="st">${s.status === "todo" ? "" : s.status}</span></a></li>`).join("");
  const cur = ph.current != null ? ph.steps[ph.current] : null;
  $("phase-caption").textContent = cur ? cur.caption : "";
}

// ------------------------------------------------------------------ map
const SVGNS = "http://www.w3.org/2000/svg";
function buildMap() {
  const L = S.layout, svg = $("map");
  if (!L) {
    svg.innerHTML = `<text x="16" y="32" style="fill:var(--muted)">Dependency graph missing: run chaosctl graph, or check the cluster.</text>`;
    svg.setAttribute("viewBox", "0 0 800 60");
    return;
  }
  svg.setAttribute("viewBox", `0 0 ${L.width} ${L.height}`);
  const mk = (id, color) => `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" style="fill:${color}"/></marker>`;
  let h = `<defs>${mk("arr", "var(--border-strong)")}${mk("arr-warn", "var(--warn)")}${mk("arr-fault", "var(--fault)")}${mk("arr-hl", "var(--accent)")}</defs>`;
  for (const z of L.zones) {
    h += `<g class="zone"><rect x="${z.x}" y="${z.y}" width="${z.w}" height="${z.h}" rx="8"/><text x="${z.x + 12}" y="${z.y + 24}">${esc(z.label)}</text></g>`;
  }
  for (const e of L.edges) {
    const w = e.label.length * 6.4 + 10;
    h += `<g class="edge${e.logical ? " logical" : ""}" id="e-${cssId(e.id)}" data-ref="${esc(e.id)}"><path d="${e.d}" marker-end="url(#arr)"/>
      <title>${esc(e.id)}: ${esc(e.from)} depends on ${esc(e.to)} (${esc(e.label)})</title>
      <g class="lbl"><rect x="${e.mid[0] - w / 2}" y="${e.mid[1] - 8}" width="${w}" height="16" rx="3"/><text x="${e.mid[0]}" y="${e.mid[1] + 4}" text-anchor="middle">${esc(e.label)}</text></g></g>`;
  }
  for (const [name, n] of Object.entries(L.nodes)) {
    const roles = name === "client" ? ["W1 sync: waits for the result", "W2 async: 200 right away"] : (n.roles || []);
    const lines = roleLines(roles);
    h += `<g class="node" id="n-${cssId(name)}" data-node="${esc(name)}" tabindex="0" role="button" aria-label="${esc(name)}">
      <rect class="box" x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="8"/>
      <text class="name" x="${n.x + 12}" y="${n.y + 24}">${esc(name)}</text>
      <text class="st" id="n-${cssId(name)}-st" x="${n.x + n.w - 12}" y="${n.y + 24}" text-anchor="end"></text>
      ${lines.map((l, i) => `<text class="role" x="${n.x + 12}" y="${n.y + 44 + i * 15}">${esc(l)}</text>`).join("")}
      <text class="meta" id="n-${cssId(name)}-m1" x="${n.x + 12}" y="${n.y + 96}"></text>
      <text class="meta muted" id="n-${cssId(name)}-m2" x="${n.x + 12}" y="${n.y + 115}"></text></g>`;
  }
  for (const it of L.lane.items) {
    h += `<g class="lane" id="l-${cssId(it.id)}"><rect x="${it.x}" y="${it.y}" width="${it.w}" height="${it.h}" rx="6"/>
      <text class="lbl" x="${it.x + 8}" y="${it.y + 15}">${esc(it.label)}</text>
      <text id="l-${cssId(it.id)}-s" x="${it.x + 8}" y="${it.y + 32}">${esc(it.role || "")}</text></g>`;
  }
  h += `<g id="overlay"></g>`;
  svg.innerHTML = h;
  svg.querySelectorAll(".node").forEach((g) => {
    const pick = () => { S.selected = g.dataset.node; S.userSelected = true; render(); };
    g.addEventListener("click", pick);
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
  });
}
const cssId = (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "_");
function roleLines(roles) {
  const out = [];
  let cur = "";
  for (const r of roles) {
    const next = cur ? `${cur}, ${r}` : r;
    if (next.length > 25 && cur) { out.push(cur); cur = r; } else cur = next;
  }
  if (cur) out.push(cur);
  return out.slice(0, 3);
}

function setText(id, text) { const el = document.getElementById(id); if (el && el.textContent !== text) el.textContent = text; }

function nodeLines(name, c) {
  const role = S.roles[name];
  if (c.kind === "external") {
    const m = c.metric || {};
    return [m.load_on ? `load: ${m.profile}, ${fix(m.rate)} req/s` : "Load generator off",
            m.load_on ? `${m.w1_pending ?? DASH} W1 clients waiting` : ""];
  }
  let m1 = c.ready == null ? "pods: no data" : `${c.ready}/${c.desired ?? DASH} pods Ready`;
  if (c.pods_reconstructed && c.ready != null) m1 = `${c.ready}/${c.desired ?? DASH} Ready (recorded)`;
  
  if (c.restarts_delta) m1 += `, ${c.restarts_delta} restart${c.restarts_delta > 1 ? "s" : ""}`;
  const m = c.metric || {};
  let m2 = "";
  if (role === "queue") m2 = m.reachable === false ? "unreachable" : m.reachable ? `waiting ${m.waiting}, active ${m.active}, keys ${m.dbsize}` : "queue: no data";
  else if (role === "db") m2 = m.reachable === false ? "unreachable" : m.recon_gap != null ? `reconciliation gap ${m.recon_gap}` : "gap: no data";
  else if (role === "mock") m2 = m.reachable === false ? "unreachable" : m.delay_s != null ? `delay ${fix(m.delay_s)} s, errors ${pct(m.error_rate)}` : "settings: no data";
  else if (role === "client_facing") m2 = m.w1_p95 != null ? `W1 p95 ${secs(m.w1_p95)}` : "W1 p95: -";
  else if (role === "consumer") m2 = `concurrency ${S.conc[name] ?? DASH} per pod`;
  return [m1, m2];
}

function currentExp() {
  const st = S.state;
  return {x: st.experiment || st.last_experiment, active: !!(st.experiment && st.experiment.active)};
}

function updateMap() {
  const L = S.layout;
  if (!L || !S.comps) return;
  const st = S.state, comps = S.comps.nodes;
  const {x, active} = currentExp();
  const injected = active && x.t_inject && st.now >= x.t_inject;
  const states = {};
  for (const name of Object.keys(L.nodes)) {
    const c = comps[name] || {};
    const g = document.getElementById(`n-${cssId(name)}`);
    if (!g) continue;
    const stt = c.kind === "external" ? "" : (c.state || "Unknown");
    states[name] = stt;
    const cls = `node ${stt}${S.selected === name ? " sel" : ""}${S.hl === `node:${name}` ? " hl" : ""}`;
    if (g.getAttribute("class") !== cls) g.setAttribute("class", cls);
    setText(`n-${cssId(name)}-st`, stt === "Unknown" ? "No data" : stt);
    const [m1, m2] = nodeLines(name, c);
    setText(`n-${cssId(name)}-m1`, m1);
    setText(`n-${cssId(name)}-m2`, m2);
    g.setAttribute("aria-label", `${name}: ${stt || "client"}. ${m1}. ${m2}`);
  }
  for (const e of L.edges) {
    const g = document.getElementById(`e-${cssId(e.id)}`);
    const to = states[e.to];
    const lvl = S.hl === e.id ? "hl" : to === "Down" ? "fault" : to === "Degraded" ? "warn" : "";
    const cls = `edge${e.logical ? " logical" : ""}${lvl ? " " + lvl : ""}`;
    if (g.getAttribute("class") !== cls) {
      g.setAttribute("class", cls);
      g.querySelector("path").setAttribute("marker-end", `url(#${lvl ? "arr-" + lvl : "arr"})`);
    }
  }
  for (const it of L.lane.items) {
    const p = (S.comps.platform || {})[it.id];
    setText(`l-${cssId(it.id)}-s`, `${it.role || ""}${p ? ": " + p.detail : ""}`);
  }
  // overlay: blast radius, first symptoms, fault marker, attack arrow
  let o = "";
  if (x) {
    if (injected) {
      for (const b of x.blast_radius || []) {
        const n = L.nodes[b];
        if (n) o += `<rect class="blast" x="${n.x - 6}" y="${n.y - 6}" width="${n.w + 12}" height="${n.h + 12}" rx="10"><title>in the blast radius of ${esc(x.target)}</title></rect>`;
      }
      for (const [node, s] of Object.entries(x.first_symptom || {})) {
        const n = L.nodes[node];
        if (n) o += `<text class="symptom" x="${n.x + 4}" y="${n.y + n.h + 22}">first symptom ${tplus(s)}</text>`;
      }
    }
    o += faultMarker(x, active, injected);
  }
  $("overlay").innerHTML = o;
}

function faultMarker(x, active, injected) {
  const L = S.layout, st = S.state;
  let anchor, nodeBox = L.nodes[x.target];
  const edge = x.edge ? L.edges.find((e) => e.id === x.edge) : null;
  if (!nodeBox && !edge) return "";
  const W = 232, H = active ? 76 : 62;
  let mx, my, ax, ay;
  if (edge) {
    mx = edge.mid[0] - W / 2; my = edge.mid[1] - H - 16; ax = edge.mid[0]; ay = edge.mid[1];
  } else {
    const n = nodeBox, free = (yy) => yy >= 36 && yy + H <= L.map_height && !Object.values(L.nodes).some((m) =>
      m !== n && m.x < n.x + W && m.x + m.w > n.x && m.y < yy + H && m.y + m.h > yy);
    const above = n.y - H - 24, below = n.y + n.h + 24;
    my = free(above) ? above : below;
    mx = n.x - (W - n.w) / 2;
    ax = n.x + n.w / 2; ay = my < n.y ? n.y + n.h : my + H;
  }
  const lines = [];
  if (active && !injected) {
    lines.push(`NEXT FAULT: ${x.fault_label}`, `${x.variant || x.rung || ""}`, `${x.phase}`);
  } else if (active) {
    lines.push(`FAULT: ${x.fault_label}`, x.pod ? `pod ${x.pod}` : `target ${x.target}${x.edge ? " -> " + x.edge.split("->")[1] : ""}`,
      `${tplus(st.now - x.t_inject)}, ${x.phase}`,
      x.in_flight ? `${x.in_flight.total} in flight at injection` : "");
  } else {
    const rt = x.recovery_time_s != null ? `recovered in ${x.recovery_time_s} s` : x.phase;
    lines.push(`LAST: ${x.id} ${x.fault_label}`, x.pod ? `pod ${x.pod}` : `target ${x.target}`, rt);
  }
  const cls = active && injected ? "marker active" : "marker past";
  let s = `<g class="${cls}"><rect x="${mx}" y="${my}" width="${W}" height="${H}" rx="6"/>`;
  lines.filter(Boolean).forEach((l, i) => { s += `<text class="${i === 0 ? "t1" : "t2"}" x="${mx + 8}" y="${my + 18 + i * 16}">${esc(l)}</text>`; });
  s += `</g>`;
  if (active && injected) {
    const cm = L.lane.items.find((it) => it.id === "chaos-mesh");
    if (cm) {
      let sx = cm.x + cm.w / 2;
      const sy = cm.y;
      const below = !edge && my > nodeBox.y;   // marker under the node: aim at its outer corner, clear of the column
      const tx = edge ? ax : below ? mx + W - 12 : ax + 24, ty = edge ? ay : below ? my + H : nodeBox.y + nodeBox.h;
      sx = Math.min(Math.max(tx, cm.x + 8), cm.x + cm.w - 8);   // leave the lane box straight below the target if possible
      s += `<path class="attack" d="M${sx},${sy} C${sx},${sy - 60} ${tx},${ty + 60} ${tx},${ty + 2}" marker-end="url(#arr-fault)"><title>Chaos Mesh applies the fault here</title></path>`;
    }
  }
  return s;
}

// ------------------------------------------------------------------ workflows + timeline
function renderStrip() {
  const wf = S.state.workflows || [];
  if (!wf.length) { $("wf-strip").innerHTML = `<span class="caption">No workflows in the graph.</span>`; return; }
  $("wf-strip").innerHTML = wf.map((w) => `<span class="wf lvl-${w.level === "ok" || w.level === "none" ? "none" : w.level}"><span class="dot"></span>
    <b>${esc(w.name)} ${esc(w.mode)}</b><span>${esc(w.text)}</span>${w.expected ? `<span class="caption">(graph says ${w.expected === "silent" ? "silent" : "client-visible"})</span>` : ""}</span>`).join("");
}

function renderTimeline() {
  const st = S.state;
  const {x, active} = currentExp();
  const cap = $("exp-caption");
  if (!x) {
    cap.textContent = "No experiment has run yet";
    $("timeline").innerHTML = `<div class="head"><span class="caption">Recovery time</span><div class="big">${DASH}</div></div><div class="caption">No experiment yet. The timeline fills in when one runs.</div>`;
    return;
  }
  if (active) cap.textContent = `${x.id}: ${x.fault_label} on ${x.target}, ${x.phase}`;
  else cap.textContent = `No experiment running. Last: ${x.id} ${x.fault} on ${x.target}, ${x.recovery_time_s != null ? `recovered in ${x.recovery_time_s} s` : x.phase}`;
  const tl = x.timeline;
  const steps = tl.steps;
  const t0 = steps[0].ts || (x.t_inject ? x.t_inject - 30 : x.t_start);
  const known = steps.map((s) => s.ts).filter(Boolean).concat(tl.marks.map((m) => m.ts));
  const t1 = Math.max(...known, active ? st.now : 0, x.t_end || 0, t0 + 60);
  const W = 1000, X = (t) => 8 + (t - t0) / (t1 - t0) * (W - 16);
  let svg = `<svg viewBox="0 0 ${W} 24" preserveAspectRatio="none" aria-hidden="true"><line class="track" x1="8" x2="${W - 8}" y1="12" y2="12"/>`;
  if (x.t_inject) svg += `<line class="band" x1="${X(x.t_inject)}" x2="${X(x.t_fault_end || (active ? st.now : x.t_end || x.t_inject))}" y1="12" y2="12"/>`;
  steps.forEach((s, i) => { if (s.ts) svg += `<circle class="seen" cx="${X(s.ts)}" cy="12" r="4"/>`; });
  if (active) svg += `<line class="now" x1="${X(st.now)}" x2="${X(st.now)}" y1="2" y2="22"/>`;
  svg += `</svg>`;
  const rt = tl.recovery_time_s;
  const head = `<div class="head"><span class="caption" title="SLIs back within 10 % of baseline for 30 s, measured by the harness">Recovery time</span>
    <div class="big">${rt != null ? rt + " s" : DASH}</div><span class="caption">${rt != null ? "inject to SLIs recovered" : active ? "measuring" : "SLIs did not recover in the window"}</span></div>`;
  const chips = steps.map((s, i) => `<span>${i + 1} ${esc(s.label)} <b>${s.ts ? clock(s.ts) : DASH}</b>${s.rel_s != null && s.key !== "t_inject" && s.key !== "baseline_stable" ? ` ${tplus(s.rel_s)}` : ""}${s.note ? ` (${esc(s.note)})` : ""}</span>`)
    .concat(tl.marks.map((m) => `<span class="mark">${esc(m.label)} <b>${clock(m.ts)}</b> ${tplus(m.rel_s)}${m.detail ? `: ${esc(m.detail)}` : ""}</span>`)).join("");
  $("timeline").innerHTML = `${head}<div>${svg}<div class="steps">${chips}${active ? `<span>now <b>${clock(st.now)}</b></span>` : ""}</div></div>`;
}

// ------------------------------------------------------------------ detail panel
function renderDetail() {
  if (!S.comps || !S.graph) { $("detail").innerHTML = `<div class="empty">Waiting for data.</div>`; return; }
  const comps = S.comps.nodes;
  const {x} = currentExp();
  if (!S.userSelected || !comps[S.selected]) {
    S.selected = (x && comps[x.target]) ? x.target
      : (S.graph.ranking || []).find((n) => comps[n] && comps[n].kind !== "external") || Object.keys(comps)[0];
  }
  const c = comps[S.selected];
  if (!c) { $("detail").innerHTML = `<div class="empty">No component selected.</div>`; return; }
  const g = S.graph.nodes.find((n) => n.name === c.name) || {};
  const lvl = {Down: "fault", Degraded: "warn", Recovering: "warn"}[c.state] || "none";
  const deps = S.graph.edges.filter((e) => e.from === c.name), rdeps = S.graph.edges.filter((e) => e.to === c.name);
  const br = g.blast_radius || {nodes: []};
  const wfa = g.workflows_affected || {};
  const evs = S.events.filter((e) => (e.object && e.object.startsWith(c.name)) || (e.source !== "k8s" && e.message.includes(c.name))).slice(-6).reverse();
  const tested = c.tested || [];
  const pods = (c.pods || []).map((p) => `<tr><td class="mono">${esc(p.name)}</td><td>${esc(p.phase)}</td><td>${p.ready ? "yes" : "no"}</td>
    <td class="num">${p.restarts_delta ?? DASH} / ${p.restarts ?? DASH}</td><td class="num">${p.age_s != null ? age(p.age_s) : DASH}</td><td>${esc(p.last_termination || DASH)}</td></tr>`).join("");
  $("detail").innerHTML = `
    <div class="head"><h2>${esc(c.name)}</h2>${c.kind === "external" ? "" : `<span class="state lvl-${lvl}"><span class="dot"></span>${esc(c.state === "Unknown" ? "No data" : c.state)}</span>`}</div>
    <p class="why">${c.why && c.why.length ? esc(c.why.join("; ")) : c.kind === "external" ? "Synthetic clients from the load generator." : "All signals normal."}</p>
    <dl>
      <dt>Kind</dt><dd>${esc(c.kind)}${g.workload ? `, ${esc(g.workload)}` : ""}</dd>
      ${c.kind === "external" ? "" : `<dt>Replicas</dt><dd>${c.replicas ?? DASH}${c.spof ? `, ${term("SPOF", TIPS.spof)}` : ""}</dd>
      <dt>Ready pods</dt><dd>${c.ready ?? DASH} of ${c.desired ?? DASH}</dd>
      <dt>Restarts</dt><dd>${c.restarts_delta ?? DASH} ${esc(S.comps.restart_baseline || "")}, ${c.restarts_lifetime ?? DASH} lifetime</dd>
      <dt>Pods replaced</dt><dd>${c.replaced ?? DASH} ${esc(S.comps.restart_baseline || "")}</dd>`}
      <dt>Criticality</dt><dd class="mono">${fix(g.criticality, 2)}</dd>
    </dl>
    ${c.kind === "external" ? "" : `<h3>Pods ${c.pods_reconstructed ? "(replay: names from recorded events)" : ""}</h3>
    ${pods ? `<table><thead><tr><th>Name</th><th>Phase</th><th>Ready</th><th class="num" title="since baseline / lifetime">Restarts</th><th class="num">Age</th><th>Last termination</th></tr></thead><tbody>${pods}</tbody></table>` : `<div class="caption">${c.ready == null ? "No pod data (cluster unreachable or not recorded)." : "Pod list not recorded."}</div>`}`}
    <h3>Depends on</h3><div class="ids">${deps.map((e) => idChip(`node:${e.to}`) + `<span class="caption">${esc(e.flow || "")}</span>`).join(" ") || `<span class="caption">nothing</span>`}</div>
    <h3>Depended on by</h3><div class="ids">${rdeps.map((e) => idChip(`node:${e.from}`)).join(" ") || `<span class="caption">nothing</span>`}</div>
    <h3>${term("Blast radius", TIPS.blast)}</h3><div class="ids">${br.nodes.map((n) => idChip(`node:${n}`)).join(" ") || `<span class="caption">none</span>`}${br.client_impact ? ` <span class="caption">and clients</span>` : ""}</div>
    <h3>Workflows affected</h3><div class="caption">${Object.entries(wfa).map(([w, v]) => `${esc(w)}: ${v === "silent" ? "silent (clients still get 200)" : v === "client_visible" ? "client-visible" : esc(v)}`).join(", ") || "none"}</div>
    <h3>Recent events</h3>${evs.length ? `<table>${evs.map((e) => `<tr><td class="mono">${clock(e.ts)}</td><td>${esc(e.message)}</td></tr>`).join("")}</table>` : `<div class="caption">none in the buffer</div>`}
    <h3>Faults already tested here</h3>${tested.length ? `<table>${tested.map((h) => `<tr><td>${esc(nice(h.fault))}</td><td class="mono">${esc(h.run_id)}</td><td>${esc(h.status)}${h.passed ? ", passed" : ""}</td></tr>`).join("")}</table>` : `<div class="caption">none recorded</div>`}`;
}
const age = (s) => (s < 120 ? `${s} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`);

// ------------------------------------------------------------------ tiles + charts
function lastPoint() { return S.points[S.points.length - 1] || {}; }
function renderTiles() {
  const st = S.state, p = lastPoint(), b = st.baseline || {};
  const off = !st.loadgen.on;
  const offTxt = st.sources.loadgen === "no data" ? "no loadgen data" : "Load generator off";
  const ratio = p.w1_p95 && b.w1_p95_ms ? p.w1_p95 / b.w1_p95_ms : null;
  const fr = S.exp && S.exp.available && S.exp.fates && S.exp.fates.total ? S.exp.fates : null;
  const lost = fr ? fr.rows.filter((r) => ["lost_silently", "lost_visible", "duplicated"].includes(r.fate)).reduce((a, r) => a + r.count, 0) : null;
  const q = st.sources.redis;
  const tiles = [
    ["W1 completed / s", off ? DASH : fix(p.w1_ok), off ? offTxt : `errors ${fix(p.w1_err)} / s, ${p.w1_pending ?? DASH} clients waiting`, off],
    ["W1 p95 latency", off ? DASH : secs(p.w1_p95), off ? offTxt : ratio ? `${ratio.toFixed(1)}x baseline (${secs(b.w1_p95_ms)})` : b.w1_p95_ms ? `baseline ${secs(b.w1_p95_ms)}` : "no baseline captured", off],
    ["W2 accepted / s", off ? DASH : fix(p.w2_ok), off ? offTxt : `clients told 200 OK${p.w2_err ? `, errors ${fix(p.w2_err)} / s` : ""}`, off],
    ["Redis queue: jobs waiting", q === "ok" ? String(p.q_wait ?? DASH) : DASH, q === "ok" ? `active ${p.q_active ?? DASH}, keys ${p.dbsize ?? DASH}` : q === "unreachable" ? "Redis unreachable" : "no data", q !== "ok"],
    ["Lost + duplicated requests", lost == null ? DASH : String(lost), fr ? `of ${fr.total} ${"in flight"} at injection (${S.exp.id})` : S.exp && S.exp.active ? "classified after recovery" : "no finished experiment yet", lost == null],
  ];
  $("tiles").innerHTML = tiles.map(([l, v, s, o]) => `<div class="tile${o ? " off" : ""}"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div><div class="sub">${esc(s)}</div></div>`).join("");
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  for (const f of [1, 2, 2.5, 5, 10]) if (f * p >= raw) return f * p;
  return 10 * p;
}

function chartMarks() {
  const out = [], bands = [];
  for (const x of [S.state.experiment, S.state.last_experiment]) {
    if (!x || !x.t_inject) continue;
    bands.push([x.t_inject, x.t_fault_end || (x.active ? S.state.now : x.t_end || x.t_inject)]);
    for (const s of x.timeline.steps) {
      if (!s.ts) continue;
      const label = {t_inject: "inject", t_first_symptom: "first symptom", t_slis_recovered: "recovered"}[s.key];
      if (label) out.push({t: s.ts, label});
    }
    for (const m of x.timeline.marks) out.push({t: m.ts, label: m.label});
  }
  return {marks: out, bands};
}

function chart(svgId, hoverId, series, opts) {
  const svg = $(svgId);
  const W = svg.clientWidth || 400, H = svg.clientHeight || 180;
  const m = {l: 44, r: 8, t: 18, b: 20};
  const now = S.state.now, t0 = now - S.win;
  const pts = S.points.filter((p) => p.t >= t0 && p.t <= now + 1);
  const X = (t) => m.l + (t - t0) / S.win * (W - m.l - m.r);
  let ymax = opts.min;
  for (const s of series) for (const p of pts) { const v = s.get(p); if (v != null && v > ymax) ymax = v; }
  if (opts.ref != null) ymax = Math.max(ymax, opts.ref * 1.3);
  const step = niceStep(ymax / 4);
  ymax = Math.ceil(ymax / step) * step;
  const Y = (v) => H - m.b - v / ymax * (H - m.t - m.b);
  let g = "";
  const {marks, bands} = chartMarks();
  for (const [a, b] of bands) {
    const xa = Math.max(m.l, X(a)), xb = Math.min(W - m.r, X(b));
    if (xb > xa) g += `<rect class="band" x="${xa}" y="${m.t}" width="${xb - xa}" height="${H - m.t - m.b}"/>`;
  }
  for (let v = 0; v <= ymax + 1e-9; v += step) {
    g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${m.l - 4}" y="${Y(v) + 3}" text-anchor="end">${opts.fmt(v)}</text>`;
  }
  g += `<text x="${m.l - 4}" y="${m.t - 6}" text-anchor="end">${opts.unit}</text>`;
  for (let i = 0; i <= 3; i++) { const t = t0 + S.win * i / 3; g += `<text x="${X(t)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === 3 ? "end" : "middle"}">${clock(t)}</text>`; }
  g += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  if (opts.ref != null) g += `<line class="base" x1="${m.l}" x2="${W - m.r}" y1="${Y(opts.ref)}" y2="${Y(opts.ref)}"/>`;
  let lane = 0;
  for (const mk of marks.sort((a, b) => a.t - b.t)) {
    if (mk.t < t0 || mk.t > now) continue;
    const xx = X(mk.t);
    const right = xx > W - m.r - 90;
    g += `<line class="mk" x1="${xx}" x2="${xx}" y1="${m.t}" y2="${H - m.b}"/><text x="${right ? xx - 3 : xx + 3}" y="${m.t + 10 + (lane++ % 3) * 11}" text-anchor="${right ? "end" : "start"}">${esc(mk.label)}</text>`;
  }
  for (const s of series) {
    let d = "", pen = false;
    for (const p of pts) {
      const v = s.get(p);
      if (v == null) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${X(p.t).toFixed(1)},${Y(v).toFixed(1)}`;
      pen = true;
    }
    g += `<path class="${s.cls}" d="${d}" fill="none" stroke-width="1.75" stroke-linejoin="round"/>`;
  }
  let hov = "";
  if (S.hoverT != null && S.hoverT >= t0 && S.hoverT <= now) {
    const hp = nearest(pts, S.hoverT);
    if (hp) {
      g += `<line class="cursor" x1="${X(hp.t)}" x2="${X(hp.t)}" y1="${m.t}" y2="${H - m.b}"/>`;
      hov = `${clock(hp.t)}  ` + series.map((s) => `${s.name} ${s.get(hp) == null ? DASH : opts.fmtv(s.get(hp))}`).join("  ");
    }
  }
  g += `<rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/>`;
  svg.innerHTML = g;
  $(hoverId).textContent = hov;
  if (!svg.dataset.bound) {
    svg.dataset.bound = "1";
    svg.addEventListener("mousemove", (ev) => {
      const r = svg.getBoundingClientRect();
      S.hoverT = (S.state.now - S.win) + (ev.clientX - r.left - 44) / (r.width - 52) * S.win;
      renderCharts();
    });
    svg.addEventListener("mouseleave", () => { S.hoverT = null; renderCharts(); });
  }
}
function nearest(pts, t) {
  let best = null, bd = Infinity;
  for (const p of pts) { const d = Math.abs(p.t - t); if (d < bd) { bd = d; best = p; } }
  return bd <= 2 ? best : null;
}
function renderCharts() {
  if (!S.state) return;
  const b = S.state.baseline || {};
  chart("c-lat", "h-lat", [{name: "p95", cls: "s1", get: (p) => (p.w1_p95 == null ? null : p.w1_p95 / 1000)}],
    {min: 5, unit: "s", ref: b.w1_p95_ms ? b.w1_p95_ms / 1000 : null, fmt: (v) => fix(v, v < 10 ? 1 : 0), fmtv: (v) => fix(v) + " s"});
  chart("c-tp", "h-tp", [
    {name: "W1 completed", cls: "s1", get: (p) => p.w1_ok},
    {name: "W2 accepted", cls: "s2", get: (p) => p.w2_ok},
    {name: "W1 errors", cls: "s3", get: (p) => p.w1_err}],
    {min: 2, unit: "req/s", fmt: (v) => fix(v, v < 10 ? 1 : 0), fmtv: (v) => fix(v)});
  chart("c-q", "h-q", [
    {name: "waiting", cls: "s1", get: (p) => p.q_wait},
    {name: "active", cls: "s2", get: (p) => p.q_active}],
    {min: 10, unit: "jobs", fmt: (v) => String(Math.round(v)), fmtv: (v) => String(v)});
}
$("chart-legend").innerHTML = `<span><i class="s1"></i>primary series: W1 p95, W1 completed, jobs waiting</span><span><i class="s2"></i>W2 accepted, jobs active</span>
  <span><i class="s3"></i>W1 errors</span><span><i class="base"></i>baseline p95</span><span><i class="band"></i>fault active</span><span>gaps: no data (load off or probe unreachable)</span>`;

// ------------------------------------------------------------------ Agent 1
function renderAgent1() {
  const a = S.agent1, el = $("sec-agent1");
  if (!a || !a.available) {
    el.innerHTML = `<div class="card-head"><h2>Agent 1 decision</h2></div><div class="empty">No decision yet. Run chaosctl plan or an experiment.</div>`;
    return;
  }
  const c = a.chosen || {}, v = a.verdict || {}, ab = v.abort || {};
  const src = a.source || {};
  const rows = a.candidates.map((r) => `<tr class="${r.status !== "Allowed" ? "muted" : ""}"><td>${r.chosen ? "<b>chosen</b> " : ""}${esc(nice(r.fault))}${r.count > 1 ? ` (x${r.count})` : ""}</td>
    <td class="mono">${esc(nodeOf(r.target))}</td><td class="mono">${esc(r.rung)}${r.variant ? `: ${esc(r.variant)}` : ""}</td><td class="num">${r.tier ?? DASH}</td>
    <td class="num">${r.score == null ? DASH : r.score.toFixed(2)}</td><td>${r.status === "Not enabled" ? DASH : r.tested ? "tested before" : "untested"}</td>
    <td title="${esc(r.status_why)}">${esc(r.status)}</td></tr>`).join("");
  el.innerHTML = `<div class="card-head"><h2>Agent 1 decision</h2><span class="caption">${esc(a.run_id)} at ${clock(a.ts)}, plan of ${a.plan_size}</span></div>
    <dl class="decision">
      <dt>Chosen experiment</dt><dd><b>${esc(nice(c.fault))}</b> on ${idChip(c.target)} <span class="caption">${esc(c.id || "")}, variant ${esc(c.rung)}${a.chosen_variant ? ` (${esc(a.chosen_variant)})` : ""}</span></dd>
      <dt>Reason</dt><dd>${esc(c.reason)}</dd>
      <dt>Cites</dt><dd class="ids">${(c.cites || []).map(idChip).join(" ")}</dd>
      <dt>Hypothesis</dt><dd>${esc(c.hypothesis)}<span class="tag">hypothesis, not fact</span></dd>
      <dt>Guard verdict</dt><dd><b>${esc(v.verdict || DASH)}</b>${v.rationale ? `: ${esc(v.rationale)}` : ""}
        <div class="caption">${term("blast radius", TIPS.blast)}: ${(v.blast_radius || []).map(idChip).join(" ") || "none"}.
        Auto-stop if W1 errors &gt; ${ab.w1_error_rate != null ? pct(ab.w1_error_rate) : DASH}${ab.error_rate_armed_after_s ? ` (armed after ${ab.error_rate_armed_after_s} s)` : ""},
        W1 p95 &gt; ${secs(ab.w1_p95_ms)}, jobs waiting &gt; ${ab.queue_waiting ?? DASH}, or after ${ab.max_total_s ?? DASH} s. ${a.guard_notes.length ? esc(a.guard_notes.join("; ")) : ""}</div></dd>
      <dt>Source</dt><dd>${esc(src.text)}${src.why ? ` <span class="caption">(${esc(src.why)})</span>` : ""}</dd>
    </dl>
    <details><summary><h3>Candidates considered (${a.candidates.length})</h3><span class="caption">Allowed first, then by score. Score = 0.4 criticality + 0.3 untested + 0.2 new information - 0.1 risk tier.</span></summary>
      <div class="inner-scroll"><table><thead><tr><th>Experiment</th><th>Target</th><th>Variant / parameter</th><th class="num">Tier</th><th class="num">Score</th><th>History</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
}

// ------------------------------------------------------------------ impact + fates
function renderImpact() {
  const e = S.exp, el = $("sec-impact");
  if (!e || !e.available) {
    el.innerHTML = `<div class="card-head"><h2>Impact and result</h2></div><div class="empty">No experiment has run yet.</div>`;
    return;
  }
  const p = e.plan || {}, f = e.fates, fl = e.in_flight_at_inject || {};
  const total = Object.values(fl).reduce((a, b) => a + b, 0);
  const rows = f.rows.map((r) => `<tr class="${r.muted ? "muted" : ""}${r.bad ? " bad" : ""}"><td class="lbl">${esc(r.label)}</td><td class="num">${r.count}</td>
    <td><div class="bar"><i style="width:${Math.round(r.share * 100)}%"></i></div></td><td>${esc(r.meaning)}</td></tr>`).join("");
  const ids = f.rows.filter((r) => ["lost_silently", "lost_visible", "duplicated"].includes(r.fate)).flatMap((r) => r.request_ids.map((id) => `${r.label}: ${id}`));
  el.innerHTML = `<div class="card-head"><h2>Impact and result</h2><span class="caption">${esc(e.run_id)} ${esc(e.id)}</span></div>
    <div class="two"><dl class="decision">
      <dt>Experiment</dt><dd>${esc(nice(p.fault))} on ${idChip(p.target)} <span class="caption">variant ${esc(p.rung)}</span></dd>
      <dt>Status</dt><dd>${e.active ? "running" : esc(e.status || DASH)}${e.abort_reason ? `, auto-stopped: <span class="mono">${esc(e.abort_reason)}</span>` : ""}</dd>
      <dt>Recovery time</dt><dd class="mono">${e.recovery_time_s != null ? e.recovery_time_s + " s" : e.active ? "measuring" : "SLIs did not recover in the window"}</dd>
      <dt>Manual action</dt><dd>${e.manual_action_needed == null ? DASH : e.manual_action_needed ? "needed (watchdog restarted a workload)" : "none: self-recovered"}</dd>
      <dt>${term("In flight", TIPS.inflight)} at injection</dt><dd class="mono">${total ? `${total} (W1 ${fl.W1 ?? 0}, W2 ${fl.W2 ?? 0})` : DASH}</dd>
      <dt>Restarts during run</dt><dd class="mono">${e.restarts_delta ? Object.entries(e.restarts_delta).filter(([, v]) => v).map(([k, v]) => `${k} ${v > 0 ? "+" : ""}${v}`).join(", ") || "none" : DASH}</dd>
    </dl>
    <div><h3>${term("Fate", TIPS.fate)} of ${f.total ? `the ${f.total} ` : ""}in-flight requests</h3>
      ${f.total ? `<table class="fate"><thead><tr><th>Outcome</th><th class="num">Count</th><th>Share</th><th>Meaning</th></tr></thead><tbody>${rows}</tbody></table>
      ${ids.length ? `<details><summary><span class="caption">Request ids for lost and duplicated requests (${ids.length})</span></summary><div class="inner-scroll mono caption">${ids.map(esc).join("<br>")}</div></details>` : ""}`
      : `<div class="empty">${e.active ? "Classified after recovery (the harness waits up to 180 s for stragglers)." : "No fate table recorded."}</div>`}
    </div></div>`;
}

// ------------------------------------------------------------------ Agent 2
function renderAgent2() {
  const e = S.exp, el = $("sec-agent2");
  const d = e && e.available ? e.diagnosis : null;
  if (!d) {
    el.innerHTML = `<div class="card-head"><h2>Agent 2: blind diagnosis</h2><span class="badge">not told what was injected</span></div>
      <div class="empty">${e && e.active ? "Runs after the experiment ends." : "No diagnosis for this experiment yet (chaosctl diagnose)."}</div>`;
    return;
  }
  const src = d.source || {};
  el.innerHTML = `<div class="card-head"><h2>Agent 2: blind diagnosis</h2><span class="badge">not told what was injected</span>
      <span class="badge">${esc(src.text)}</span>${src.why ? `<span class="caption">${esc(src.why)}</span>` : ""}</div>
    <div class="two"><div><h3>Reasoning</h3><ol class="steps">${d.thoughts.map((t) => `<li><span class="lbl">${esc(t.step)}</span><span>${esc(t.text)}</span></li>`).join("")}</ol></div>
    <div><div class="rootcause"><h3>Root cause</h3><div>${esc(d.root_cause)}</div>
      <div class="caption" style="margin-top:8px">${d.failure_chain.map((s, i) => `${i + 1}. ${esc(s)}`).join("<br>")}</div></div>
      <h3>Recommended fixes (advisory, from the vetted playbook)</h3>
      <ol class="fixes">${d.fixes.map((f) => `<li><b>${esc(f.title)}</b><div class="meta">severity <span class="sev-${esc(f.severity)}">${esc(f.severity || DASH)}</span>, effort ${esc(f.effort || DASH)}, impact ${esc(f.impact || DASH)}</div>
        <div>${esc(f.why)}</div><details><summary><span class="caption">runbook</span></summary><pre>${Object.entries(f.runbook).map(([k, v]) => `${k}: ${esc(v)}`).join("\n")}</pre></details></li>`).join("")}</ol>
      ${e.grade ? `<p class="caption">Matches injected fault: ${e.grade.root_cause_correct ? "yes" : "no"} (offline check against the plan: ${esc(nice(e.grade.fault))} on ${esc(e.grade.actual_target)})</p>` : ""}
    </div></div>`;
}

// ------------------------------------------------------------------ event log
function renderEvents() {
  const {x} = currentExp();
  const t0 = x && x.t_inject;
  const evs = S.events.filter((e) => S.filters[e.source]);
  const key = `${evs.length}:${evs.length ? evs[evs.length - 1].ts : 0}:${JSON.stringify(S.filters)}:${t0}`;
  if (key === S.eventsKey) return;
  S.eventsKey = key;
  $("events-count").textContent = `${S.events.length} events (Kubernetes, harness journal, safety controller), newest first`;
  $("events").innerHTML = evs.slice().reverse().slice(0, 400).map((e) => `<tr class="${e.level}"><td>${clock(e.ts)}</td>
    <td>${t0 && e.ts >= t0 - 300 ? (e.ts >= t0 ? tplus(e.ts - t0) : `T${Math.round(e.ts - t0)} s`) : DASH}</td><td>${esc(e.source)}</td><td>${esc(e.message)}</td></tr>`).join("");
}
document.querySelectorAll("[data-src]").forEach((b) => b.addEventListener("click", () => {
  S.filters[b.dataset.src] = !S.filters[b.dataset.src];
  b.setAttribute("aria-pressed", S.filters[b.dataset.src]);
  renderEvents();
}));

// ------------------------------------------------------------------ interactions
document.querySelectorAll("[data-win]").forEach((b) => b.addEventListener("click", () => {
  S.win = +b.dataset.win;
  document.querySelectorAll("[data-win]").forEach((x) => x.setAttribute("aria-pressed", x === b));
  renderCharts();
}));
const hlOn = (ev) => { const r = ev.target.closest && ev.target.closest("[data-ref]"); if (r && !r.closest("#map")) { S.hl = r.dataset.ref; updateMap(); } };
const hlOff = (ev) => { const r = ev.target.closest && ev.target.closest("[data-ref]"); if (r && !r.closest("#map")) { S.hl = null; updateMap(); } };
document.addEventListener("mouseover", hlOn);
document.addEventListener("mouseout", hlOff);
document.addEventListener("focusin", hlOn);
document.addEventListener("focusout", hlOff);
window.addEventListener("resize", () => renderCharts());

// ------------------------------------------------------------------ render loop
function render() {
  const c = renderConn();
  if (!S.state || !S.comps) return;
  renderTop();
  renderAlerts();
  renderPhases();
  if (S.layout && !$("overlay")) buildMap();
  updateMap();
  renderStrip();
  renderTimeline();
  renderDetail();
  renderTiles();
  renderCharts();
  renderEvents();
  if (!S.agent1) renderAgent1();
  if (!S.exp) { renderImpact(); renderAgent2(); }
  return c;
}
setInterval(renderConn, 1000);   // the chip ages even when no message arrives
connect();
