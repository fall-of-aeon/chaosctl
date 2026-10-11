// The two agents, live: Agent 1 on a button with human approval, Agent 2 always watching.
// Every write goes to /api/agent1/*, /api/kill*, /api/traffic, /api/agent2/* (chaosctl/dash/control.py, watcher.py).
import {$, esc, DASH, nice, plain, pct, secs, tsec, clock, pill, sourceBadge} from "./util.js";

const A = {ctrl: null, a2: null, busy: false, err: null, open: new Set(), sig1: "", sig2: "", sig3: ""};

export async function post(path, body = {}) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Chaosctl": "1"}, body: JSON.stringify(body)});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

async function act(path, body) {
  if (A.busy) return;
  A.busy = true; A.err = null; paint(true);
  try { const d = await post(path, body); if (d.control || d.state) { /* the stream brings the new state within a second */ } }
  catch (e) { A.err = e.message; }
  finally { A.busy = false; paint(true); }
}

export function setupAgents() {
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest?.("[data-ag]");
    if (!b || b.disabled) return;
    const p = A.ctrl?.proposal;
    const what = b.dataset.ag;
    if (what === "plan") act("/api/agent1/plan");
    else if (what === "replan") act("/api/agent1/plan", {exclude: [...(p?.excluded || []), p?.item?.fault].filter(Boolean)});
    else if (what === "approve") act("/api/agent1/approve", {proposal_id: p?.proposal_id});
    else if (what === "cancel") act("/api/agent1/cancel");
    else if (what === "kill") act("/api/kill");
    else if (what === "kill-reset") act("/api/kill/reset");
    else if (what === "traffic") act("/api/traffic", {on: b.dataset.on === "1"});
    else if (what === "investigate") act("/api/agent2/investigate");
    else if (what === "a2pause") act("/api/agent2/pause", {paused: b.dataset.paused === "1"});
    else if (what === "toggle-step") { const k = b.dataset.key; A.open.has(k) ? A.open.delete(k) : A.open.add(k); paint(true); }
  });
}

/** called with every stream message (or poll) */
export function updateAgents(p, liveState, visible) {
  if (p.control) A.ctrl = p.control;
  if (p.agent2) A.a2 = p.agent2;
  A.state = liveState;
  A.visible = visible;
  paint(false);
}

function paint(force) {
  const host = $("agents");
  const show = A.visible && A.ctrl && A.ctrl.available;
  host.hidden = !show;
  $("kill-btn").hidden = !(A.ctrl && A.ctrl.available);
  if (A.ctrl) {
    $("kill-btn").disabled = A.busy || A.ctrl.kill?.status === "running";
    $("kill-btn").textContent = A.ctrl.kill?.status === "running" ? "Stopping…" : "Kill switch";
  }
  if (!show) return;
  // repaint only what changed, so open <details> and scroll positions survive the 1 s stream
  const s1 = JSON.stringify([A.ctrl, A.busy, A.err, A.state?.campaign, A.state?.experiment?.t_inject && Math.round(A.state.now - A.state.experiment.t_inject)]);
  if (force || s1 !== A.sig1) { A.sig1 = s1; $("agent1-live").innerHTML = agent1Html(); }
  // Agent 2: the status header changes every second (checked_at, streak); the investigation transcript only
  // when a step arrives. Repainting them separately keeps the transcript still while the user scrolls it.
  const panel = $("agent2-live");
  if (!panel.querySelector("[data-a2=head]")) { panel.innerHTML = `<div data-a2="head"></div><div data-a2="body"></div>`; A.sig2 = A.sig3 = ""; }
  const s2 = JSON.stringify([A.a2 && {...A.a2, current: null, recent: null, bundle_job: null}, A.busy]);
  if (force || s2 !== A.sig2) { A.sig2 = s2; panel.querySelector("[data-a2=head]").innerHTML = agent2Head(); }
  const s3 = JSON.stringify([A.a2?.available && [A.a2.current, A.a2.recent, A.a2.bundle_job], [...A.open]]);
  if (force || s3 !== A.sig3) { A.sig3 = s3; paintIncident(panel.querySelector("[data-a2=body]")); }
}

