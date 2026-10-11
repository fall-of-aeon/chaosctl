// SVG time-series charts on a shared "seconds since injection" axis.
// One registry, so the crosshair, the brushed range and the flash marker are the same on every chart.
import {esc, DASH, clock, tsec, showTip, hideTip} from "./util.js";

const charts = new Map();          // id -> {host, cfg}
export const bus = {hoverT: null, brush: null, flashT: null, hidden: new Set(), onBrush: null};

export function redrawAll() { for (const [id, c] of charts) if (document.body.contains(c.host)) draw(id); else charts.delete(id); }
export function setHover(t) { bus.hoverT = t; redrawAll(); }
export function flash(t) {
  bus.flashT = t; redrawAll();
  clearTimeout(flash._t); flash._t = setTimeout(() => { bus.flashT = null; redrawAll(); }, 4000);
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  for (const f of [1, 2, 2.5, 5, 10]) if (f * p >= raw) return f * p;
  return 10 * p;
}

/** cfg: {id, title, unit, x:[min,max], series:[{key,name,color,type:'line'|'area'|'bars',t,v,dash}], bands, markers,
 *  refs:[{y,label,kind:'base'|'limit',from}], gaps:[{from,to,reason,series}], peak, bracket, callouts:[{t,text}],
 *  empty, now, wallT0, fmt, yMin} */
export function chart(host, cfg) {
  charts.set(cfg.id, {host, cfg});
  if (!host.dataset.built) {
    host.dataset.built = "1";
    host.classList.add("chart-wrap");
    host.innerHTML = `<div class="chart-head"><span class="title"></span><span class="legend" role="group" aria-label="Series"></span></div>
      <p class="chart-sum" hidden></p><svg class="chart" role="img"></svg>`;
    bind(cfg.id, host.querySelector("svg"));
    let lastW = 0;   // drawn while hidden the svg is 0 px wide: redraw at the real width (and when panels open/close)
    new ResizeObserver(() => {
      const w = host.querySelector("svg").clientWidth;
      if (w && Math.abs(w - lastW) > 2 && charts.has(cfg.id)) { lastW = w; draw(cfg.id); }
    }).observe(host);
  }
  draw(cfg.id);
}

function bind(id, svg) {
  const toT = (ev) => {
    const {cfg} = charts.get(id), r = svg.getBoundingClientRect(), m = margins(cfg);
    return cfg.x[0] + (ev.clientX - r.left - m.l) / (r.width - m.l - m.r) * (cfg.x[1] - cfg.x[0]);
  };
  let down = null;
  svg.addEventListener("mousemove", (ev) => {
    const t = toT(ev);
    if (down != null) { bus.brush = [Math.min(down, t), Math.max(down, t)]; }
    setHover(t);
    tooltip(id, ev, t);
  });
  svg.addEventListener("mouseleave", () => { setHover(null); hideTip(); });
  svg.addEventListener("mousedown", (ev) => { down = toT(ev); bus.brush = null; });
  window.addEventListener("mouseup", () => {
    if (down == null) return;
    down = null;
    if (bus.brush && bus.brush[1] - bus.brush[0] < 2) bus.brush = null;
    redrawAll();
    bus.onBrush && bus.onBrush(bus.brush);
  });
}

const LANE = 14;   // px per row of event-marker labels, drawn above the plot so they never cover the data
function margins(cfg, laneRows = 0) { return {l: 64, r: 16, t: laneRows * LANE + (cfg.bracket || cfg.bands?.length ? 30 : 14), b: 26}; }

/** place marker labels in up to 3 rows above the plot, left to right, without overlaps */
function laneLayout(markers, X, W, m) {
  const ends = [], out = [];
  for (const mk of markers) {
    const xx = X(mk.t), w = mk.label.length * 6.2 + 10;
    const right = xx + w > W - m.r;
    const a = right ? xx - w : xx, z = right ? xx : xx + w;
    let row = ends.findIndex((e) => a > e + 4);
    if (row < 0) row = ends.length < 3 ? ends.length : ends.indexOf(Math.min(...ends));
    ends[row] = Math.max(ends[row] ?? -1e9, z);
    out.push({mk, xx, row, right});
  }
  return {rows: Math.max(0, ...out.map((o) => o.row + 1)), items: out};
}

function valueAt(s, t) {
  let best = null, bd = 1.6;
  for (let i = 0; i < s.t.length; i++) { const d = Math.abs(s.t[i] - t); if (d < bd && s.v[i] != null) { bd = d; best = s.v[i]; } }
  return best;
}

