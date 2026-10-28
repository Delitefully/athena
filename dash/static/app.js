// Live updates: the server pushes a new <main> over Server-Sent Events whenever athena's state files change. An item's
// full view (/item/<id>) fetches its own <main> again on each push; the search page (/items) keeps its own.
const main = document.querySelector("main");
const fullItem = main.matches(".full[data-item]") ? main.dataset.item : "";
const searchPage = main.matches(".full:not([data-item])");
const motion = matchMedia("(prefers-reduced-motion: no-preference)");

function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}
// A row's age, as server.go short says it: now, 10m, 2h, 7d.
function short(seconds) {
  if (seconds < 60) return "now";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
  return Math.floor(seconds / 86400) + "d";
}
// When an item is due, as server.go dueText says it.
function dueText(ts) {
  const now = new Date();
  const due = new Date(ts * 1000);
  const d = ts - now / 1000;
  if (d < 0) return "overdue " + short(-d);
  if (d < 3600) return "due in " + Math.floor(d / 60) + "m";
  const day = (t) => new Date(t.getFullYear(), t.getMonth(), t.getDate());
  const days = Math.round((day(due) - day(now)) / 86400000);
  if (days === 0) return "due " + due.toTimeString().slice(0, 5);
  if (days === 1) return "due tmrw";
  if (days < 7) return "due " + due.toLocaleDateString("en-GB", { weekday: "short" });
  return "due " + due.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}
// athena doctor's dot is rendered in <main>, which every push replaces, and shown in the header.
function placeHealth() {
  const h = main.querySelector(":scope > .health");
  const slot = document.querySelector(".health-slot");
  if (h && slot) slot.replaceChildren(h);
}
function tick() {
  for (const t of document.querySelectorAll("time[data-ts]"))
    t.textContent = t.hasAttribute("data-short") ? short(Date.now() / 1000 - +t.dataset.ts) : ago(+t.dataset.ts);
  for (const t of document.querySelectorAll("time[data-due]")) t.textContent = dueText(+t.dataset.due);
}

// Quick actions. A new page waits while the human is mid-gesture (a press, a collapse, typing a reply), so a push
// never eats a click or a draft; the newest waiting page goes in once they are done.
const token = document.querySelector('meta[name="athena-token"]')?.content || "";
const answered = new Set(); // asks answered here, kept off any page rendered before the server had the answer
const drafts = new Map(); // where (an item, and "|bar" for its panel's answers) -> a reply being typed, kept across pages
let held = null;
let shown = main.innerHTML;
let collapsing = 0;
let pressing = false;

// Where a reply is typed: a row's pills, or the answers in its panel, which share the item.
const where = (acts) => acts.dataset.item + (acts.matches(".bar") ? "|bar" : "");
function actsAt(key) {
  const [item, bar] = key.split("|");
  return main.querySelector(`.acts${bar ? ".bar" : ":not(.bar)"}[data-item="${CSS.escape(item)}"]`);
}

// Text selected in a full view, being read or copied: a push would drop the selection. Only while this window has
// focus: a page left with a selection in it still takes pushes, so it is never stale when looked at again.
function selecting() {
  const sel = getSelection();
  return document.hasFocus() && !!sel && !sel.isCollapsed && !!sel.anchorNode?.parentElement?.closest("main.full");
}

function busy() {
  return collapsing > 0 || pressing || selecting() || (document.activeElement?.matches("main .reply input") ?? false);
}

function render(html) {
  if (busy()) {
    held = html;
    return;
  }
  held = null;
  shown = html;
  const before = new Set([...main.querySelectorAll("li[id]")].map((n) => n.id));
  main.innerHTML = html;
  document.title = main.querySelector("[data-title]")?.dataset.title || document.title;
  for (const item of answered) {
    const all = main.querySelectorAll(`.acts[data-item="${CSS.escape(item)}"]`); // the row's, and its panel's
    for (const acts of all) (acts.closest(".apr") || acts.closest("li"))?.remove();
    if (!all.length) answered.delete(item); // the server's page has it too
  }
  if (tipFor && !tipFor.isConnected) hideTip(); // its pill went with the old page
  placeHealth();
  for (const [key, text] of drafts) {
    const acts = actsAt(key);
    if (acts) openReply(acts, text, false);
    else drafts.delete(key);
  }
  tick();
  filter();
  if (motion.matches) {
    let i = 0;
    for (const n of main.querySelectorAll("li[id]")) {
      if (before.has(n.id)) continue;
      n.style.animationDelay = i++ * 70 + "ms";
      n.classList.add("enter");
    }
  }
}
function release() {
  if (held !== null && !busy()) render(held);
}

