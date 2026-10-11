// Report tab (score, ranking, findings, downloads) and Compare tab (two runs aligned on injection).
import {$, esc, DASH, nice, secs, getJSON, pill, tsec} from "./util.js";
import {chart} from "./chart.js";
import {gauge, renderFates} from "./story.js";

const WEIGHT = {availability: 30, self_recovery: 25, latency: 20, data_integrity: 15, detectability: 10};

export function renderReportFull(rep, runs) {
  const el = $("report-full");
  if (!rep || !rep.available) {
    el.innerHTML = `<div class="card"><h2>Resilience report</h2><p class="what">Formula-calculated scores, ranking and findings across experiments.</p>
      <p>No report exists yet. It is written from the recorded runs by:</p><pre class="cmd">${esc((rep && rep.command) || "uv run chaosctl report --all")}</pre>
      <p class="muted" style="margin-top:8px">It produces ${esc((rep && rep.produces) || "findings.md and findings.json")}.</p></div>`;
    return;
  }
  const comps = rep.ranking?.components || [], exps = rep.ranking?.experiments || [];
  const byRef = Object.fromEntries((runs || []).map((r) => [r.ref, r]));
  const detail = Object.fromEntries((rep.experiments || []).map((e) => [e.exp_ref, e]));
  const weights = rep.score_weights || Object.fromEntries(Object.entries(WEIGHT).map(([k, v]) => [k, v / 100]));
  const formula = rep.score_formula || "R = 0.30*A + 0.25*S + 0.20*L + 0.15*D + 0.10*M";
  el.innerHTML = `<div class="card"><div class="chart-head"><h2>Resilience score</h2>${pill("ok", "", "Calculated from recorded measurements")}</div>
      <p class="what">${esc(formula)}</p>
      <p class="muted">A availability, S self-recovery, L latency, D data integrity, M detectability. Each component is 0–100; unavailable components are excluded and the remaining weights are rescaled. The overall score is the mean of scored experiments.</p>
      <div class="gauge">${rep.overall ? gauge(rep.overall.score, rep.overall.verdict) : '<p class="muted">No measurable score available.</p>'}
        <div class="hbars">${comps.map((c) => `<div class="hbar" tabindex="0" data-comp="${esc(c.component)}"><span>${esc(nice(c.component))} <span class="muted">${Math.round((weights[c.component] ?? c.weight ?? 0) * 100)} %</span></span>
          <span class="rail2"><i style="width:${c.mean ?? 0}%"></i></span><span class="n mono">${c.mean ?? DASH}</span></div>`).join("")}</div></div>
      <p class="muted" style="margin-top:12px">Report ${esc(rep.path)} · <a href="/api/download?path=${encodeURIComponent(rep.md_path)}">Download findings.md</a> ·
        <a href="/api/download?path=${encodeURIComponent(rep.path)}">Download findings.json</a></p></div>
    <div class="card"><h2>Experiments, least resilient first</h2><p class="what">Score from the report; recovery and auto-stop from the run itself.</p>
      <table><thead><tr><th>#</th><th>Experiment</th><th>Target</th><th class="n">Score</th><th>Verdict</th><th class="n">Recovery</th><th>Auto-stop</th><th>Weakest</th><th></th></tr></thead>
      <tbody>${exps.map((e) => { const r = byRef[e.exp_ref] || {}; return `<tr><td class="n">${e.rank ?? DASH}</td><td>${esc(e.alias || "")} ${esc(e.title)}</td>
        <td class="mono">${esc(e.target || "-")}</td><td class="n">${e.score ?? DASH}</td><td>${esc(e.verdict || "not scored")}</td>
        <td class="n">${r.recovery_time_s != null ? r.recovery_time_s + " s" : DASH}</td><td>${r.auto_stopped ? pill("fault", "", "yes") : r.ref ? "no" : DASH}</td>
        <td>${esc(nice(e.weakest || "-"))}</td><td>${r.ref ? `<a href="#story" data-open-run="${esc(r.ref)}">Story</a>` : ""}</td></tr>`; }).join("")}</tbody></table></div>
    <div class="card"><h2>Findings</h2><p class="what">Weaknesses found, ranked by severity, each with its evidence and the playbook fix.</p>
      <div class="findings">${(rep.weaknesses || []).map((w) => { const sev = String(w.severity || "medium").toLowerCase();
        return `<div class="finding ${sev}">${pill(sev === "critical" || sev === "high" ? "fault" : sev === "medium" ? "warn" : "muted", sev === "low" ? "•" : "!", sev)}
          <p style="margin-top:8px;font-size:1.0625rem">${esc(w.title || w.finding || w.text || "")}</p>
          ${(w.evidence || w.experiments || []).length ? `<p class="muted" style="font-size:.8125rem">Evidence: ${(w.evidence || w.experiments).map((x) => `<span class="chip">${esc(x)}</span>`).join(" ")}</p>` : ""}
          ${(w.fixes || w.fix) ? `<p style="font-size:.875rem">Fix: ${[].concat(w.fixes || w.fix).map((f) => `<span class="chip">${esc(f.playbook_id || f)}</span>`).join(" ")}</p>` : ""}</div>`; }).join("") || `<p class="muted">The report lists no weaknesses.</p>`}</div></div>`;
  el.querySelectorAll(".hbar[data-comp]").forEach((row) => {
    const show = (ev) => {
      const c = row.dataset.comp;
      const lines = Object.values(detail).map((e) => { const f = (e.score?.components || {})[c];
        return `<div class="r"><span>${esc(e.exp_ref)}</span><span>Formula ${f?.score ?? DASH}</span></div>${f?.why ? `<div class="muted">${esc(f.why)}</div>` : ""}`; }).join("");
      const tip = $("tip"); tip.innerHTML = `<b>${esc(nice(c))}</b>${lines || '<div class="muted">No per-experiment detail in this report.</div>'}`; tip.hidden = false;
      const r = row.getBoundingClientRect(); tip.style.left = Math.min(r.left, innerWidth - 360) + "px"; tip.style.top = (r.bottom + 6) + "px";
    };
    row.onmouseenter = show; row.onfocus = show; row.onmouseleave = row.onblur = () => { $("tip").hidden = true; };
  });
}

