// Live updates: the server pushes a new <main> over Server-Sent Events whenever athena's state files change.
const main = document.querySelector("main");
const motion = matchMedia("(prefers-reduced-motion: no-preference)");

function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}
function tick() {
  for (const t of document.querySelectorAll("time[data-ts]")) t.textContent = ago(+t.dataset.ts);
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

// An ask's open panel stays open across pushes and reloads of this tab: its id is kept here and in sessionStorage,
// which may be unavailable (a private window, blocked storage), so every use of it is wrapped.
const OPEN = "athena-open-asks";
const opened = new Set(
  (() => {
    try {
      const ids = JSON.parse(sessionStorage.getItem(OPEN) || "[]");
      return Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : [];
    } catch {
      return [];
    }
  })(),
);
function keepOpened() {
  try {
    sessionStorage.setItem(OPEN, JSON.stringify([...opened]));
  } catch {}
}
function reopen() {
  const shown = new Set();
  for (const d of main.querySelectorAll("details[data-ask]")) {
    shown.add(d.dataset.ask);
    if (!opened.has(d.dataset.ask)) continue;
    d.classList.add("kept"); // open already: no unfolding again
    d.open = true;
  }
  let gone = false;
  for (const id of opened) if (!shown.has(id) && !answered.has(id)) gone = opened.delete(id); // done, or a new state dir
  if (gone) keepOpened();
}

// Where a reply is typed: a row's pills, or the answers in its panel, which share the item.
const where = (acts) => acts.dataset.item + (acts.matches(".bar") ? "|bar" : "");
function actsAt(key) {
  const [item, bar] = key.split("|");
  return main.querySelector(`.acts${bar ? ".bar" : ":not(.bar)"}[data-item="${CSS.escape(item)}"]`);
}

// Text selected in a panel, being read or copied: a push would drop the selection. Only while this window has focus:
// a page left with a selection in it still takes pushes, so it is never stale when looked at again.
function selecting() {
  const sel = getSelection();
  return document.hasFocus() && !!sel && !sel.isCollapsed && !!sel.anchorNode?.parentElement?.closest("main .panel");
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
    for (const acts of all) (acts.closest(".fold") || acts.closest(".apr") || acts.closest("li"))?.remove();
    if (!all.length) answered.delete(item); // the server's page has it too
  }
  if (tipFor && !tipFor.isConnected) hideTip(); // its pill went with the old page
  reopen();
  for (const [key, text] of drafts) {
    const acts = actsAt(key);
    if (acts) openReply(acts, text, false);
    else drafts.delete(key);
  }
  tick();
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

// hides: the answer takes its ask, or its pill, off the page.
function answer(acts, body, text, hides = acts.hasAttribute("data-hides")) {
  const item = acts.dataset.item;
  const row = hides ? acts.closest(".fold") || acts.closest(".apr") || acts.closest("li") : null;
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

undo.addEventListener("click", () => {
  if (!last) return;
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
  const acts = pill?.closest(".acts");
  if (!acts || pill.matches("a")) return; // Open PR is a plain link to the PR
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
  else answer(acts, { option: pill.dataset.option }, `“${label}” sent to athena`);
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
// toggle does not bubble: listen in the capture phase. A panel the human closes unfolds again when reopened.
main.addEventListener(
  "toggle",
  (e) => {
    const d = e.target;
    if (!d.matches?.("details[data-ask]")) return;
    if (d.open) opened.add(d.dataset.ask);
    else {
      opened.delete(d.dataset.ask);
      d.classList.remove("kept");
    }
    keepOpened();
  },
  true,
);
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

const events = new EventSource("/events");
const boot = document.querySelector('meta[name="athena-boot"]')?.content;
// A restarted dash has a new token and maybe new files: a page from before it reloads, once per start.
events.addEventListener("boot", (e) => boot && e.data !== boot && location.reload());
events.addEventListener("main", (e) => render(e.data));
events.onopen = () => document.body.classList.remove("offline");
events.onerror = () => document.body.classList.add("offline");
setInterval(tick, 30000);
reopen();
tick();