async function post(body) {
  const r = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Athena-Token": token },
    body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || "not sent");
  return out;
}

// The row collapses by height alone, never a fade, and a pill on the Awaiting review row by its width; at once when the
// reader asks for less motion.
function collapse(el) {
  if (!motion.matches) {
    el.remove();
    return;
  }
  collapsing++;
  const side = el.matches(".apr") ? "width" : "height";
  el.style[side] = (side === "width" ? el.offsetWidth : el.offsetHeight) + "px";
  el.offsetHeight; // start from the measured size
  el.classList.add("collapsing");
  el.style[side] = "0px";
  let done = false;
  const end = () => {
    if (done) return;
    done = true;
    el.remove();
    collapsing--;
    release();
  };
  el.addEventListener("transitionend", (e) => e.propertyName === side && end());
  setTimeout(end, 300);
}

const toast = document.querySelector(".toast");
const toastText = toast.querySelector(".toast-text");
const undo = toast.querySelector(".undo");
let toastTimer = 0;
let last = null; // {seq, item} of the answer the toast can undo; seq 0 for a PR dismissed from Awaiting review

function say(text, undoable) {
  toastText.textContent = text;
  undo.hidden = !undoable;
  undo.disabled = true; // until the server gives the answer its seq
  toast.hidden = false;
  toast.classList.remove("show");
  toast.offsetWidth;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
    last = null;
  }, 5000);
}

// hides: the answer takes its item, or its pill, off the page. "Don't ask me this again", ticked in a full view, rides
// on the answer given with it.
function answer(acts, body, text, hides = acts.hasAttribute("data-hides")) {
  const item = acts.dataset.item;
  const row = hides ? acts.closest(".apr") || acts.closest("li") : null;
  if (acts.querySelector('input[name="rule"]')?.checked && !body.discuss) {
    body = { ...body, rule: true };
    text += ", and athena won't ask again";
  }
  drafts.delete(where(acts));
  if (row) {
    answered.add(item);
    collapse(row);
  }
  say(text, true);
  last = null;
  post({ item, ...body })
    .then(({ seq }) => {
      last = { seq, item };
      undo.disabled = false;
    })
    .catch((err) => {
      answered.delete(item);
      say("Not sent: " + err.message, false);
      if (row || acts.closest(".apr")) render(held ?? shown); // bring the row back, or the pill as it was
    });
}

// The dash's own calls, which work while athena is busy or down: Done and Snooze, undone by Reopen and Wake.
async function postItem(body) {
  const r = await fetch("/api/item", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Athena-Token": token },
    body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || "not done");
  return out;
}
function snoozeUntil(what) {
  const t = new Date();
  if (/^\d+$/.test(what || "")) return Math.floor(t / 1000) + +what;
  t.setHours(9, 0, 0, 0);
  if (what === "monday") t.setDate(t.getDate() + (((8 - t.getDay()) % 7) || 7));
  else t.setDate(t.getDate() + 1); // tomorrow, 09:00
  return Math.floor(t / 1000);
}
function itemCall(pill) {
  const item = pill.closest("[data-item]").dataset.item;
  const what = pill.dataset.do;
  const body = { item, do: what };
  if (what === "snooze") body.until = snoozeUntil(pill.dataset.for);
  const row = !fullItem && (what === "done" || what === "snooze") ? pill.closest("li") : null;
  if (row) collapse(row);
  const said = { done: "Done", snooze: "Snoozed", reopen: "Reopened", wake: "Back now" }[what];
  say(said, what === "done" || what === "snooze");
  last = null;
  postItem(body)
    .then(() => {
      last = { item, undoes: what === "done" ? "reopen" : "wake" };
      undo.disabled = false;
      if (fullItem) refresh();
    })
    .catch((err) => {
      say("Not done: " + err.message, false);
      if (row) render(held ?? shown);
    });
}

undo.addEventListener("click", () => {
  if (!last) return;
  if (last.undoes) {
    const { item, undoes } = last;
    last = null;
    undo.disabled = true;
    postItem({ item, do: undoes })
      .then(() => (say("Undone", false), fullItem && refresh()))
      .catch((err) => say("Not undone: " + err.message, false));
    return;
  }
  const { seq, item } = last;
  last = null;
  undo.disabled = true;
  // Before the POST: the server pushes the restored row before it replies, and a page must not hide it again.
  answered.delete(item);
  post(seq ? { seq, undo: true } : { item, undo: true }) // a dismissed PR has no seq: its undo names it
    .then(() => say("Undone", false))
    .catch((err) => say(err.message === "too late to undo" ? "Too late: athena has it" : "Not undone: " + err.message, false));
});

