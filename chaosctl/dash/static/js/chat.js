// "Ask Agent 2": a conversation about the run on screen. Answers come from POST /api/chat (RAG over our own run
// data, diagnoses, the findings report, logs, the playbook and docs); the selected run's records are pinned first.
// Advisory only: the chat never changes the system.
import {$, esc, plain} from "./util.js";

const SUGGEST = [
  "What went wrong in this run, in plain words?",
  "What happened to the in-flight orders?",
  "Why did Agent 2 pick that root cause?",
  "Which fix should we apply first, and how do we verify it?",
];
let history = [];      // [{q, a}] sent back so follow-ups keep context
let getRun = () => null;
let busy = false;

export function setupChat(runGetter) {
  getRun = runGetter;
  $("chat-open").onclick = () => toggle(true);
  $("chat-close").onclick = () => toggle(false);
  $("chat-clear").onclick = () => { history = []; $("chat-thread").innerHTML = ""; renderSuggest(); $("chat-q").focus(); };
  $("chat-form").onsubmit = (ev) => { ev.preventDefault(); send($("chat-q").value); };
  $("chat-q").onkeydown = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send($("chat-q").value); }
  };
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("chatpanel").hidden) toggle(false); });
  $("chat-thread").addEventListener("click", (ev) => {
    const s = ev.target.closest?.("[data-src]");
    if (s) s.classList.toggle("open");
  });
  renderSuggest();
}

export function chatContextChanged() { if (!$("chatpanel").hidden) renderCtx(); }

function toggle(open) {
  $("chatpanel").hidden = !open;
  $("chat-open").setAttribute("aria-expanded", String(open));
  document.documentElement.classList.toggle("chat-open", open);
  if (open) { renderCtx(); $("chat-q").focus(); }
}

function renderCtx() {
  const run = getRun();
  $("chat-ctx").innerHTML = run
    ? `Talking about <span class="chip">${esc(run)}</span> <span class="muted">(its result, report, diagnosis, events and logs come first)</span>`
    : `<span class="muted">No run selected: answers draw on all recorded runs.</span>`;
}

function renderSuggest() {
  $("chat-suggest").innerHTML = history.length ? "" :
    SUGGEST.map((q) => `<button type="button" class="chip suggest">${esc(q)}</button>`).join("");
  $("chat-suggest").querySelectorAll("button").forEach((b) => { b.onclick = () => send(b.textContent); });
}

async function send(raw) {
  const q = (raw || "").trim();
  if (!q || busy) return;
  busy = true;
  $("chat-q").value = "";
  $("chat-suggest").innerHTML = "";
  const run = getRun();
  const thread = $("chat-thread");
  thread.insertAdjacentHTML("beforeend", `<div class="bubble you">${esc(q)}</div>
    <div class="bubble a2 pending" aria-busy="true"><span class="dots">Agent 2 is reading the run data</span></div>`);
  const slot = thread.lastElementChild;
  slot.scrollIntoView({block: "end"});
  try {
    const r = await (await fetch("/api/chat", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question: q, history: history.slice(-6), run}),
    })).json();
    slot.classList.remove("pending");
    slot.removeAttribute("aria-busy");
    slot.innerHTML = answerHTML(r);
    if (r.llm_used) history.push({q, a: plain(r.answer).slice(0, 1200)});
  } catch (e) {
    slot.classList.remove("pending");
    slot.innerHTML = `<p class="warnline">Chat unavailable: ${esc(e.message)}</p>`;
  }
  slot.scrollIntoView({block: "end"});
  busy = false;
}

function answerHTML(r) {
  const who = r.llm_used === false
    ? `<p class="warnline">Warning: The LLM was not used: these are only the matching records.</p>`
    : `<p class="who">answered by ${esc(String(r.source || "").replace(/^llm:/, ""))}</p>`;
  const srcs = (r.sources || []).map((s) =>
    `<div class="src${s.this_run ? " mine" : ""}" data-src tabindex="0" role="button" aria-label="Show source ${esc(s.id)}">
       <span class="chip">${esc(s.id)}</span>${s.this_run ? ` <span class="tag">this run</span>` : ""}
       <span class="snip">${esc(s.snippet)}</span></div>`).join("");
  return `${who}<div class="txt">${md(r.answer)}</div>${srcs ? `<div class="srcs"><span class="muted">Sources (click to expand)</span>${srcs}</div>` : ""}`;
}

// tiny, safe markdown: escape first, then **bold**, `code`, line breaks and "- " lists
function md(s) {
  return esc(s || "")
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(^|\n)- /g, "$1• ")
    .replace(/\n/g, "<br>");
}
