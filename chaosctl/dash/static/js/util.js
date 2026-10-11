// Small shared helpers. No data is generated here: every number comes from the API.
export const $ = (id) => document.getElementById(id);
export const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
export const DASH = "-";
export const nice = (s) => String(s ?? "").replaceAll("_", " ");
export const plain = (s) => String(s ?? "").replace(/\*\*|`/g, "");
export const fix1 = (v, d = 1) => (v == null || Number.isNaN(+v) ? DASH : (+v).toFixed(d));
export const secs = (ms) => (ms == null ? DASH : (ms / 1000).toFixed(1));
export const pct = (r) => (r == null ? DASH : Math.round(r * 100) + " %");
export const clock = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], {hour12: false}) : DASH);
export function tplus(s) {
  if (s == null || Number.isNaN(s)) return DASH;
  const sign = s < 0 ? "-" : "+";
  const a = Math.abs(Math.round(s));
  return `T${sign}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
}
export const tsec = (s) => (s == null ? DASH : `T${s < 0 ? "" : "+"}${Math.round(s)} s`);

export async function getJSON(url) {
  const r = await fetch(url, {cache: "no-store"});
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

// Pills always include a readable label, with an optional plain-text marker.
export const pill = (kind, icon, word) => `<span class="pill ${kind}">${icon ? `<span aria-hidden="true">${esc(icon)}</span>` : ""}${esc(word)}</span>`;
export function healthPill(state) {
  return {
    Healthy: pill("ok", "", "healthy"), Recovering: pill("warn", "", "recovering"),
    Degraded: pill("warn", "!", "degraded"), Down: pill("fault", "", "down"),
  }[state] || pill("muted", "?", "no data");
}
export function sourceBadge(src) {
  if (!src) return "";
  if (src.kind === "fallback") return pill("warn", "!", src.badge || "Rule-based fallback");
  return pill("ok", "", src.text + (src.kind === "cached" ? "" : ""));
}

export const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window: setting is not kept */ } },
};

export function showTip(ev, html) {
  const tip = $("tip");
  tip.innerHTML = html;
  tip.hidden = false;
  tip.style.left = Math.min(ev.clientX + 14, innerWidth - 360) + "px";
  tip.style.top = (ev.clientY + 14) + "px";
}
export const hideTip = () => { $("tip").hidden = true; };