function openReply(acts, text = "", focus = true) {
  if (!acts.querySelector(".reply")) {
    acts.dataset.pills = acts.innerHTML;
    acts.classList.add("replying");
    acts.innerHTML =
      '<form class="reply"><input type="text" name="text" maxlength="2000" autocomplete="off" ' +
      'placeholder="Reply to athena…" aria-label="Reply to athena"></form>';
  }
  const input = acts.querySelector("input");
  if (text) input.value = text;
  if (focus) input.focus();
}
function closeReply(acts) {
  drafts.delete(where(acts));
  acts.innerHTML = acts.dataset.pills || "";
  acts.classList.remove("replying");
  delete acts.dataset.pills;
  release();
}

main.addEventListener("click", (e) => {
  const pill = e.target.closest(".pill");
  if (pill?.hasAttribute("data-do")) return itemCall(pill);
  const acts = pill?.closest(".acts");
  if (!acts || pill.matches("a")) return; // Open, Open PR: plain links
  const label = pill.textContent.trim();
  const apr = acts.closest(".apr");
  if (pill.hasAttribute("data-reply")) openReply(acts);
  else if (pill.hasAttribute("data-discuss")) answer(acts, { discuss: true }, "athena will bring it up in HQ");
  else if (apr && pill.dataset.option === "dismiss") answer(acts, { option: "dismiss" }, "Dismissed until it changes", true);
  else if (apr && pill.dataset.option === "recheck") {
    apr.classList.add("rechecking"); // the server's page says so too, once it has the answer
    answer(acts, { option: "recheck" }, "athena will re-check it");
  } else if (pill.dataset.option === "dismiss") answer(acts, { option: "dismiss" }, "Dismissed");
  else if (pill.dataset.option === "recheck") answer(acts, { option: "recheck" }, "athena will re-check it");
  else if (pill.dataset.option === "request-review") answer(acts, { option: "request-review" }, "athena will request a review");
  // A go names the head this page shows: the server takes it only while the PR is still there.
  else if (pill.dataset.option === "go") answer(acts, { option: "go", head: pill.dataset.head }, "Merge go sent to athena");
  else if (pill.dataset.option === "undo-default") answer(acts, { option: "undo-default" }, "athena will take it back", true);
  else if (pill.matches(".rec")) answer(acts, { option: pill.dataset.option }, "Rec taken: athena has it");
  else answer(acts, { option: pill.dataset.option }, `“${label}” sent to athena`);
});
// A row with a full view opens it from anywhere on it. Its links, pills and fields do only their own thing, a drag
// that selects text opens nothing, and Cmd- or Ctrl-click opens it in a tab of its own, as its name's link would.
main.addEventListener("click", (e) => {
  const row = e.target.closest("li.has-view");
  const link = row?.querySelector(".name a");
  if (!link || e.button !== 0 || e.target.closest("a, button, input, label, summary, form, .acts")) return;
  const sel = getSelection();
  if (sel && !sel.isCollapsed && row.contains(sel.anchorNode)) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey) open(link.href, "_blank", "noopener");
  else location.href = link.href;
});
main.addEventListener("submit", (e) => {
  e.preventDefault();
  const acts = e.target.closest(".acts");
  const text = e.target.elements.text.value.trim();
  if (!acts || !text) return;
  answer(acts, { text }, "Reply sent to athena");
  if (acts.isConnected) closeReply(acts); // a worker's row stays
});
main.addEventListener("keydown", (e) => {
  const acts = e.target.closest(".reply")?.closest(".acts");
  if (acts && e.key === "Escape") {
    closeReply(acts);
    acts.querySelector(".pill")?.focus();
  }
});
main.addEventListener("input", (e) => {
  const acts = e.target.closest(".reply")?.closest(".acts");
  if (acts) drafts.set(where(acts), e.target.value);
});
document.addEventListener("selectionchange", () => release());
addEventListener("blur", () => setTimeout(release));
main.addEventListener("focusout", (e) => {
  const acts = e.target.closest(".reply")?.closest(".acts");
  if (acts && !e.target.value.trim()) setTimeout(() => acts.isConnected && !acts.contains(document.activeElement) && closeReply(acts));
  setTimeout(release);
});
main.addEventListener("pointerdown", () => (pressing = true));
addEventListener("pointerup", () => setTimeout(() => ((pressing = false), release())));
addEventListener("pointercancel", () => ((pressing = false), release()));