function tooltip(id, ev, t) {
  const {cfg} = charts.get(id);
  const vis = cfg.series.filter((s) => !bus.hidden.has(cfg.id + ":" + s.key));
  const rows = vis.map((s) => { const v = valueAt(s, t); return `<div class="r"><span>${esc(s.name)}</span><span class="mono">${v == null ? DASH : esc(cfg.fmt(v))} ${esc(cfg.unit)}</span></div>`; });
  const wall = cfg.wallT0 != null ? ` · ${clock(cfg.wallT0 + t)}` : "";
  showTip(ev, `<b>${tsec(t)}</b><span class="muted">${wall}</span>${rows.join("")}`);
}

function draw(id) {
  const {host, cfg} = charts.get(id);
  const svg = host.querySelector("svg");
  host.querySelector(".title").textContent = cfg.title;
  host.querySelector(".legend").innerHTML = cfg.series.map((s) => {
    const off = bus.hidden.has(cfg.id + ":" + s.key);
    return `<button data-k="${esc(s.key)}" aria-pressed="${!off}"><i style="background:${s.color};${s.dash ? "opacity:.6" : ""}"></i>${esc(s.name)}</button>`;
  }).join("");
  host.querySelectorAll(".legend button").forEach((b) => b.onclick = () => {
    const k = cfg.id + ":" + b.dataset.k;
    bus.hidden.has(k) ? bus.hidden.delete(k) : bus.hidden.add(k);
    draw(id);
  });
  svg.setAttribute("aria-label", `${cfg.title}, ${cfg.unit}`);
  const sum = host.querySelector(".chart-sum");
  sum.hidden = !cfg.summary;
  sum.innerHTML = cfg.summary || "";
  const W = svg.clientWidth || host.clientWidth || 600, H = svg.clientHeight || 200;
  const [x0, x1] = cfg.x;
  const mks = (cfg.markers || []).filter((k) => k.t != null && k.t >= x0 && k.t <= x1).sort((a, b) => a.t - b.t);
  const m0 = margins(cfg);
  const X = (t) => m0.l + (t - x0) / (x1 - x0 || 1) * (W - m0.l - m0.r);
  const lane = laneLayout(mks, X, W, m0);
  const m = margins(cfg, lane.rows);
  const vis = cfg.series.filter((s) => !bus.hidden.has(cfg.id + ":" + s.key));
  let ymax = cfg.yMaxMin || 1;
  let any = false;
  for (const s of vis) s.v.forEach((v, i) => { if (v != null && s.t[i] >= x0 && s.t[i] <= x1) { any = true; if (v > ymax) ymax = v; } });
  for (const r of cfg.refs || []) if (r.y != null && r.y < ymax * 4) ymax = Math.max(ymax, r.y * 1.1);
  const step = niceStep(ymax / 4);
  ymax = Math.ceil(ymax / step) * step;
  const Y = (v) => H - m.b - v / ymax * (H - m.t - m.b);
  let g = `<defs><pattern id="hatch-${id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
    <line x1="0" y1="0" x2="0" y2="6" style="stroke:var(--line-2);stroke-width:2"/></pattern></defs>`;
  // phase bands
  for (const b of cfg.bands || []) {
    if (b.kind === "recovered") continue;
    const a = Math.max(m.l, X(b.from)), z = Math.min(W - m.r, X(b.to == null ? x1 : b.to));
    if (z <= a) continue;
    g += `<rect class="band-${b.kind}" x="${a}" y="${m.t}" width="${z - a}" height="${H - m.t - m.b}"/>`;
    if (z - a > b.label.length * 5.5 + 8) g += `<text class="band-label" x="${a + 4}" y="${m.t - 4}">${esc(b.label)}</text>`;
  }
  // gaps (hatched, with the reason)
  for (const gp of cfg.gaps || []) {
    if (gp.series && !gp.series.some((k) => vis.some((s) => s.key === k))) continue;
    const a = Math.max(m.l, X(gp.from)), z = Math.min(W - m.r, X(gp.to));
    if (z - a < 2) continue;
    g += `<rect x="${a}" y="${m.t}" width="${z - a}" height="${H - m.t - m.b}" style="fill:url(#hatch-${id});opacity:.6"><title>${esc(gp.reason)}</title></rect>`;
    if (z - a > 120) g += `<text class="gap-label" x="${(a + z) / 2}" y="${(m.t + H - m.b) / 2}" text-anchor="middle">${esc(gp.reason)}</text>`;
  }
  // grid + axes
  for (let v = 0; v <= ymax + 1e-9; v += step) {
    g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${esc(cfg.fmt(v))}</text>`;
  }
  g += `<text class="axis-title" transform="translate(14 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">${esc(cfg.unit)}</text>`;
  const span = x1 - x0, tstep = niceStep(span / 7);
  for (let t = Math.ceil(x0 / tstep) * tstep; t <= x1; t += tstep) {
    g += `<text x="${X(t)}" y="${H - 6}" text-anchor="middle">${t === 0 ? "T0" : (t > 0 ? "+" : "") + t + " s"}</text>`;
  }
  g += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  // reference lines: baseline + abort limits (limits start where they arm)
  for (const r of cfg.refs || []) {
    if (r.y == null || r.y > ymax) continue;
    const a = Math.max(m.l, X(r.from ?? x0));
    g += `<line class="${r.kind}" x1="${a}" x2="${W - m.r}" y1="${Y(r.y)}" y2="${Y(r.y)}"/>
      <text class="${r.kind}-label" x="${W - m.r - 4}" y="${Y(r.y) - 4}" text-anchor="end">${esc(r.label)}</text>`;
  }
  // series
  for (const s of vis) {
    const pts = s.t.map((t, i) => [t, s.v[i]]).filter(([t]) => t >= x0 && t <= x1);
    if (s.type === "bars") {
      const bw = Math.max(1.5, (W - m.l - m.r) / (x1 - x0) * 0.8);
      for (const [t, v] of pts) if (v) g += `<rect x="${X(t) - bw / 2}" y="${Y(v)}" width="${bw}" height="${Y(0) - Y(v)}" style="fill:${s.color}"/>`;
      continue;
    }
    let d = "", pen = false, area = "", seg = [];
    const flush = () => { if (s.type === "area" && seg.length) area += `M${seg[0][0]},${Y(0)}L${seg.map((p) => p.join(",")).join("L")}L${seg[seg.length - 1][0]},${Y(0)}Z`; seg = []; };
    for (const [t, v] of pts) {
      if (v == null) { pen = false; flush(); continue; }
      d += `${pen ? "L" : "M"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`;
      seg.push([X(t).toFixed(1), Y(v).toFixed(1)]);
      pen = true;
    }
    flush();
    if (area) g += `<path d="${area}" style="fill:${s.color};fill-opacity:.18"/>`;
    g += `<path d="${d}" fill="none" style="stroke:${s.color};stroke-width:2;${s.dash ? "stroke-dasharray:6 4" : ""}" stroke-linejoin="round"/>`;
  }
  // peak + recovery bracket + callouts
  if (cfg.peak && cfg.peak.v != null && cfg.peak.t >= x0 && cfg.peak.t <= x1 && !bus.hidden.has(cfg.id + ":" + cfg.peak.key)) {
    const px = X(cfg.peak.t), py = Y(cfg.peak.v), right = px > W - 200;
    g += `<circle cx="${px}" cy="${py}" r="4" style="fill:var(--text)"/><text class="peak" x="${right ? px - 8 : px + 8}" y="${Math.max(m.t + 12, py - 6)}" text-anchor="${right ? "end" : "start"}">${esc(cfg.peak.label)}</text>`;
  }
  if (cfg.bracket && cfg.bracket.to != null && cfg.bracket.to <= x1) {
    const a = X(cfg.bracket.from), z = X(cfg.bracket.to), y = m.t - 14;
    g += `<path class="bracket" d="M${a},${y + 8}V${y}H${z}V${y + 8}"/><text class="bracket-label" x="${(a + z) / 2}" y="${y - 4}" text-anchor="middle">${esc(cfg.bracket.label)}</text>`;
  }
  for (const c of cfg.callouts || []) {
    if (c.t < x0 || c.t > x1) continue;
    g += `<text class="callout" x="${Math.min(X(c.t) + 6, W - m.r - 280)}" y="${m.t + 58}">${esc(c.text)}</text>`;
  }
  // event markers: line through the plot, label in the lane above it
  for (const {mk, xx, row, right} of lane.items) {
    g += `<line class="mk${mk.key === "abort" ? " mk-abort" : ""}" x1="${xx}" x2="${xx}" y1="${row * LANE + 14}" y2="${H - m.b}"/>
      <text class="mk-label${mk.key === "abort" ? " mk-abort-label" : ""}" x="${right ? xx - 4 : xx + 4}" y="${row * LANE + 11}" text-anchor="${right ? "end" : "start"}">${esc(mk.label)}</text>`;
  }
  if (cfg.now != null && cfg.now >= x0 && cfg.now <= x1) g += `<line class="now" x1="${X(cfg.now)}" x2="${X(cfg.now)}" y1="${m.t}" y2="${H - m.b}"/>`;
  if (bus.brush) {
    const a = Math.max(m.l, X(bus.brush[0])), z = Math.min(W - m.r, X(bus.brush[1]));
    if (z > a) g += `<rect class="brush" x="${a}" y="${m.t}" width="${z - a}" height="${H - m.t - m.b}"/>`;
  }
  if (bus.flashT != null && bus.flashT >= x0 && bus.flashT <= x1) g += `<line class="flash" x1="${X(bus.flashT)}" x2="${X(bus.flashT)}" y1="${m.t}" y2="${H - m.b}"/>`;
  if (bus.hoverT != null && bus.hoverT >= x0 && bus.hoverT <= x1) g += `<line class="cursor" x1="${X(bus.hoverT)}" x2="${X(bus.hoverT)}" y1="${m.t}" y2="${H - m.b}"/>`;
  if (!any) g += `<text class="empty" x="${(m.l + W - m.r) / 2}" y="${(m.t + H - m.b) / 2 + 20}" text-anchor="middle">${esc(cfg.empty || "No data in this range")}</text>`;
  svg.innerHTML = g;
}

/** Pod swimlane: one row per pod or service, coloured blocks per state, restart marks. Same x axis as the charts. */
export function swimlane(host, rows, x, opts = {}) {
  const id = opts.id || "swim";
  charts.delete(id);
  if (!rows || !rows.length) { host.innerHTML = `<p class="muted">${esc(opts.empty || "No pod data recorded for this run.")}</p>`; return; }
  // same plot area as the charts (left margin 64, right 16) so the time axes line up; labels sit above each bar
  const rowH = 36, lw = 64, W = host.clientWidth || 800, H = rows.length * rowH + 8;
  const X = (t) => lw + (t - x[0]) / (x[1] - x[0] || 1) * (W - lw - 16);
  const col = {Ready: "var(--ok)", NotReady: "var(--warn)", Terminating: "var(--fault)", Pending: "var(--faint)",
               CrashLoopBackOff: "var(--fault)", Running: "var(--warn)"};
  let g = `<defs><pattern id="sw-h" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="5" style="stroke:var(--bg);stroke-width:2"/></pattern></defs>`;
  rows.forEach((r, i) => {
    const y = i * rowH + 4;
    g += `<text x="${lw}" y="${y + 10}" style="font:12px var(--mono);fill:var(--muted)">${esc(r.row)}</text>`;
    for (const s of r.segments) {
      const a = Math.max(lw, X(s.from)), z = Math.min(W - 16, X(s.to == null ? x[1] : s.to));
      if (z <= a) continue;
      g += `<rect x="${a}" y="${y + 14}" width="${Math.max(1.5, z - a)}" height="16" rx="2" style="fill:${col[s.state] || "var(--faint)"}"><title>${esc(r.row)}: ${esc(s.state)} ${tsec(s.from)} to ${tsec(s.to)}</title></rect>`;
      if (s.state === "Pending" || s.state === "CrashLoopBackOff") g += `<rect x="${a}" y="${y + 14}" width="${Math.max(1.5, z - a)}" height="16" style="fill:url(#sw-h)"/>`;
      if (z - a > 90) g += `<text x="${a + 6}" y="${y + 26}" style="font:11px var(--body);fill:var(--bg)">${esc(s.state)}</text>`;
    }
    for (const mk of r.marks || []) {
      if (mk.t == null || mk.t < x[0] || mk.t > x[1]) continue;
      g += `<text x="${X(mk.t)}" y="${y + 27}" text-anchor="middle" style="font:700 15px var(--body);fill:var(--text)">${mk.kind === "restart" ? "↻" : "+"}<title>${esc(mk.label)} at ${tsec(mk.t)}</title></text>`;
    }
  });
  if (opts.cursor != null) g += `<line x1="${X(opts.cursor)}" x2="${X(opts.cursor)}" y1="0" y2="${H - 20}" style="stroke:var(--a1);stroke-width:2"/>`;
  const legend = ["Ready", "NotReady", "Terminating", "Pending", "CrashLoopBackOff"].map((k) =>
    `<span><i style="background:${col[k]};width:12px;height:12px;display:inline-block;border-radius:2px"></i> ${k === "NotReady" ? "not ready" : k}</span>`).join(" ");
  host.innerHTML = `<svg style="width:100%;height:${H}px" role="img" aria-label="Pod readiness over time">${g}</svg>
    <div class="legend muted">${legend}<span>↻ restart</span></div>`;
}
