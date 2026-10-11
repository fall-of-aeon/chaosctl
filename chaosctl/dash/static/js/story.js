// The six acts of one run. Every sentence and number comes from /api/run/<id>/*; nothing is invented here.
import {$, esc, DASH, nice, plain, fix1, secs, pct, tsec, clock, pill, sourceBadge} from "./util.js";
import {chart, swimlane} from "./chart.js";

// numbers inside a narrator sentence are set in mono and the act's colour
export function narrate(id, text, pending) {
  const el = $(id);
  el.innerHTML = esc(text || "").replace(/(?<![\w-]|Agent |exp-|W)(\d+(?:[.,]\d+)*)/g, "<b>$1</b>");
  el.closest(".act").classList.toggle("pending", !!pending);
}

// ------------------------------------------------------------------ Act 0
export function renderBaseline(s) {
  const b = s.baseline || {};
  const foot = b.duration_s ? `measured over ${b.duration_s} s at ${b.rate ?? DASH} orders/s` : "no baseline recorded";
  const stat = (label, v, unit) => `<div class="stat"><div class="label">${label}</div><div class="value">${v}<span class="unit">${unit}</span></div><div class="foot">${foot}</div></div>`;
  $("baseline").innerHTML = stat("W1 sync success", b.w1_success == null ? DASH : Math.round(b.w1_success * 100), "%")
    + stat("W1 p95 latency", secs(b.w1_p95_ms), "s") + stat("W2 accepted", fix1(b.w2_rate), "/ s")
    + stat("Queue waiting", b.wait_mean == null ? DASH : Math.round(b.wait_mean), "jobs");
}