// Tooltips: one element on the body, outside every clipped .swap, .acts and .panel, for the icon-only pills (their
// aria-label) and anything with a data-tip. Above its pill, below when there is no room, shifted to stay in the window.
const tip = document.querySelector(".tip");
const TIPPED = ".acts.options [data-discuss], .acts.options [data-reply], .pill.dismiss, .pill.recheck, [data-tip]";
const EDGE = 8;
let tipFor = null;
function showTip(el) {
  const text = el.dataset.tip || el.getAttribute("aria-label");
  if (!text) return hideTip();
  tipFor = el;
  tip.textContent = text;
  tip.hidden = false;
  const r = el.getBoundingClientRect();
  const t = tip.getBoundingClientRect();
  let top = r.top - t.height - 6;
  if (top < EDGE) top = r.bottom + 6; // no room above: below
  const width = document.documentElement.clientWidth; // the window less its scrollbar
  const left = Math.min(Math.max(r.left + r.width / 2 - t.width / 2, EDGE), width - t.width - EDGE);
  tip.style.transform = `translate(${Math.floor(left)}px, ${Math.round(top)}px)`;
}
function hideTip() {
  tipFor = null;
  tip.hidden = true;
}
document.addEventListener("pointerover", (e) => {
  if (e.pointerType === "touch") return;
  const el = e.target.closest?.(TIPPED);
  if (el) el !== tipFor && showTip(el);
  else if (tipFor) hideTip();
});
document.addEventListener("pointerout", (e) => !e.relatedTarget && hideTip()); // left the window
document.addEventListener("focusin", (e) => {
  const el = e.target.closest?.(TIPPED);
  if (el?.matches(":focus-visible")) showTip(el);
  else hideTip();
});
document.addEventListener("focusout", () => setTimeout(() => tipFor && !tipFor.matches(":hover, :focus-visible") && hideTip()));
addEventListener("scroll", hideTip, true);
addEventListener("resize", hideTip);

// The header's filter, on the main page: typing in Search items hides every row and Awaiting review pill whose
// data-search (its name, title, PR, repo, ticket and workers) lacks one of the words, case-insensitively, as you type;
// a fold with a match opens while it is on. Escape or an empty box shows them all again. Enter opens the full search
// (/items), which also finds resolved and archived items.
const search = document.querySelector(".search input");
function filter() {
  if (fullItem || searchPage || !search) return;
  const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
  for (const el of main.querySelectorAll("li.item[data-search], .aprs > li")) {
    const hay = [el, ...el.querySelectorAll("[data-search]")].map((n) => n.dataset.search || "").join(" ");
    el.hidden = !words.every((w) => hay.includes(w));
  }
  main.classList.toggle("filtering", words.length > 0);
  for (const d of main.querySelectorAll("details")) {
    if (words.length && !d.open && d.querySelector("li.item[data-search]:not([hidden])")) {
      d.open = true;
      d.dataset.filtered = "";
    } else if (!words.length && d.hasAttribute("data-filtered")) {
      d.open = false;
      delete d.dataset.filtered;
    }
  }
}
search?.addEventListener("input", filter);
search?.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !search.value) return;
  e.preventDefault();
  search.value = "";
  filter();
});

const events = new EventSource("/events");
const boot = document.querySelector('meta[name="athena-boot"]')?.content;
// A restarted dash has a new token and maybe new files: a page from before it reloads, once per start.
events.addEventListener("boot", (e) => boot && e.data !== boot && location.reload());
// A full view takes its own <main> again on each push: the server's is the main page's.
async function refresh() {
  const r = await fetch(location.pathname + "?fragment=1", { headers: { Accept: "text/html" } }).catch(() => null);
  if (r?.ok) render(await r.text());
}
// The search page's results are its own: a push of the main page's <main> would replace them (and did, at once, since
// the stream sends the current page as soon as it connects).
events.addEventListener("main", (e) => (fullItem ? refresh() : searchPage ? null : render(e.data)));
events.onopen = () => document.body.classList.remove("offline");
events.onerror = () => document.body.classList.add("offline");
setInterval(tick, 30000);
placeHealth();
tick();
filter();
