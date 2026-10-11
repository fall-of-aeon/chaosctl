// Unified error log: Kubernetes events, harness journal, safety controller, load-generator request errors and the
// pod log lines captured in the evidence. Clicking a row flashes that moment on every chart; a brushed chart range
// filters the log; an Agent 2 citation opens it filtered to that evidence.
import {esc, tsec, clock, getJSON, DASH} from "./util.js";
import {bus, flash} from "./chart.js";

const SEV = {error: ["✕", "error"], warn: ["!", "warn"], info: ["i", "info"]};

export class LogPanel {
  /** host: element; source: {url(params) -> string} for a finished run, or {rows() -> rows} for live */
  constructor(host, source, opts = {}) {
    this.host = host; this.src = source; this.opts = opts;
    this.f = {severity: new Set(), source: new Set(), component: new Set(), q: "", faultOnly: false, evidence: null};
    this.page = 0; this.open = new Set(); this.sel = null;
    this.render();
  }
  setSource(src) { this.src = src; this.page = 0; this.render(); }
  focusEvidence(ev) { this.f.evidence = ev; this.page = 0; this.render(); }

  async load() {
    const f = this.f, brush = bus.brush;
    const fault = this.opts.faultWindow && this.opts.faultWindow();
    const from = brush ? brush[0] : f.faultOnly && fault ? fault[0] : null;
    const to = brush ? brush[1] : f.faultOnly && fault ? fault[1] : (this.opts.cursor ? this.opts.cursor() : null);
    if (this.src.url) {
      const p = new URLSearchParams();
      if (f.severity.size) p.set("severity", [...f.severity].join(","));
      if (f.source.size) p.set("source", [...f.source].join(","));
      if (f.component.size) p.set("component", [...f.component].join(","));
      if (f.q) p.set("q", f.q);
      if (f.evidence) p.set("evidence", f.evidence);
      if (from != null) p.set("from", from);
      if (to != null) p.set("to", to);
      p.set("page", this.page); p.set("page_size", this.opts.pageSize || 100);
      return getJSON(this.src.url(p));
    }
    // live: filter client-side, same row shape
    let rows = this.src.rows();
    const counts = {severity: {}, source: {}, component: {}};
    for (const r of rows) for (const k in counts) counts[k][r[k]] = (counts[k][r[k]] || 0) + 1;
    if (f.severity.size) rows = rows.filter((r) => f.severity.has(r.severity));
    if (f.source.size) rows = rows.filter((r) => f.source.has(r.source));
    if (f.component.size) rows = rows.filter((r) => f.component.has(r.component));
    if (from != null) rows = rows.filter((r) => r.rel_s != null && r.rel_s >= from);
    if (to != null) rows = rows.filter((r) => r.rel_s == null || r.rel_s <= to);
    if (f.q) rows = rows.filter((r) => r.message.toLowerCase().includes(f.q.toLowerCase()));
    rows = rows.slice().reverse();
    const errs = this.src.rows().filter((r) => r.severity === "error");
    return {total: rows.length, rows: rows.slice(0, 200), counts, summary: summarize(errs)};
  }

