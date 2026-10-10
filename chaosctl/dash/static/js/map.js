// System map: built from /api/graph (nodes, edges, layout), never hardcoded. Per node: name, health pill
// (icon + word), one key number. Roles go to a hover tooltip. During a fault: 💥 on the target, amber outline
// on the blast radius, red edge for network faults between two services.
import {$, esc} from "./util.js";

let G = null;   // {graph, layout, roles}
export const mapState = {hl: null};

export function buildMap(g) {
  G = g;
  const svg = $("map"), L = g.layout;
  if (!L) { svg.innerHTML = `<text x="16" y="40" style="fill:var(--muted)">No dependency graph: run chaosctl graph.</text>`; svg.setAttribute("viewBox", "0 0 800 80"); return; }
  svg.setAttribute("viewBox", `0 0 ${L.width} ${L.map_height + 8}`);
  const mk = (id, c) => `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" style="fill:${c}"/></marker>`;
  // Arrows show data flow (A -> B in the picture means data moves from A to B); the graph stores
  // "A depends on B", which for this app is the same direction as the request flow.
  let h = `<defs>${mk("m-arr", "var(--line-2)")}${mk("m-arr-hl", "var(--a1)")}${mk("m-arr-f", "var(--fault)")}</defs>`;
  for (const z of L.zones) h += `<g class="zone"><rect x="${z.x}" y="${z.y}" width="${z.w}" height="${z.h}" rx="10"/><text x="${z.x + 14}" y="${z.y + 28}">${esc(z.label)}</text></g>`;
  for (const e of L.edges) {
    const flow = (e.label || "").split(" ")[0];
    const w = flow.length * 9.5 + 16;
    h += `<g class="edge${e.logical ? " logical" : ""}" id="me-${cid(e.id)}"><path d="${e.d}" marker-end="url(#m-arr)"/>
      <title>${esc(e.from)} to ${esc(e.to)}: ${esc(e.label)}</title>
      <g class="lbl"><rect x="${e.mid[0] - w / 2}" y="${e.mid[1] - 13}" width="${w}" height="26" rx="4"/><text x="${e.mid[0]}" y="${e.mid[1] + 6}" text-anchor="middle">${esc(flow)}</text></g></g>`;
  }
  for (const [name, n] of Object.entries(L.nodes)) {
    const roles = name === "client" ? ["load generator: W1 sync (waits), W2 async (200 at once)"] : (n.roles || []);
    h += `<g class="node" id="mn-${cid(name)}" tabindex="0" role="img" aria-label="${esc(name)}">
      <title>${esc(name)}: ${esc(roles.join(", "))}</title>
      <rect class="box" x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="10"/>
      <text class="name" x="${n.x + 14}" y="${n.y + 38}">${esc(name)}</text>
      <g id="mn-${cid(name)}-pill"></g>
      <text class="key" id="mn-${cid(name)}-key" x="${n.x + 14}" y="${n.y + n.h - 18}"></text></g>`;
  }
  h += `<g id="m-over"></g>`;
  svg.innerHTML = h;
}
const cid = (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "_");

const PILL = {ok: ["✓", "healthy", "var(--ok)"], warn: ["!", "degraded", "var(--warn)"], bad: ["✕", "down", "var(--fault)"],
              rec: ["↻", "recovering", "var(--warn)"], none: ["?", "no data", "var(--faint)"], client: ["", "", "var(--muted)"]};

/** view: {nodes: {name: {health: ok|warn|bad|rec|none, key: "queue 0"}}, fault: {target, edge, active, label}, blast: [names]} */
export function updateMap(view) {
  if (!G || !G.layout) return;
  const L = G.layout;
  for (const [name, n] of Object.entries(L.nodes)) {
    const v = (view.nodes || {})[name] || {health: name === "client" ? "client" : "none"};
    const g = document.getElementById(`mn-${cid(name)}`);
    if (!g) continue;
    const cls = `node ${v.health === "bad" ? "bad" : (v.health === "warn" || v.health === "rec") ? "warn" : ""}${mapState.hl === "node:" + name ? " hl" : ""}`;
    if (g.getAttribute("class") !== cls) g.setAttribute("class", cls);
    const [ic, word, col] = PILL[v.health] || PILL.none;
    const pill = document.getElementById(`mn-${cid(name)}-pill`);
    const pw = word ? word.length * 9.5 + 38 : 0;
    const html = word ? `<rect x="${n.x + 14}" y="${n.y + 52}" width="${pw}" height="28" rx="14" style="fill:none;stroke:${col};stroke-width:1.5"/>
      <text class="pill-t" x="${n.x + 26}" y="${n.y + 72}" style="fill:${col}">${ic} ${word}</text>` : "";
    if (pill.dataset.h !== html) { pill.innerHTML = html; pill.dataset.h = html; }
    document.getElementById(`mn-${cid(name)}-key`).textContent = v.key || "";
    g.setAttribute("aria-label", `${name}: ${word || "client"}${v.key ? ", " + v.key : ""}`);
  }
  for (const e of L.edges) {
    const g = document.getElementById(`me-${cid(e.id)}`);
    const lvl = mapState.hl === e.id ? "hl" : (view.fault && view.fault.active && view.fault.edge === e.id) ? "fault" : "";
    const cls = `edge${e.logical ? " logical" : ""}${lvl ? " " + lvl : ""}`;
    if (g.getAttribute("class") !== cls) {
      g.setAttribute("class", cls);
      g.querySelector("path").setAttribute("marker-end", `url(#${lvl === "hl" ? "m-arr-hl" : lvl === "fault" ? "m-arr-f" : "m-arr"})`);
    }
  }
  let o = "";
  const f = view.fault;
  if (f && f.active) {
    for (const b of view.blast || []) {
      const n = L.nodes[b];
      if (n) o += `<rect class="blast" x="${n.x - 8}" y="${n.y - 8}" width="${n.w + 16}" height="${n.h + 16}" rx="14"><title>blast radius: depends on ${esc(f.target)}</title></rect>`;
    }
    const n = L.nodes[f.target];
    const e = f.edge ? L.edges.find((x) => x.id === f.edge) : null;
    const [cx, cy] = e ? e.mid : n ? [n.x + n.w - 20, n.y + 4] : [null, null];
    if (cx != null) {
      o += `<circle class="boom-ring" cx="${cx}" cy="${cy}" r="22"/><text x="${cx}" y="${cy + 9}" text-anchor="middle" style="font-size:24px" aria-hidden="true">\u{1F4A5}</text>
        <text class="boom" x="${cx}" y="${cy - 30}" text-anchor="middle">${esc(f.label)}</text>`;
    }
  }
  $("m-over").innerHTML = o;
}

export function highlight(ref) { mapState.hl = ref; }