// ------------------------------------------------------------------ Compare
export async function renderCompare(runs, a, b) {
  const opts = runs.filter((r) => r.has_evidence).map((r) => `<option value="${esc(r.ref)}">${esc(r.ref)} · ${esc(r.alias || "")} ${esc(r.title)}${r.config ? ` · ${r.config.worker_replicas} worker${r.config.worker_replicas > 1 ? "s" : ""}` : ""}</option>`).join("");
  for (const id of ["cmp-a", "cmp-b"]) if ($(id).dataset.n !== String(runs.length)) { $(id).innerHTML = opts; $(id).dataset.n = String(runs.length); }
  let d;
  try { d = await getJSON(`/api/compare?${new URLSearchParams(Object.fromEntries([["a", a], ["b", b]].filter(([, v]) => v)))}`); }
  catch (e) { $("cmp-verdict").textContent = "Pick two runs that have recorded evidence."; return; }
  $("cmp-a").value = d.a.summary.ref; $("cmp-b").value = d.b.summary.ref;
  $("cmp-verdict").textContent = d.verdict;
  const arrow = (row) => {
    const n = (v) => (typeof v === "boolean" ? (v ? 1 : 0) : v);
    const va = n(row.a), vb = n(row.b);
    // "never" (no recovery) is worse than any time
    if (va == null && vb == null || va === vb) return `<span class="muted">= same</span>`;
    if (va == null || vb == null) return (va == null) === (row.metric === "recovery time")
      ? `<span style="color:var(--ok)">\u25BC better</span>` : `<span style="color:var(--fault)">\u25B2 worse</span>`;
    const better = row.better === "lower" ? vb < va : vb > va;
    return better ? `<span style="color:var(--ok)">▼ better</span>` : `<span style="color:var(--fault)">▲ worse</span>`;
  };
  const fmt = (row, v) => v == null ? "never" : typeof v === "boolean" ? (v ? "yes" : "no") : row.unit === "ms" ? secs(v) + " s" : v + (row.unit ? " " + row.unit : "");
  $("cmp-deltas").innerHTML = `<table><thead><tr><th>Measure</th><th class="n">A (before)</th><th class="n">B (after)</th><th>Change</th></tr></thead><tbody>${d.deltas.map((r) =>
    `<tr><td>${esc(r.metric)}</td><td class="n">${fmt(r, r.a)}</td><td class="n">${fmt(r, r.b)}</td><td>${arrow(r)}</td></tr>`).join("")}</tbody></table>
    <p class="muted" style="margin-top:8px">A: ${esc(d.a.summary.ref)}${d.a.summary.config ? `, ${d.a.summary.config.worker_replicas} worker(s)` : ""} · B: ${esc(d.b.summary.ref)}${d.b.summary.config ? `, ${d.b.summary.config.worker_replicas} worker(s)` : ""}</p>`;
  const sa = d.a.series.cols, sb = d.b.series.cols;
  const x = [Math.min(sa.t[0], sb.t[0]), Math.max(sa.t[sa.t.length - 1], sb.t[sb.t.length - 1])];
  const host = $("cmp-charts");
  if (!host.dataset.built) { host.dataset.built = "1"; host.innerHTML = `<div id="cmp-lat"></div><div id="cmp-tp"></div><div id="cmp-q"></div>`; }
  const mk = (s) => s.summary.markers.filter((m) => ["inject", "slis_recovered", "abort"].includes(m.key)).map((m) => ({...m, t: m.rel_s}));
  const markers = [...mk(d.a).map((m) => ({...m, label: "A " + m.label})), ...mk(d.b).map((m) => ({...m, label: "B " + m.label}))];
  const base = {x, markers, empty: "No data recorded"};
  chart($("cmp-lat"), {...base, id: "cmp-lat", title: "W1 p95 latency", unit: "seconds", fmt: (v) => v.toFixed(v < 10 ? 1 : 0), yMaxMin: 6,
    series: [{key: "a", name: "A p95", color: "var(--muted)", dash: true, t: sa.t, v: sa.w1_p95.map((v) => v == null ? null : v / 1000)},
             {key: "b", name: "B p95", color: "var(--text)", t: sb.t, v: sb.w1_p95.map((v) => v == null ? null : v / 1000)}]});
  chart($("cmp-tp"), {...base, id: "cmp-tp", title: "W1 completed / s", unit: "requests / s", fmt: (v) => v.toFixed(1), yMaxMin: 2,
    series: [{key: "a", name: "A W1 completed", color: "var(--muted)", dash: true, t: sa.t, v: sa.w1_ok},
             {key: "b", name: "B W1 completed", color: "var(--text)", t: sb.t, v: sb.w1_ok},
             {key: "ea", name: "A errors", color: "var(--fault)", dash: true, t: sa.t, v: sa.w1_err},
             {key: "eb", name: "B errors", color: "var(--fault)", t: sb.t, v: sb.w1_err}]});
  chart($("cmp-q"), {...base, id: "cmp-q", title: "Redis queue: jobs waiting", unit: "jobs", fmt: (v) => String(Math.round(v)), yMaxMin: 10,
    gaps: [...d.a.series.gaps, ...d.b.series.gaps].filter((g) => !g.series),
    series: [{key: "a", name: "A waiting", color: "var(--warn)", dash: true, t: sa.t, v: sa.q_wait},
             {key: "b", name: "B waiting", color: "var(--warn)", t: sb.t, v: sb.q_wait}]});
  $("cmp-fates").innerHTML = `<div><h3>A · ${esc(d.a.summary.ref)}</h3><div id="cmp-fa"></div></div><div><h3>B · ${esc(d.b.summary.ref)}</h3><div id="cmp-fb"></div></div>`;
  renderFates($("cmp-fa"), d.a.summary.fates);
  renderFates($("cmp-fb"), d.b.summary.fates);
}