  async render() {
    let d;
    try { d = await this.load(); } catch (e) { this.host.innerHTML = `<p class="muted">Log not available: ${esc(e.message)}</p>`; return; }
    const f = this.f;
    const btn = (kind, val, label) => `<button class="fbtn" data-k="${kind}" data-v="${esc(val)}" aria-pressed="${f[kind].has(val)}">${label} <span class="muted">${(d.counts[kind] || {})[val] || 0}</span></button>`;
    const sev = ["error", "warn", "info"].map((s) => btn("severity", s, `${SEV[s][0]} ${s === "error" ? "errors" : s === "warn" ? "warnings" : "info"}`)).join("");
    const src = Object.keys(d.counts.source || {}).sort().map((s) => btn("source", s, esc(s))).join("");
    const comp = Object.keys(d.counts.component || {}).sort().map((s) => btn("component", s, esc(s))).join("");
    const sum = d.summary || {};
    const scope = bus.brush ? `chart range ${tsec(bus.brush[0])} to ${tsec(bus.brush[1])}` : f.evidence ? `evidence ${f.evidence}` : "";
    this.host.innerHTML = `
      <div class="logsum">${spark(sum.errors_per_10s || [], this.opts.x)}
        <div><b>${sum.errors ?? 0} errors.</b> Most frequent:${(sum.top || []).length ? `<ol style="margin:4px 0 0;padding-left:1.2rem">${sum.top.map((t) =>
          `<li><span class="mono">${esc(t.pattern)}</span> <b>x${t.count}</b></li>`).join("")}</ol>` : " none"}</div></div>
      <div class="logbar">${sev}<span class="muted">|</span>${src}</div>
      <div class="logbar secondary">${comp}</div>
      <div class="logbar"><input type="search" placeholder="Search messages" value="${esc(f.q)}" aria-label="Search log">
        ${this.opts.faultWindow ? `<label><input type="checkbox" ${f.faultOnly ? "checked" : ""} data-fault> fault window only</label>` : ""}
        ${scope ? `<button class="fbtn" data-clear aria-pressed="true">${esc(scope)} (clear)</button>` : ""}
        ${this.opts.drawer ? `<button class="fbtn" data-drawer>Open full screen</button>` : ""}</div>
      <div class="scroll"><table class="logtable"><thead><tr><th>T+</th><th>Severity</th><th>Source</th><th>Component</th><th>Message</th><th>Id</th></tr></thead>
      <tbody>${d.rows.map((r) => `<tr data-id="${r.id}" data-t="${r.rel_s ?? ""}" class="${this.sel === r.id ? "sel" : ""}">
        <td class="n" title="${clock(r.ts)}">${r.rel_s == null ? DASH : tsec(r.rel_s)}</td>
        <td><span class="sev ${r.severity}">${SEV[r.severity][0]} ${SEV[r.severity][1]}</span></td>
        <td><span class="chip">${esc(r.source)}</span></td><td>${esc(r.component)}</td>
        <td class="msgcell${this.open.has(r.id) ? " open" : ""}"><span class="t">${esc(r.message)}</span></td>
        <td class="mono">${esc(r.request_id || r.evidence || "")}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">No rows match these filters.</td></tr>`}</tbody></table></div>
      <p class="muted">${d.total} rows, newest first${d.total > d.rows.length ? `, showing ${d.rows.length}` : ""}.
        ${d.total > (this.page + 1) * (this.opts.pageSize || 100) && this.src.url ? `<button class="fbtn" data-next>Next page</button>` : ""}
        ${this.page > 0 ? `<button class="fbtn" data-prev>Previous page</button>` : ""}</p>`;
    this.host.querySelectorAll(".fbtn[data-k]").forEach((b) => b.onclick = () => {
      const s = f[b.dataset.k]; s.has(b.dataset.v) ? s.delete(b.dataset.v) : s.add(b.dataset.v); this.page = 0; this.render();
    });
    const search = this.host.querySelector("input[type=search]");
    search.onchange = () => { f.q = search.value; this.page = 0; this.render(); };
    const fo = this.host.querySelector("[data-fault]");
    if (fo) fo.onchange = () => { f.faultOnly = fo.checked; this.render(); };
    const clr = this.host.querySelector("[data-clear]");
    if (clr) clr.onclick = () => { bus.brush = null; f.evidence = null; this.render(); this.opts.onClear && this.opts.onClear(); };
    const dr = this.host.querySelector("[data-drawer]");
    if (dr) dr.onclick = () => this.opts.drawer();
    const nx = this.host.querySelector("[data-next]"), pv = this.host.querySelector("[data-prev]");
    if (nx) nx.onclick = () => { this.page++; this.render(); };
    if (pv) pv.onclick = () => { this.page--; this.render(); };
    this.host.querySelectorAll("tbody tr[data-id]").forEach((tr) => tr.onclick = () => {
      const id = +tr.dataset.id;
      this.open.has(id) ? this.open.delete(id) : this.open.add(id);
      this.sel = id;
      if (tr.dataset.t !== "") flash(+tr.dataset.t);
      this.render();
    });
  }
}

function spark(buckets, x) {
  if (!buckets.length) return `<div class="muted">No errors with a timestamp.</div>`;
  const [x0, x1] = x ? x() : [Math.min(...buckets.map((b) => b[0])), Math.max(...buckets.map((b) => b[0])) + 10];
  const max = Math.max(...buckets.map((b) => b[1]));
  const W = 400, H = 40, X = (t) => (t - x0) / (x1 - x0 || 1) * W;
  const bars = buckets.filter(([t]) => t >= x0 && t <= x1).map(([t, n]) =>
    `<rect class="spark" x="${X(t)}" y="${H - n / max * (H - 4)}" width="${Math.max(2, X(t + 10) - X(t) - 1)}" height="${n / max * (H - 4)}"><title>${tsec(t)}: ${n} errors</title></rect>`).join("");
  return `<div><span class="muted">Errors per 10 s (same axis as the charts)</span><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}
    ${x0 < 0 && x1 > 0 ? `<line x1="${X(0)}" x2="${X(0)}" y1="0" y2="${H}" style="stroke:var(--fault)"/>` : ""}</svg></div>`;
}

function summarize(errs) {
  const b = {}, g = {};
  for (const r of errs) {
    if (r.rel_s != null) { const k = Math.floor(r.rel_s / 10) * 10; b[k] = (b[k] || 0) + 1; }
    const p = r.message.replace(/\d+(\.\d+)?/g, "#").slice(0, 120);
    g[p] = (g[p] || 0) + 1;
  }
  return {errors: errs.length, errors_per_10s: Object.entries(b).map(([k, v]) => [+k, v]).sort((a, c) => a[0] - c[0]),
          top: Object.entries(g).sort((a, c) => c[1] - a[1]).slice(0, 3).map(([pattern, count]) => ({pattern, count}))};
}