// ------------------------------------------------------------------ Act 1
export function renderAgent1(a, s) {
  const el = $("agent1");
  if (!a || !a.available) { el.innerHTML = `<p class="muted">No Agent 1 decision recorded for this run.</p>`; return; }
  const c = a.chosen || {}, v = a.verdict || {}, lim = s.limits || {}, hc = s.hypothesis_check || {};
  const verdictPill = {confirmed: pill("ok", "", "confirmed"), refuted: pill("fault", "", "refuted"),
                       partly: pill("warn", "½", "partly")}[hc.verdict] || pill("muted", "…", "not judged yet");
  const cites = (c.cites || []).map((x) => `<button class="chip" data-ref="${esc(x)}">${esc(x)}</button>`).join(" ");
  const limits = [
    lim.w1_error_rate != null ? `W1 errors above ${pct(lim.w1_error_rate)}${lim.armed_after_s ? ` (armed after T+${lim.armed_after_s} s)` : ""}` : "W1 error-rate stop off for this tier (outage expected)",
    lim.w1_p95_ms != null ? `W1 p95 above ${secs(lim.w1_p95_ms)} s${lim.armed_after_s ? ` (after T+${lim.armed_after_s} s)` : ""}` : null,
    lim.queue_waiting != null ? `more than ${lim.queue_waiting} jobs waiting` : null,
    lim.max_total_s != null ? `experiment longer than ${lim.max_total_s} s` : null,
  ].filter(Boolean);
  const rows = a.candidates.map((r) => {
    const t = r.terms || {}, w = (x) => Math.max(0, Math.abs(x || 0)) * 100;
    const bar = r.terms ? `<span class="termbar" title="criticality ${t.criticality}, untested ${t.untested}, new info ${t.info_gain}, risk ${t.tier_risk}">
      <i style="width:${w(t.criticality)}%;background:var(--text)"></i><i style="width:${w(t.untested)}%;background:var(--muted)"></i>
      <i style="width:${w(t.info_gain)}%;background:var(--faint)"></i><i style="width:${w(t.tier_risk)}%;background:var(--fault)"></i></span>` : "";
    return `<tr class="${r.status !== "Allowed" ? "dim" : ""}"><td>${r.chosen ? pill("a1", "", "chosen") + " " : ""}${esc(r.alias || "")} ${esc(nice(r.fault))}</td>
      <td class="mono">${esc((r.target || "").replace("node:", ""))}</td><td class="mono">${esc(r.rung)}${r.variant ? `: ${esc(r.variant)}` : ""}</td>
      <td class="n">${r.tier ?? DASH}</td><td class="n">${r.score == null ? DASH : r.score.toFixed(2)}</td><td>${bar}</td><td>${esc(r.status)}</td></tr>`;
  }).join("");
  el.innerHTML = `<div class="chart-head"><h2>Agent 1 decision</h2>${sourceBadge(a.source)}</div>
    <dl class="rows" style="margin-top:16px">
      <dt>Chose</dt><dd class="big"><b>${esc(s.title)}</b> on <b>${esc(s.target)}</b>, variant <span class="mono">${esc(c.rung)}</span>${a.variant ? ` (${esc(a.variant)})` : ""}
        <div class="muted" style="font-size:.8125rem">Catalog experiment <b>${esc(a.alias || "-")}</b> · this run's experiment number <span class="mono">${esc(s.exp_id)}</span></div></dd>
      <dt>Why</dt><dd>${esc(plain(c.reason))}<div style="margin-top:6px">${cites}</div></dd>
      <dt>Predicted</dt><dd>${esc(plain(c.hypothesis))} ${pill("a1", "?", "hypothesis")} ${verdictPill}
        ${(hc.claims || []).length ? `<ul class="muted" style="margin:6px 0 0;padding-left:1.1rem;font-size:.8125rem">${hc.claims.map((x) =>
          `<li>${x.held ? "Pass:" : "Fail:"} ${esc(x.claim)}${x.hedged ? " (hedged: 'may', not counted)" : ""}</li>`).join("")}</ul>` : ""}</dd>
      <dt>Safety limits</dt><dd>Stop automatically if: <ul style="margin:4px 0 0;padding-left:1.1rem">${limits.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
        <span class="muted" style="font-size:.8125rem">Blast radius (amber on the map): ${(v.blast_radius || []).map((x) => `<button class="chip" data-ref="${esc(x)}">${esc(x)}</button>`).join(" ") || "none"}</span></dd>
      <dt>Guard verdict</dt><dd>${v.verdict === "approve" ? pill("ok", "", "approved") : pill("fault", "", v.verdict || "-")} ${esc(a.tier_rule)}</dd>
    </dl>
    <details style="margin-top:16px"><summary><h3>Candidates considered (${a.candidates.length})</h3><span class="muted secondary">Score = 0.4 criticality + 0.3 untested + 0.2 new info - 0.1 risk.
      Bar: <span style="color:var(--text)">criticality</span>, <span>untested</span>, <span style="color:var(--faint)">new info</span>, <span style="color:var(--fault)">risk (subtracted)</span>.</span></summary>
      <div class="scroll"><table><thead><tr><th>Experiment</th><th>Target</th><th>Variant</th><th class="n">Tier</th><th class="n">Score</th><th>Score terms</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
}

// ------------------------------------------------------------------ Act 2
export function renderAutostop(s, cursor) {
  const a = s.abort;
  if (a && (cursor == null || cursor >= a.rel_s)) {
    $("autostop").innerHTML = `<div class="banner fault"><b>AUTO-STOP at ${tsec(a.rel_s)}:</b> ${esc(a.text)}. The safety controller stopped the load, removed the fault and checked every service came back.</div>`;
  } else if (s.status && s.status !== "running" && (cursor == null || cursor >= (s.markers.find((m) => m.key === "end")?.rel_s ?? 0))) {
    $("autostop").innerHTML = `<div class="banner">No safety limit was crossed; the experiment ran to completion.</div>`;
  } else $("autostop").innerHTML = "";
}

/** chart configs shared by Story, Live ops and Compare. data: {t, w1_p50, w1_p95, w1_ok, w1_err, w2_ok, q_wait, q_active}; cut at cursor. */
export function impactCharts(host, prefix, data, s, x, cursor, extra = {}) {
  if (!host.dataset.built) {
    host.dataset.built = "1";
    host.innerHTML = `<div id="${prefix}-lat"></div><div id="${prefix}-tp"></div><div id="${prefix}-q"></div>`;
  }
  const cut = (col) => data.t.map((t, i) => (cursor != null && t > cursor ? null : (data[col] || [])[i] ?? null));
  const lim = (s && s.limits) || {}, b = (s && s.baseline) || {};
  const bands = s ? s.phases : [], markers = s ? s.markers.filter((m) => cursor == null || m.rel_s <= cursor).map((m) => ({...m, t: m.rel_s})) : [];
  const rt = s && s.recovery_time_s, rec = s && s.milestones?.find((m) => m.key === "t_slis_recovered")?.rel_s;
  const empty = extra.empty || "No data in this range";
  const gaps = (data.gaps || []);
  const peak = s && s.peak_p95 && (cursor == null || s.peak_p95.rel_s <= cursor)
    ? {key: "p95", t: s.peak_p95.rel_s, v: s.peak_p95.ms / 1000, label: `peak ${secs(s.peak_p95.ms)} s${s.peak_p95.ratio ? ` (${s.peak_p95.ratio}x baseline)` : ""}`} : null;
  const bracket = rt != null && rec != null && (cursor == null || cursor >= rec) ? {from: 0, to: rec, label: `recovery ${rt} s`} : null;
  const lat = {id: prefix + "-lat", title: "W1 latency: how slow did it get for customers?", unit: "seconds", x, fmt: (v) => fix1(v, v < 10 ? 1 : 0),
    series: [{key: "p95", name: "W1 p95", color: "var(--text)", t: data.t, v: cut("w1_p95").map((v) => v == null ? null : v / 1000)},
             {key: "p50", name: "W1 p50", color: "var(--muted)", dash: true, t: data.t, v: cut("w1_p50").map((v) => v == null ? null : v / 1000)},
             ...(extra.lat || [])],
    refs: [b.w1_p95_ms ? {y: b.w1_p95_ms / 1000, kind: "base", label: `baseline p95 ${secs(b.w1_p95_ms)} s`} : null,
           lim.w1_p95_ms ? {y: lim.w1_p95_ms / 1000, kind: "limit", label: `abort: ${secs(lim.w1_p95_ms)} s`, from: lim.armed_after_s || 0} : null].filter(Boolean),
    bands, markers, peak, bracket, gaps, empty, yMaxMin: 6, now: extra.now, wallT0: data.wall_t0};
  const silent = silentFailure(data, cursor);
  const tp = {id: prefix + "-tp", title: "Throughput and errors: did work stop? were customers still told OK?", unit: "requests / s", x, fmt: (v) => fix1(v),
    series: [{key: "w1", name: "W1 completed / s", color: "var(--text)", t: data.t, v: cut("w1_ok")},
             {key: "w2", name: "W2 accepted / s", color: "var(--ok)", t: data.t, v: cut("w2_ok")},
             {key: "err", name: "W1 errors / s", color: "var(--fault)", type: "area", t: data.t, v: cut("w1_err")}, ...(extra.tp || [])],
    bands, markers, gaps: gaps.filter((g) => !g.series), empty, yMaxMin: 2, now: extra.now, wallT0: data.wall_t0,
    callouts: silent != null ? [{t: silent, text: "Silent failure: async customers still told OK"}] : []};
  const q = {id: prefix + "-q", title: "Redis queue: did a backlog build up, and did it drain?", unit: "jobs", x, fmt: (v) => String(Math.round(v)),
    series: [{key: "wait", name: "jobs waiting", color: "var(--warn)", type: "area", t: data.t, v: cut("q_wait")},
             {key: "active", name: "jobs active", color: "var(--text)", t: data.t, v: cut("q_active")}, ...(extra.q || [])],
    refs: lim.queue_waiting ? [{y: lim.queue_waiting, kind: "limit", label: `abort: ${lim.queue_waiting} jobs`, from: 0}] : [],
    bands, markers, gaps, empty, yMaxMin: 10, now: extra.now, wallT0: data.wall_t0};
  Object.assign(lat, {summary: latSummary(data, cursor, b, peak && s.peak_p95, rec)});
  const stopT = (s && s.markers.find((m) => m.key === "abort")?.rel_s) ?? null;   // the safety controller stops the load
  Object.assign(tp, {summary: tpSummary(data, cursor, stopT)});
  Object.assign(q, {summary: qSummary(data, cursor, lim)});
  chart($(prefix + "-lat"), lat);
  chart($(prefix + "-tp"), tp);
  chart($(prefix + "-q"), q);
}

// ---- one-line takeaways above each chart, computed from the same series (after injection, up to the cursor)
const after = (d, cursor) => d.t.map((t, i) => [t, i]).filter(([t]) => t >= 0 && (cursor == null || t <= cursor));
const b_ = (x) => `<b>${x}</b>`;

function latSummary(d, cursor, base, pk, rec) {
  if (!after(d, cursor).length) return "";
  const bs = base.w1_p95_ms ? base.w1_p95_ms / 1000 : null;
  if (!pk) return bs ? `Latency stayed near the ${bs.toFixed(1)} s baseline.` : "";
  const ratio = pk.ratio ? ` (${pk.ratio}× the ${bs ? bs.toFixed(1) + " s " : ""}baseline)` : "";
  const back = rec != null && (cursor == null || cursor >= rec) ? ` Back to normal at ${b_("T+" + Math.round(rec) + " s")}.` : " Not back to normal in this window.";
  return `Slowest orders took ${b_((pk.ms / 1000).toFixed(1) + " s")}${ratio} at T+${Math.round(pk.rel_s)} s.${pk.ratio && pk.ratio < 1.5 ? " Customers barely noticed." : back}`;
}

function tpSummary(d, cursor, stopT) {
  const pts = after(d, cursor).filter(([t]) => stopT == null || t <= stopT);   // after an auto-stop no orders are sent
  if (!pts.length) return "";
  let errs = 0, errSecs = 0, stall = 0, w2while = 0;
  for (const [, i] of pts) {
    const e = (d.w1_err || [])[i] || 0, ok = (d.w1_ok || [])[i], w2 = (d.w2_ok || [])[i];
    errs += e; if (e > 0) errSecs++;
    if (ok === 0) { stall++; if (w2 > 0) w2while++; }
  }
  const parts = [];
  parts.push(errSecs ? `About ${b_(Math.round(errs))} W1 orders failed, spread over ${errSecs} s.` : "No W1 order failed.");
  if (stall) parts.push(`W1 completed nothing for ${b_(stall + " s")}${stopT != null ? ` before the auto-stop at T+${Math.round(stopT)} s` : ""}.`);
  if (w2while >= 5) parts.push(`W2 customers were still told ${b_("OK")} during ${w2while} s of that: possible silent failure.`);
  return parts.join(" ");
}

function qSummary(d, cursor, lim) {
  const pts = after(d, cursor);
  if (!pts.length) return "";
  let mx = 0, tmx = null, drained = null;
  for (const [t, i] of pts) { const w = (d.q_wait || [])[i]; if (w != null && w > mx) { mx = w; tmx = t; } }
  if (!mx) return "The queue never backed up (0 jobs waiting).";
  for (const [t, i] of pts) if (t > tmx && ((d.q_wait || [])[i] ?? 1) === 0) { drained = t; break; }
  const limit = lim.queue_waiting ? ` (abort limit ${lim.queue_waiting})` : "";
  return `Backlog peaked at ${b_(Math.round(mx) + " jobs")}${limit} at T+${Math.round(tmx)} s; ${drained != null ? `drained by ${b_("T+" + Math.round(drained) + " s")}` : "not drained in this window"}.`;
}

/** first second of a 5 s stretch where W2 is still accepted while W1 completes nothing */
function silentFailure(d, cursor) {
  let run = 0;
  for (let i = 0; i < d.t.length; i++) {
    if (cursor != null && d.t[i] > cursor) break;
    if (d.t[i] >= 0 && (d.w2_ok[i] || 0) >= 0.4 && d.w1_ok[i] === 0) { if (++run >= 5) return d.t[i - 4]; } else run = 0;
  }
  return null;
}

export function renderTimeline(s, cursor) {
  // numbered dots on a proportional track; the numbered list below carries the labels (milestones often cluster)
  const ms = s.milestones;
  const known = ms.filter((m) => m.rel_s != null);
  const lo = Math.min(-10, ...known.map((m) => m.rel_s)), hi = Math.max(30, ...known.map((m) => m.rel_s));
  const X = (t) => 2 + (t - lo) / (hi - lo) * 96;   // percent of the track
  const rt = s.recovery_time_s, rec = ms.find((m) => m.key === "t_slis_recovered")?.rel_s;
  const showRec = rt != null && rec != null && (cursor == null || cursor >= rec);
  const seen = (m) => cursor == null || m.rel_s <= cursor;
  const color = (m) => m.key === "t_inject" ? "var(--fault)" : m.key === "t_slis_recovered" ? "var(--ok)" : "var(--text)";
  // dots that land on the same spot are stacked upwards
  const used = [];
  const dots = ms.map((m, i) => {
    if (m.rel_s == null) return "";
    const x = X(m.rel_s);
    let lift = 0; while (used.some(([ux, ul]) => Math.abs(ux - x) < 2.2 && ul === lift)) lift++;
    used.push([x, lift]);
    return `<span class="tl-dot" style="left:${x}%;bottom:${18 + lift * 26}px;border-color:${color(m)};opacity:${seen(m) ? 1 : .3}" title="${esc(m.label)} ${tsec(m.rel_s)}">${i + 1}</span>`;
  }).join("");
  $("timeline").innerHTML = `<div class="tl-grid">
      <div><div class="muted">Recovery time</div><div class="hero-num">${showRec ? rt + '<span class="unit"> s</span>' : DASH}</div>
        <div class="muted" style="font-size:.8125rem">${rt != null ? `from inject until W1 success and p95 were back within 10 % of baseline for ${s.recovery_hold_s ?? 30} s` : s.status === "running" ? "measuring" : "SLIs did not recover in the window"}</div></div>
      <div><div class="tl-track">
          ${showRec ? `<span class="tl-bracket" style="left:${X(0)}%;width:${X(rec) - X(0)}%"><b>recovery ${rt} s</b></span>` : ""}
          <span class="tl-line"></span>${dots}
          <span class="tl-axis" style="left:${X(0)}%">T0</span>${cursor != null ? `<span class="tl-now" style="left:${Math.min(98, X(cursor))}%"></span>` : ""}</div>
        <ol class="milestones">${ms.map((m, i) => `<li class="${m.rel_s != null && !seen(m) ? "pending" : ""}"><span class="tl-n" style="border-color:${color(m)}">${i + 1}</span>
          <span><span class="muted">${esc(m.label)}</span><b>${m.rel_s == null ? DASH : tsec(m.rel_s)}</b>${m.note ? `<span class="muted"> ${esc(m.note)}</span>` : ""}</span></li>`).join("")}</ol></div></div>
    ${s.gap_note ? `<div class="note"><b>Why so long?</b> ${esc(s.gap_note)} <button class="more" data-logwin="${ms.find((m) => m.key === "t_pod_ready")?.rel_s},${rec}">Show those log lines</button></div>` : ""}`;
}

export function renderFates(host, f, opts = {}) {
  if (!f || !f.total) { host.innerHTML = `<p class="muted">${opts.pending ? "Classified after recovery (the harness waits up to 180 s for stragglers)." : "No fate table recorded for this run."}</p>`; return; }
  const bar = f.rows.filter((r) => r.count).map((r) => `<i class="f-${r.fate}" style="width:${r.share * 100}%" title="${esc(r.label)}: ${r.count}">${r.count}</i>`).join("");
  const lostIds = f.rows.filter((r) => ["lost_silently", "lost_visible", "duplicated", "phantom_success"].includes(r.fate)).flatMap((r) => r.request_ids.map((x) => `${r.label}: ${x}`));
  host.innerHTML = `<p class="big" style="font-size:1.125rem">${f.total} in flight: ${f.rows.filter((r) => r.count).map((r) => `${r.count} ${esc(r.label)}`).join(", ")}</p>
    <div class="fatebar" role="img" aria-label="Fate of ${f.total} in-flight orders">${bar}</div>
    <table class="fate-table"><thead><tr><th>Outcome</th><th class="n">Count</th><th>What it means</th></tr></thead><tbody>${f.rows.map((r) =>
      `<tr class="${r.count ? "" : "zero"}${r.bad ? " bad" : ""}"><td>${esc(r.label)}</td><td class="n">${r.count}</td><td>${esc(r.meaning)}${!r.count && r.fate !== "completed_once" ? " (none: good news)" : ""}</td></tr>`).join("")}</tbody></table>
    ${lostIds.length ? `<details style="margin-top:8px"><summary><span class="muted">Request ids of lost, duplicated and phantom orders (${lostIds.length})</span></summary><div class="mono muted" style="font-size:.8125rem">${lostIds.map(esc).join("<br>")}</div></details>` : ""}`;
}

// ------------------------------------------------------------------ Act 3
export function renderAgent2(d) {
  const el = $("agent2");
  if (!d || !d.available) { el.innerHTML = `<p class="muted">Agent 2 has not diagnosed this run. Command: <span class="mono">uv run chaosctl diagnose --run &lt;run-id&gt;</span></p>`; return; }
  const g = d.grade;
  el.innerHTML = `<div class="chart-head"><h2>Agent 2: blind diagnosis</h2><span>${pill("a2", "", "Blind: not told what was injected")} ${sourceBadge(d.source)}</span></div>
    <div class="two" style="margin-top:16px">
      <ol class="stepper">${d.thoughts.map((t, i) => `<li><span class="ico" aria-hidden="true">${i + 1}</span>
        <div><span class="kind">${esc(t.step)}</span><div class="txt clip" id="th-${i}">${esc(plain(t.text))}</div>
        <button class="more" data-more="th-${i}">more</button> ${(t.cites || []).map((c) => `<button class="chip" data-evidence="${esc(c)}">${esc(c)}</button>`).join(" ")}</div></li>`).join("")}</ol>
      <div><div class="callout"><h3 style="color:var(--a2)">Root cause</h3><div class="rc">${esc(plain(d.root_cause))}</div>
          <ul style="margin:0;padding-left:1.1rem">${d.failure_chain.map((x) => `<li>${esc(plain(x))}</li>`).join("")}</ul>
          ${d.findings.length ? `<div style="margin-top:8px">Evidence: ${[...new Set(d.findings.flatMap((f) => f.evidence))].map((c) => `<button class="chip" data-evidence="${esc(c)}">${esc(c)}</button>`).join(" ")}</div>` : ""}</div>
        ${g ? `<p style="margin-top:16px;font-size:1.0625rem">${g.verdict === "NO_IMPACT" ? pill("muted", "–", "not graded") + " The fault had no measurable impact, so there was nothing to find."
          : (g.root_cause_correct ? pill("ok", "", "correct") : pill("fault", "", "missed")) + ` Matches injected fault: <b>${g.root_cause_correct ? "yes" : "no"}</b>`}
          <span class="muted">(checked offline against the plan: ${esc(nice(g.fault))} on ${esc(g.actual_target)})</span></p>` : ""}</div></div>`;
  el.querySelectorAll("[data-more]").forEach((b) => b.onclick = () => { const t = $(b.dataset.more); t.classList.toggle("clip"); b.textContent = t.classList.contains("clip") ? "more" : "less"; });
}

// ------------------------------------------------------------------ Act 4
export function renderFix(fs, s) {
  const el = $("fixsum");
  if (!fs || !fs.available) {
    el.innerHTML = `<p class="muted">No fix summary yet. It is written at the end of <span class="mono">chaosctl diagnose</span>; to write it now:</p>
      <pre class="cmd">${esc((fs && fs.command) || `uv run chaosctl fix-summary --run ${s.run_id} --exp ${s.exp_id}`)}</pre>`;
    return;
  }
  const llm = String(fs.source || "").startsWith("llm");
  const src = llm ? pill("ok", "", `LLM (${fs.source.split(":").pop()})`) : pill("warn", "!", `Rule-based fallback: ${fs.fallback_reason || "LLM unavailable"}`);
  const fr = Object.fromEntries((s.fates?.rows || []).map((r) => [r.fate, r.count]));
  const lost = (fr.lost_silently || 0) + (fr.lost_visible || 0) + (fr.duplicated || 0);
  const expect = lost ? `no lost or duplicated orders (this run: ${lost})` : s.recovery_time_s ? `recovery no slower than ${s.recovery_time_s} s, no lost orders` : "SLIs back within the window";
  el.innerHTML = `<div class="chart-head"><h2>Fix summary</h2>${src}</div>
    <dl class="rows" style="margin-top:16px">
      <dt>What broke</dt><dd class="big">${esc(fs.what_broke)}</dd>
      <dt>Why</dt><dd>${esc(fs.why)}</dd>
      <dt>Fix</dt><dd><ol class="fixes">${fs.fixes.map((f) => `<li><b>${esc(f.action)}</b> <button class="chip" title="playbook entry">${esc(f.playbook_id)}</button>
        <div class="muted" style="font-size:.8125rem">effort ${esc(f.effort || DASH)} · impact ${esc(f.impact || DASH)}</div><pre class="cmd">${esc(f.change || "")}</pre></li>`).join("")}</ol></dd>
      <dt>How to verify</dt><dd>${fs.fixes.map((f) => `<pre class="cmd">${esc(f.verify || DASH)}</pre>`).join("")}
        <div style="margin-top:8px">Then re-run <b>${esc(s.alias || s.fault)}</b> (<span class="mono">uv run chaosctl run --exp ${esc(s.fault)}</span>) and expect ${esc(expect)}.</div></dd>
      <dt>Rollback</dt><dd>${fs.fixes.map((f) => `<pre class="cmd">${esc(f.rollback || "no rollback step in the playbook")}</pre>`).join("")}</dd>
    </dl><p class="muted" style="margin-top:12px;font-size:.8125rem">Advisory only: every fix comes from playbook.yaml; nothing here changes the system.</p>`;
}

// ------------------------------------------------------------------ Act 5 + Report tab
export function gauge(score, verdict, size = 260) {
  const v = Math.max(0, Math.min(100, +score || 0)), r = 100, c = Math.PI * r;
  const col = v >= 80 ? "var(--ok)" : v >= 60 ? "var(--warn)" : "var(--fault)";
  return `<svg viewBox="-120 -110 240 130" style="width:${size}px;height:${size * 0.6}px" role="img" aria-label="Resilience score ${v} of 100, ${esc(verdict)}">
    <path class="g-track" d="M-100,0 A100,100 0 0 1 100,0" fill="none" stroke-width="18"/>
    <path class="g-val" d="M-100,0 A100,100 0 0 1 100,0" fill="none" stroke-width="18" style="stroke:${col}" stroke-dasharray="${c * v / 100} ${c}"/>
    <text x="0" y="-18" text-anchor="middle" style="font:700 52px var(--display);fill:var(--text)">${esc(score)}</text>
    <text x="0" y="12" text-anchor="middle" style="font:600 15px var(--body);fill:${col}">${esc(verdict)}</text></svg>`;
}
export function renderReportMini(rep, running) {
  const el = $("report-mini");
  if (!rep || !rep.available) {
    el.innerHTML = running ? `<p class="muted">The resilience score is calculated from the recorded measurements as soon as the run ends.</p>`
      : `<p>No resilience score for this run: it has no evidence bundle. A full report across runs is written by:</p>
      <pre class="cmd">${esc((rep && rep.command) || "uv run chaosctl report --all")}</pre>`;
    return;
  }
  if (!rep.overall) {
    const why = (rep.experiments || []).map((e) => e.score?.why).filter(Boolean)[0];
    el.innerHTML = `<p class="muted">Not scored${why ? `: ${esc(why)}` : ": no measurable component was recorded"}.</p>`;
    return;
  }
  const comps = rep.ranking?.components || [];
  const weakest = comps.filter((c) => c.mean != null).sort((a, b) => a.mean - b.mean)[0];
  const one = rep.experiments?.length === 1 ? rep.experiments[0].score?.components || {} : null;
  const rows = one ? `<table style="margin-top:8px"><thead><tr><th>Component</th><th class="n">Weight</th><th class="n">Score</th><th>Measured</th></tr></thead><tbody>${
    Object.entries(one).map(([k, c]) => `<tr><td>${esc(nice(k))}</td><td class="n">${Math.round(((rep.score_weights || {})[k] || 0) * 100)} %</td>
      <td class="n">${c.score ?? DASH}</td><td class="muted">${esc(c.why || "")}</td></tr>`).join("")}</tbody></table>` : "";
  el.innerHTML = `<div class="gauge">${gauge(rep.overall.score, rep.overall.verdict)}<div>
    <p class="big" style="font-size:1.25rem">Weakest component: <b>${esc(nice(weakest?.component || "-"))}</b>${weakest?.mean != null ? ` (${weakest.mean} / 100)` : ""}</p>
    <p class="muted">${rep.computed ? "Calculated from this run's recorded measurements" : `Formula-calculated score · ${rep.overall.experiments} experiments scored · report ${esc(rep.path)}`}</p>
    ${rep.score_formula ? `<p class="muted mono" style="font-size:.8125rem">${esc(rep.score_formula)}</p>` : ""}
    ${rep.computed ? "" : `<p style="margin-top:8px"><a href="#report">Full report</a></p>`}</div></div>${rows}`;
}

// ------------------------------------------------------------------ stretch goals
export function renderStretch(host, d) {
  if (!d) { host.innerHTML = `<p class="muted">Not available.</p>`; return; }
  const st = (s) => s === "Done" ? pill("ok", "", "Done") : s === "Partial" ? pill("warn", "", "Partial") : pill("muted", "", "Planned");
  host.innerHTML = d.goals.map((g) => {
    let body = "";
    if (g.id === "rerun_after_fix") {
      body = g.pair ? `<p>${esc(g.pair.alias || nice(g.pair.fault))}: <span class="mono">${esc(g.pair.a)}</span> vs <span class="mono">${esc(g.pair.b)}</span></p>
        <p class="muted">Fix applied between them: <b>${esc(g.pair.change)}</b></p><p><a href="#compare">Open the comparison</a></p>` : `<p class="muted">Needs two runs of one experiment with different recorded configurations.</p>`;
      body += `<p class="muted" style="margin-top:8px">Pending: ${esc(g.pending)}</p>`;
    } else if (g.id === "live_dashboard") {
      body = `<ul>${g.shows.map((x) => `<li>${esc(x)}</li>`).join("")}</ul><p><a href="#live">Open Live ops</a></p>`;
    } else {
      body = g.runs.length ? `<p>Runs with more than one fault: ${g.runs.map((r) => `<span class="mono">${esc(r)}</span>`).join(", ")}</p>`
        : `<p class="muted">No run has had two faults at once yet. Planned: ${esc(g.scenario)}; needs ${esc(g.needs)}.</p>
           <p class="muted" style="margin-top:8px">Ready in this UI: one fault band and label per fault, and the combined blast radius.</p>`;
    }
    return `<div class="goal">${st(g.status)}<h3>${esc(g.title)}</h3>${body}</div>`;
  }).join("");
}