/** repaint the investigation, keeping scroll positions and focus; a stepper scrolled to the end follows new steps */
function paintIncident(body) {
  const incident = body.querySelector(".incident")?.dataset.incidentId;
  const scrolls = new Map([...body.querySelectorAll("[data-scroll-key]")].map((el) =>
    [el.dataset.scrollKey, {top: el.scrollTop, left: el.scrollLeft, end: el.scrollTop + el.clientHeight >= el.scrollHeight - 8}]));
  const focused = body.contains(document.activeElement) ? document.activeElement?.dataset.scrollKey : null;
  const recentOpen = body.querySelector("details.recent")?.open;
  body.innerHTML = agent2Body();
  if (incident && incident === body.querySelector(".incident")?.dataset.incidentId) {
    body.querySelectorAll("[data-scroll-key]").forEach((el) => {
      const pos = scrolls.get(el.dataset.scrollKey);
      if (pos) { el.scrollTop = pos.end && el.dataset.scrollKey === "steps" ? el.scrollHeight : pos.top; el.scrollLeft = pos.left; }
      if (focused === el.dataset.scrollKey) el.focus({preventScroll: true});
    });
  }
  const recent = body.querySelector("details.recent");
  if (recent && recentOpen != null) recent.open = recentOpen;
}

// ------------------------------------------------------------------ Agent 1
function agent1Html() {
  const c = A.ctrl, st = A.state, busy = A.busy;
  const err = A.err ? `<div class="banner fault small"><b>Refused:</b> ${esc(A.err)}</div>` : "";
  const msg = c.message ? `<p class="muted" style="margin-top:8px">${esc(c.message)}</p>` : "";
  const traffic = c.traffic.on
    ? `<button class="toggle" data-ag="traffic" data-on="0" ${busy ? "disabled" : ""}>Stop steady traffic</button> <span class="muted">${esc(c.traffic.rate)} req/s from the dashboard</span>`
    : `<button class="toggle" data-ag="traffic" data-on="1" ${busy || c.running ? "disabled" : ""}>Start steady traffic</button> <span class="muted">so Agent 2 sees what customers see between runs</span>`;
  const blockers = (c.blockers || []).filter((b) => !b.includes("is still going"));
  const killRow = c.kill_file ? `<div class="banner fault small"><b>Kill switch tripped.</b> ${c.kill?.status === "running" ? "Deleting chaos and waiting for every service to be Ready…" :
    c.kill?.result ? `Chaos deleted (${esc((c.kill.result.deleted || []).length)}), all Ready after ${esc(c.kill.result.all_ready_s)} s.` : ""}
    <button class="toggle" data-ag="kill-reset" ${busy || c.running || c.kill?.status === "running" ? "disabled" : ""}>Reset kill switch</button></div>` : "";
  let body = "";
  if (c.state === "planning") {
    body = `<div class="a-wait"><span class="spin" aria-hidden="true"></span><div><b>Agent 1 is choosing.</b>
      <div class="muted">Scoring every catalog fault against the live dependency map, asking Mistral to pick and explain, then the policy guard sets the limits. Nothing is injected yet.</div></div></div>`;
  } else if (c.state === "proposed" && c.proposal) {
    body = proposalHtml(c.proposal, busy, blockers);
  } else if (c.running || (c.run && ["running"].includes(c.state))) {
    body = runHtml(c, st);
  } else {
    const last = c.run ? `<p style="margin-top:12px">Last run <span class="mono">${esc(c.run.run_id)}</span>: ${esc(nice(c.run.fault))} on ${esc(c.run.target.replace("node:", ""))} ·
      <b>${esc(c.run.result?.status || (c.run.returncode === 0 ? "finished" : "failed"))}</b>${c.run.result?.skip_reason ? ` (${esc(c.run.result.skip_reason)})` : ""}${c.run.result?.abort_reason ? ` · auto-stop: ${esc(c.run.result.abort_reason)}` : ""}
      ${c.run.result?.exp_id ? ` · <a href="?run=${esc(c.run.run_id)}/${esc(c.run.result.exp_id)}#story" data-open-run="${esc(c.run.run_id)}/${esc(c.run.result.exp_id)}">open the story</a>` : ""}</p>` : "";
    body = `<p>Agent 1 reads the live dependency map and the fault catalog, picks <b>one</b> experiment and explains why. You approve it before anything is injected.</p>
      <div class="a-actions"><button class="primary a1" data-ag="plan" ${busy || blockers.length ? "disabled" : ""}>Ask Agent 1 to pick a fault</button></div>
      ${blockers.length ? `<ul class="blockers">${blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}${last}`;
  }
  return `<div class="chart-head"><h2><span class="eyebrow a1">Agent 1</span> Injection</h2>${c.running ? pill("fault", "●", "run in progress") : c.state === "proposed" ? pill("a1", "?", "waiting for your approval") : pill("muted", "", "on demand")}</div>
    ${err}${killRow}<div class="a-body">${body}</div>${msg}<div class="a-traffic">${traffic}</div>`;
}

function proposalHtml(p, busy, blockers) {
  const it = p.item, v = p.verdict || {}, ab = v.abort || {};
  const limits = [ab.error_rate_abort === false ? "W1 error-rate stop off (outage expected for this tier)" : ab.w1_error_rate != null ? `W1 errors above ${pct(ab.w1_error_rate)}` : null,
    ab.w1_p95_ms != null ? `W1 p95 above ${secs(ab.w1_p95_ms)} s` : null, ab.queue_waiting != null ? `more than ${ab.queue_waiting} jobs waiting` : null,
    ab.max_total_s != null ? `longer than ${ab.max_total_s} s` : null].filter(Boolean);
  const src = String(p.source || "").startsWith("llm") ? pill("ok", "", `Mistral (${String(p.source).split(":").pop()})`) : pill("warn", "!", "Deterministic fallback: the LLM did not choose this");
  return `<div class="proposal">
    <p class="big"><b>${esc(nice(it.fault))}</b> on <b>${esc(it.target.replace("node:", ""))}</b> <span class="mono muted">${esc(it.rung)}</span> ${src}</p>
    <dl class="rows">
      <dt>Why</dt><dd>${esc(plain(it.reason))} ${(it.cites || []).map((x) => `<button class="chip" data-ref="${esc(x)}">${esc(x)}</button>`).join(" ")}</dd>
      <dt>Predicts</dt><dd>${esc(plain(it.hypothesis))}</dd>
      <dt>Starts when</dt><dd class="mono">${esc(it.start_when)} (only while orders are in flight)</dd>
      <dt>Auto-stop if</dt><dd>${limits.map(esc).join(" · ") || DASH}</dd>
      <dt>Blast radius</dt><dd>${(v.blast_radius || []).map((x) => `<button class="chip" data-ref="${esc(x)}">${esc(x)}</button>`).join(" ") || "none"}
        ${Object.entries(v.workflows_affected || {}).map(([w, how]) => `<span class="muted"> ${esc(w)}: ${esc(nice(how))}</span>`).join("")}</dd>
      <dt>Policy guard</dt><dd>${v.verdict === "approve" ? pill("ok", "", "approved") : pill("fault", "", v.verdict || DASH)} ${esc(v.rationale || "")}</dd>
    </dl>
    ${p.excluded?.length ? `<p class="muted">Excluded on request: ${p.excluded.map((f) => esc(nice(f))).join(", ")}</p>` : ""}
    <div class="a-actions">
      <button class="primary fault" data-ag="approve" ${busy || blockers.length ? "disabled" : ""}>Approve and inject</button>
      <button class="toggle" data-ag="replan" ${busy ? "disabled" : ""}>Pick another fault</button>
      <button class="toggle" data-ag="cancel" ${busy ? "disabled" : ""}>Discard</button></div>
    ${blockers.length ? `<ul class="blockers">${blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
    <p class="muted small-note">On approval the plan runs exactly as shown, in its own process, under the safety controller: preflight, warm-up, injection only with orders in flight, the limits above, recovery watchdog. The kill switch stops it at any moment.</p></div>`;
}

function runHtml(c, st) {
  const r = c.run || {};
  const x = st?.experiment;
  const phase = st?.campaign?.active ? st.campaign.state : "starting";
  const t = x && x.active && x.t_inject ? tsec(st.now - x.t_inject) : null;
  const steps = [["PREFLIGHT_OK", "preflight"], ["WAIT_IN_FLIGHT", "waiting for orders in flight"], ["INJECTING", "injecting"], ["OBSERVING", "observing"],
                 ["RECOVERY_WAIT", "recovering"], ["DONE", "done"]];
  return `<p class="big"><b>${esc(nice(r.fault))}</b> on <b>${esc((r.target || "").replace("node:", ""))}</b> · run <span class="mono">${esc(r.run_id)}</span></p>
    <p>State <b class="mono">${esc(phase)}</b>${t ? ` · <b class="mono">${esc(t)}</b> since injection` : ""} <span class="muted">(${esc(steps.map((s) => s[1]).join(" → "))})</span></p>
    <p class="muted">Agent 2 is not told about this run. Watch its panel: it should notice on its own.</p>
    <details class="console"><summary class="muted">Runner output</summary><pre class="cmd">${esc((c.console || []).join("\n")) || "…"}</pre></details>`;
}

// ------------------------------------------------------------------ Agent 2
const A2_STATE = {
  watching: ["ok", "", "watching"], suspicious: ["warn", "!", "symptom seen, confirming"], investigating: ["a2", "", "investigating"],
  concluded: ["a2", "", "diagnosed"], "re-arming": ["muted", "", "re-arming"], paused: ["muted", "‖", "paused"],
};
function agent2Head() {
  const a = A.a2;
  if (!a || !a.available) return `<p class="muted">Agent 2 runs with the live dashboard only.</p>`;
  const [k, i, w] = A2_STATE[a.state] || ["muted", "?", a.state];
  const bad = Object.entries(a.unhealthy || {});
  const watch = bad.length ? `<p><b>Now:</b> ${bad.map(([n, why]) => `<b>${esc(n)}</b> ${esc(why.join(", "))}`).join("; ")}
      ${a.state === "suspicious" ? `<span class="muted"> (${a.streak}/${a.trigger_ticks} s before it investigates)</span>` : ""}</p>`
    : `<p class="muted">Every service healthy. Checked ${a.checked_at ? clock(a.checked_at) : DASH}; investigates on its own when a service stays Down or Degraded for ${a.trigger_ticks} s.${a.rearm_in_s != null ? ` Re-arms in ${a.rearm_in_s} s.` : ""}</p>`;
  return `<div class="chart-head"><h2><span class="eyebrow a2">Agent 2</span> Diagnosis, always on</h2><span>${pill("a2", "", "blind")} ${pill(k, i, w)}</span></div>
    ${watch}
    <div class="a-actions"><button class="toggle" data-ag="investigate" ${A.busy || a.state === "investigating" ? "disabled" : ""}>Investigate now</button>
      <button class="toggle" data-ag="a2pause" data-paused="${a.enabled ? "1" : "0"}" ${A.busy ? "disabled" : ""}>${a.enabled ? "Pause auto-trigger" : "Resume auto-trigger"}</button></div>
    ${a.error ? `<p class="muted">Watcher error: ${esc(a.error)}</p>` : ""}`;
}

function agent2Body() {
  const a = A.a2;
  if (!a || !a.available) return "";
  const cur = a.current;
  const bj = a.bundle_job;
  const bundle = bj ? `<p class="muted" style="margin-top:8px">Full evidence diagnosis of <span class="mono">${esc(bj.run_id)}/${esc(bj.exp)}</span>:
      ${bj.status === "running" ? "running…" : bj.status === "done" ? `done (${esc(String(bj.source || "").startsWith("llm") ? "LLM" : "rule-based fallback")}) · <a href="?run=${esc(bj.run_id)}/${esc(bj.exp)}#story" data-open-run="${esc(bj.run_id)}/${esc(bj.exp)}">graded in the story</a>` : `failed: ${esc(bj.error)}`}</p>` : "";
  const recent = (a.recent || []).filter((r) => !cur || r.id !== cur.id).slice(0, 4);
  return `${cur ? incidentHtml(cur) : ""}${bundle}
    ${recent.length ? `<details class="recent"><summary class="muted">Earlier investigations (${recent.length})</summary><ul>${recent.map((r) =>
      `<li><span class="mono">${esc(clock(r.started))}</span> ${esc((r.services || []).join(", ") || "health check")}: ${esc(plain(r.root_cause || r.status))}</li>`).join("")}</ul></details>` : ""}`;
}

function incidentHtml(r) {
  const d = r.diagnosis;
  const steps = (r.steps || []).map((s) => {
    const key = `${r.id}:${s.n}`, open = A.open.has(key);
    return `<li><span class="ico" aria-hidden="true">${s.n}</span><div>
      <span class="kind">${esc(s.kind)}</span> <span class="muted">${s.by === "rule" ? "(fixed sequence)" : ""}</span>
      <div class="txt${open ? "" : " clip"}">${esc(plain(s.thought))}</div>
      <button class="chip tool" data-ag="toggle-step" data-key="${esc(key)}" aria-expanded="${open}">tool:${s.n} ${esc(s.action)}${s.service !== "none" ? `(${esc(s.service)})` : "()"}${s.action !== "pod_status" && s.action !== "database_check" && s.action !== "downstream_check" && s.action !== "dependencies" ? ` · ${s.since_s} s` : ""} ${open ? "▾" : "▸"}</button>
      ${open ? `<pre class="cmd result" data-scroll-key="${esc(key)}" tabindex="0" aria-label="Tool result">${esc(JSON.stringify(s.result, null, 1))}</pre>` : `<div class="muted result-sum">${esc(summarize(s))}</div>`}</div></li>`;
  }).join("");
  const working = r.status === "investigating";
  const pending = working ? `<li><span class="ico" aria-hidden="true"><span class="spin"></span></span><div><span class="kind">${r.phase === "concluding" ? "conclude" : "thinking"}</span>
      <div class="txt muted">${r.phase === "concluding" ? "Writing the diagnosis from the tool results; fixes only from the playbook." : "Choosing the next tool…"}</div></div></li>` : "";
  const concl = d ? `<div class="callout" style="margin-top:12px"><h3 style="color:var(--a2)">Root cause ${sourceBadge(r.llm_used ? {kind: "llm", text: "Mistral"} : {kind: "fallback", badge: "Rule-based fallback"})}</h3>
      <div class="rc">${esc(plain(d.root_cause))}</div>
      <ul style="margin:0;padding-left:1.1rem">${(d.failure_chain || []).map((x) => `<li>${esc(plain(x))}</li>`).join("")}</ul>
      ${(d.fixes || []).length ? `<h3 style="margin-top:12px;color:var(--fix)">Suggested fixes (vetted playbook, not applied)</h3><ol class="fixes">${d.fixes.map((f) =>
        `<li><b>${esc(f.title || nice(f.playbook_id))}</b> <span class="chip">${esc(f.playbook_id)}</span><div class="muted">${esc(plain(f.why))}</div>${f.change ? `<pre class="cmd">${esc(f.change)}</pre>` : ""}</li>`).join("")}</ol>` : `<p class="muted">No fix needed.</p>`}
    </div>` : r.status === "failed" ? `<p class="muted">Investigation failed: ${esc(r.error)}</p>` : "";
  return `<div class="incident" data-incident-id="${esc(r.id)}">
    <p style="margin-top:12px"><b>${r.by === "operator" ? "Asked by the operator" : "Triggered by the monitor"}</b> at <span class="mono">${esc(clock(r.started))}</span>${r.ended ? ` · ${Math.round(r.ended - r.started)} s` : ""}${r.run_id ? ` · during run <span class="mono">${esc(r.run_id)}</span>` : ""}</p>
    <pre class="cmd trig">${esc(r.trigger)}</pre>
    <ol class="stepper" data-scroll-key="steps" tabindex="0" aria-label="Agent 2 investigation steps">${steps}${pending}</ol>${concl}</div>`;
}

/** one line per tool result, so the transcript reads without opening the JSON */
function summarize(s) {
  const r = s.result || {};
  if (r.error) return `error: ${r.error}`;
  switch (s.action) {
    case "pod_status": return Object.entries(r).map(([svc, ps]) => `${svc} ${ps.filter((p) => p.ready).length}/${ps.length} Ready${ps.some((p) => p.restarts) ? ` (${ps.reduce((a, p) => a + (p.restarts || 0), 0)} restarts)` : ""}`).join(" · ");
    case "recent_events": return `${r.total} events` + (r.events?.length ? `: ${r.events.slice(0, 2).map((e) => `${e.reason} ${e.object}`).join("; ")}` : "");
    case "service_logs": return `${r.matching_lines} error/warning lines` + (r.lines?.length ? `: "${String(r.lines[r.lines.length - 1]).slice(0, 110)}"` : "");
    case "queue_stats": return `reachable ${r.now?.reachable}, waiting ${r.now?.wait ?? DASH} (max ${r.waiting_max ?? DASH}, baseline ${r.baseline_waiting_max ?? DASH}), active ${r.now?.active ?? DASH}, unreachable ${r.seconds_unreachable} s`;
    case "client_slis": return r.load_on === false ? r.note : `W1 p95 max ${secs(r.w1_p95_ms?.max)} s (${r.w1_p95_vs_baseline ?? DASH}x baseline), errors/s max ${r.w1_errors_per_s?.max ?? DASH}, W2 accepted/s ${r.w2_accepted_per_s?.mean ?? DASH}, ${r.seconds_w1_completed_nothing} s with no W1 completion`;
    case "database_check": return `reachable ${r.reachable}, ${r.w2_accepted_30s_to_5min_ago_without_row ?? DASH} accepted W2 orders without a row`;
    case "downstream_check": return (r.probes || []).map((p) => p.error ? p.error : `${p.status} in ${p.ms} ms`).join(", ");
    case "dependencies": return `depends on ${(r.depends_on || []).join(", ") || "nothing"}; used by ${(r.used_by || []).join(", ") || "nothing"}`;
    case "run_detectors": return (r.hits || []).map((h) => `${h.severity} ${h.id}`).join(", ") || "no detector fired";
    default: return "";
  }
}
