// The Flame page: an icicle of where athena's time and tokens went. One request per scope (/flame/data); zoom, metric and
// table switches redraw here. When the index is stale the server updates it in the background; this page then watches
// /flame/status and swaps the new data in when it lands, keeping the zoom.
(() => {
  const M = ["time", "tokens", "cost", "output"], MI = { time: 0, tokens: 1, cost: 2, output: 3 }, GAP = 2;
  const UNIT = ["agent time", "tokens", "est.", "output"], BY = ["agent time", "tokens", "est. $", "output"];
  const AGENT_TIME = "Agent time sums parallel work (subagents, workflows, background runs), so it is not wall time.";
  const KINDS = [["model", "model replies"], ["shell", "shell"], ["files", "files"], ["agent", "agents and workflows"],
    ["web", "web and MCP"], ["tool", "other tools"], ["group", "sessions"]];
  const $ = (id) => document.getElementById(id);
  let data = null, hl = null, boxes = [], watching = null;
  const st = { m: "time", s: "athena", f: "", u: "", z: [], t: "tool", d: "0" };

  function readHash() {
    const h = new URLSearchParams(location.hash.slice(1));
    st.m = M.includes(h.get("m")) ? h.get("m") : "time"; st.s = h.get("s") || "athena";
    st.f = h.get("from") || ""; st.u = h.get("to") || ""; st.t = h.get("tab") || "tool";
    st.d = h.get("d") || (st.f || st.u ? "" : "0");
    st.z = (h.get("z") || "").split("/").filter(Boolean).map(decodeURIComponent);
  }
  function writeHash() {
    const h = new URLSearchParams();
    if (st.m !== "time") h.set("m", st.m); if (st.s !== "athena") h.set("s", st.s);
    if (st.f) h.set("from", st.f); if (st.u) h.set("to", st.u); if (st.d && st.d !== "0") h.set("d", st.d);
    if (st.t !== "tool") h.set("tab", st.t); if (st.z.length) h.set("z", st.z.map(encodeURIComponent).join("/"));
    const t = "#" + h.toString();
    if (location.hash !== t) history.replaceState(null, "", t.length > 1 ? t : location.pathname);
  }
  // A local date, as the index's days are.
  const day = (n) => { const d = new Date(); d.setDate(d.getDate() - n + 1); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };

  function fmtT(ms) {
    const s = ms / 1000; if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + " s";
    const m = Math.floor(s / 60), h = Math.floor(m / 60);
    if (h >= 100) return Math.round(h).toLocaleString() + " h";
    if (h) return h + " h " + String(m % 60).padStart(2, "0") + " m";
    return m + " m " + String(Math.round(s % 60)).padStart(2, "0") + " s";
  }
  function fmtExact(ms) {
    const s = Math.round(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return (h ? h + " h " : "") + (h || m ? String(m).padStart(h ? 2 : 1, "0") + " m " : "") + String(s % 60).padStart(h || m ? 2 : 1, "0") + " s";
  }
  function fmtN(n) {
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 1 : 2) + " B";
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 1 : 2) + " M";
    if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + " k";
    return String(n);
  }
  function fmtC(u) { const d = u / 1e6; if (d >= 1000) return "$" + Math.round(d).toLocaleString(); if (d >= 10) return "$" + d.toFixed(0); return "$" + d.toFixed(2); }
  const fmt = (i, v) => (i === 0 ? fmtT(v) : i === 2 ? fmtC(v) : fmtN(v));
  const pct = (a, b) => (b ? (a / b * 100 < 0.1 ? "<0.1" : (a / b * 100).toFixed(a / b * 100 < 10 ? 1 : 0)) + "%" : "–");
  function ago(at) {
    if (!at) return "";
    const min = Math.round((Date.now() / 1000 - at) / 60);
    return min < 1 ? "just now" : min < 120 ? min + " min ago" : Math.round(min / 60) + " h ago";
  }
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };

  function query() {
    const q = new URLSearchParams({ scope: st.s });
    if (st.f) q.set("since", st.f); if (st.u) q.set("until", st.u);
    return q;
  }
  async function load(quiet) {
    if (!quiet) { $("ice").classList.add("stale"); }
    let r;
    try { r = await fetch("/flame/data?" + query()); } catch { note("The dashboard did not answer."); return; }
    if (!r.ok) { note("The index could not be read: " + (await r.text())); return; }
    data = await r.json();
    $("ice").classList.remove("stale");
    fillScopes(); render(); watch();
  }
  // While an index runs in the background, look every few seconds; when it has finished, take the new data.
  function watch() {
    clearTimeout(watching);
    if (!data || !data.refreshing) return;
    watching = setTimeout(async () => {
      try {
        const s = await (await fetch("/flame/status")).json();
        if (s.refreshing) { data.refreshing = true; status(); return watch(); }
        if ((s.at || 0) !== ((data.status.last_run || {}).at || 0)) return load(true);
        data.refreshing = false; status();
      } catch { watch(); }
    }, 4000);
  }

  function fillScopes() {
    const sel = $("scope"), keep = st.s, base = [...sel.options].slice(0, 4);
    sel.innerHTML = ""; base.forEach((o) => sel.add(o));
    const add = (label, items, prefix) => {
      if (!items.length) return;
      const g = document.createElement("optgroup"); g.label = label;
      items.forEach(([v, t]) => g.append(new Option(t, prefix + v))); sel.add(g);
    };
    const w = Object.entries(data.sessions || {}).filter(([, s]) => s.kind === "worker").sort((a, b) => (b[1].last || 0) - (a[1].last || 0));
    add("Worker", w.map(([n, s]) => [n, s.ticket ? n + " · " + s.ticket : n]), "worker:");
    add("Ticket", (data.status.tickets || []).map((t) => [t, t]), "ticket:");
    if (![...sel.options].some((o) => o.value === keep)) sel.add(new Option(keep.replace(":", ": "), keep));
    sel.value = keep;
  }
  function note(text) { const n = $("fl-status"); n.className = "fl-note"; n.textContent = text; }
  function status() {
    const s = data.status, n = $("fl-status");
    n.className = "fl-note"; n.textContent = "";
    if (data.refreshing) n.append(el("span", "live", s.files ? "Updating the index · " : "Building the index for the first time; it reads every transcript once, in about a minute · "));
    const at = (s.last_run || {}).at;
    n.append((at ? "Updated " + ago(at) + " · " : "") + (s.files || 0).toLocaleString() + " transcripts"
      + (s.first_day ? " from " + s.first_day + " to " + s.last_day : "") + " · served in " + data.ms + " ms. ");
    if (!data.refreshing) n.append("It updates itself when you open this page; ", el("code", "", "athena flame index"), " does it by hand.");
    const lines = s.skipped_lines || 0, failed = (s.last_run || {}).failed || 0;
    if (lines || failed) {
      // Quiet, but said: what the index could not read is missing from the chart.
      const h = el("span", "fl-health");
      h.append(" Not in the chart: " + [lines ? lines.toLocaleString() + (lines === 1 ? " line" : " lines") + " it could not read" : "",
        failed ? failed.toLocaleString() + (failed === 1 ? " transcript" : " transcripts") + " the last update could not index" : ""]
        .filter(Boolean).join(" and ") + "; ", el("code", "", "athena flame status"), " lists them.");
      n.append(h);
    }
  }

  function find(path) {
    let n = data.root; const chain = [n];
    for (const p of path) { const c = (n.c || []).find((x) => x.n === p); if (!c) break; n = c; chain.push(n); }
    return chain;
  }
  function render() {
    writeHash();
    document.querySelectorAll("#fl-metric button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.m === st.m));
    document.querySelectorAll("#fl-span button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.d === st.d || (b.dataset.d === "custom" && st.d === "")));
    $("fl-range").hidden = st.d !== "";
    document.querySelectorAll("#fl-tabs button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.t === st.t));
    $("since").value = st.f; $("until").value = st.u;
    const s = data.status;
    if (!data.root || (!data.root.v[0] && !data.root.v[1])) {
      $("ice").style.height = "0"; $("ice").textContent = ""; $("fl-crumbs").textContent = ""; $("fl-rows").textContent = ""; $("fl-sum").textContent = "";
      status();
      if (!data.refreshing) { const n = $("fl-status"); n.className = s.files ? "fl-note" : "fl-empty"; n.prepend(s.files ? "Nothing in this scope and span. " : "No index yet. "); }
      return;
    }
    status();
    const chain = find(st.z); st.z = chain.slice(1).map((n) => n.n);
    const node = chain[chain.length - 1], r = data.root, i = MI[st.m];
    const sum = $("fl-sum"); sum.textContent = "";
    M.forEach((m, j) => {
      const o = el("span"); o.append(el(j === i ? "b" : "span", "", fmt(j, node.v[j])), " " + UNIT[j] + (j === i && node !== r ? " · " + pct(node.v[j], r.v[j]) + " of " + r.n : ""));
      sum.append(o);
    });
    const cr = $("fl-crumbs"); cr.textContent = "";
    chain.forEach((n, k) => {
      if (k) cr.append(el("i", "", "›"));
      const b = el("button", "", n.n); b.type = "button"; b.onclick = () => { st.z = st.z.slice(0, k); render(); }; cr.append(b);
    });
    draw(chain); top(); legend();
  }
  // The chart takes the height the window gives it below the controls, between 24 and 56 px a row, so a shallow tree
  // isn't a strip over an empty page and a deep one still scrolls.
  function rowHeight(rows) {
    const top = $("ice").getBoundingClientRect().top + scrollY, room = innerHeight - top - 104;
    return Math.max(24, Math.min(56, Math.floor(room / Math.max(rows, 1))));
  }
  function draw(chain) {
    const ice = $("ice"), W = ice.clientWidth || 900, i = MI[st.m], node = chain[chain.length - 1];
    ice.textContent = ""; boxes = [];
    const ups = chain.slice(0, -1), laid = [];
    const total = node.v[i] || 1, base = chain.slice(1).map((x) => x.n);
    let depth = 0;
    (function lay(n, x, w, d, path, anc) {
      if (laid.length > 3000) return;
      depth = Math.max(depth, d); laid.push([n, x, w, d, path, anc]);
      let cx = x;
      for (const c of n.c || []) { const cw = (c.v[i] / total) * W; if (cw >= 1) lay(c, cx, cw, d + 1, path.concat(c.n), anc.concat(c)); cx += cw; }
    })(node, 0, W, 0, base, chain.slice());
    const ROW = rowHeight(ups.length + depth + 1), frag = document.createDocumentFragment();
    ice.style.setProperty("--row", ROW - GAP + "px");
    ups.forEach((n, k) => frag.append(box(n, 0, k * ROW, W, "up", chain.slice(1, k + 1).map((x) => x.n), chain.slice(0, k + 1))));
    const y = ups.length * ROW;
    laid.forEach(([n, x, w, d, path, anc]) => frag.append(box(n, x, y + d * ROW, w, "", path, anc)));
    ice.append(frag); ice.style.height = y + (depth + 1) * ROW + "px"; applyHl();
  }
  function box(n, x, y, w, cls, path, anc) {
    const b = el("button", "k-" + (n.k || "group") + (cls ? " " + cls : "") + (w < 26 ? " tiny" : "") + (w < 6 ? " hair" : ""), n.n);
    b.type = "button"; b.style.left = x + "px"; b.style.top = y + "px"; b.style.width = Math.max(w - GAP, 1) + "px";
    b.setAttribute("role", "treeitem"); b.setAttribute("aria-label", n.n + ", " + fmt(MI[st.m], n.v[MI[st.m]]));
    b._n = n; b._p = path; b._a = anc; boxes.push(b); return b;
  }
  function tip(e, b) {
    const n = b._n, t = $("fl-tip"), r = data.root, a = b._a, parent = a.length > 1 ? a[a.length - 2] : null, i = MI[st.m];
    const rows = [["Agent time", fmtExact(n.v[0]) + "  ·  " + pct(n.v[0], r.v[0])], ["  model / tools", fmtT(n.d[4]) + " / " + fmtT(Math.max(n.v[0] - n.d[4], 0))],
      ["Tokens", n.v[1].toLocaleString() + "  ·  " + pct(n.v[1], r.v[1])], ["  input", n.d[0].toLocaleString()], ["  output", n.v[3].toLocaleString()],
      ["  cache read", n.d[1].toLocaleString()], ["  cache write", n.d[2].toLocaleString()], ["Est. $", fmtC(n.v[2]) + "  ·  " + pct(n.v[2], r.v[2])],
      ["Model calls", n.d[3].toLocaleString()]];
    if (parent && parent !== r) rows.splice(1, 0, ["  of " + parent.n, pct(n.v[i], parent.v[i]) + " by " + BY[i]]);
    const s = n.s && data.sessions[n.s];
    const when = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    if (s) { if (s.ticket) rows.unshift(["Ticket", s.ticket]); if (s.first) rows.push(["Wall span", when(s.first) + " → " + when(s.last)]); }
    t.textContent = "";
    const tb = document.createElement("table");
    rows.forEach(([k, v]) => { const tr = tb.insertRow(); tr.insertCell().textContent = k; tr.insertCell().textContent = v; });
    t.append(el("b", "", n.n), el("div", "p", [r.n].concat(b._p).join(" › ") + (s && s.title ? " — " + s.title : "")), tb,
      el("div", "q", "Agent time sums parallel work; it is not wall time."));
    t.hidden = false; place(e);
  }
  // Beside the pointer, and inside the chart's own band, so it never covers the legend or the table.
  function place(e) {
    const t = $("fl-tip"), box = $("ice").getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight;
    let x = e.clientX + 16;
    if (x + w > Math.min(innerWidth - 8, box.right)) x = e.clientX - w - 16;
    let y = e.clientY + 16;
    if (y + h > Math.min(box.bottom, innerHeight - 8)) y = e.clientY - h - 12;
    y = Math.max(y, box.top, 8);
    t.style.left = Math.max(8, x) + "px"; t.style.top = y + "px";
  }
  function legend() {
    const l = $("fl-legend"); l.textContent = "";
    KINDS.forEach(([k, t]) => { const s = el("span", "", t); s.style.setProperty("--c", "var(--k-" + k + ")"); l.append(s); });
    if (st.m === "time") l.append(el("p", "fl-meaning", AGENT_TIME));
  }
  function top() {
    const rows = (data.top || {})[st.t] || [], i = MI[st.m];
    $("fl-topby").textContent = " by " + BY[i];
    const sorted = rows.slice().sort((a, b) => b[1 + i] - a[1 + i]).slice(0, 15), tot = data.root.v[i] || 1, tb = $("fl-rows");
    tb.textContent = "";
    if (!sorted.length) { const td = tb.insertRow().insertCell(); td.colSpan = 3; td.className = "c none"; td.textContent = st.t === "skill" ? "No step in this scope ran under a skill." : "Nothing here."; return; }
    sorted.forEach((row) => {
      const tr = tb.insertRow(), n = tr.insertCell(); n.className = "n"; n.textContent = row[0]; n.title = row[0];
      const sh = el("span", "fl-share"), bar = el("i"); bar.style.width = Math.min(100, (row[1 + i] / tot) * 100) + "%"; sh.append(bar); n.append(sh);
      const v = tr.insertCell(); v.className = "v"; v.textContent = fmt(i, row[1 + i]);
      const c = tr.insertCell(); c.className = "c"; c.textContent = pct(row[1 + i], tot) + " · " + fmtN(row[5]) + " calls";
      tr.onmouseenter = () => { hl = { t: st.t, label: row[0] }; applyHl(); };
      tr.onmouseleave = () => { hl = null; applyHl(); };
      tr.onclick = () => { const lit = boxes.filter((x) => x.classList.contains("hl")); if (lit.length === 1) { st.z = lit[0]._p.slice(); render(); } };
    });
  }
  function applyHl() {
    const on = !!hl && hl.t !== "skill"; let any = false;
    boxes.forEach((b) => { const m = on && (b._n.h || []).includes(hl.t + ":" + hl.label); b.classList.toggle("hl", m); any = any || m; });
    $("ice").classList.toggle("dim", on && any);
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("#ice button"); if (b) { st.z = b._p.slice(); $("fl-tip").hidden = true; render(); return; }
    const m = e.target.closest("#fl-metric button"); if (m) { st.m = m.dataset.m; render(); return; }
    const t = e.target.closest("#fl-tabs button"); if (t) { st.t = t.dataset.t; render(); return; }
    const d = e.target.closest("#fl-span button");
    if (d && d.dataset.d === "custom") {
      // A range starts as the last 7 days, filled in, so no empty date field shows its placeholder.
      st.d = ""; st.f = st.f || day(7); st.u = st.u || day(1); st.z = []; writeHash(); render(); load(); $("since").focus(); return;
    }
    if (d) { st.d = d.dataset.d; st.f = d.dataset.d === "0" ? "" : day(+d.dataset.d); st.u = ""; st.z = []; writeHash(); load(); }
  });
  $("ice").addEventListener("mousemove", (e) => { const b = e.target.closest("button"); if (b) tip(e, b); else $("fl-tip").hidden = true; });
  $("ice").addEventListener("mouseleave", () => { $("fl-tip").hidden = true; });
  $("ice").addEventListener("focusin", (e) => { const b = e.target.closest("button"); if (b) { const r = b.getBoundingClientRect(); tip({ clientX: r.left, clientY: r.bottom }, b); } });
  $("ice").addEventListener("focusout", () => { $("fl-tip").hidden = true; });
  $("scope").addEventListener("change", (e) => { st.s = e.target.value; st.z = []; writeHash(); load(); });
  ["since", "until"].forEach((id) => $(id).addEventListener("change", (e) => { st[id === "since" ? "f" : "u"] = e.target.value; st.d = ""; st.z = []; writeHash(); load(); }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && st.z.length && !e.target.closest("input,select")) { st.z.pop(); render(); } });
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => data && render(), 80); });
  addEventListener("hashchange", () => {
    const old = JSON.stringify([st.s, st.f, st.u]); readHash();
    if (JSON.stringify([st.s, st.f, st.u]) !== old) load(); else if (data) render();
  });
  readHash(); load(true);
})();
