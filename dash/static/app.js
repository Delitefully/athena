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
const drafts = new Map(); // item -> a reply being typed, kept across pages
let held = null;
let shown = main.innerHTML;
let collapsing = 0;
let pressing = false;

function busy() {
  return collapsing > 0 || pressing || (document.activeElement?.matches("main .reply input") ?? false);
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
    const acts = main.querySelector(`.acts[data-item="${CSS.escape(item)}"]`);
    if (acts) (acts.closest(".fold") || acts.closest("li"))?.remove();
    else answered.delete(item); // the server's page has it too
  }
  for (const [item, text] of drafts) {
    const acts = main.querySelector(`.acts[data-item="${CSS.escape(item)}"]`);
    if (acts) openReply(acts, text, false);
    else drafts.delete(item);
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

// The row collapses by height alone, never a fade; at once when the reader asks for less motion.
function collapse(el) {
  if (!motion.matches) {
    el.remove();
    return;
  }
  collapsing++;
  el.style.height = el.offsetHeight + "px";
  el.offsetHeight; // start from the measured height
  el.classList.add("collapsing");
  el.style.height = "0px";
  let done = false;
  const end = () => {
    if (done) return;
    done = true;
    el.remove();
    collapsing--;
    release();
  };
  el.addEventListener("transitionend", (e) => e.propertyName === "height" && end());
  setTimeout(end, 300);
}

const toast = document.querySelector(".toast");
const toastText = toast.querySelector(".toast-text");
const undo = toast.querySelector(".undo");
let toastTimer = 0;
let last = null; // {seq, item} of the answer the toast can undo

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

function answer(acts, body, text) {
  const item = acts.dataset.item;
  const row = acts.hasAttribute("data-hides") ? acts.closest(".fold") || acts.closest("li") : null;
  drafts.delete(item);
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
      if (row) render(held ?? shown); // bring the row back
    });
}

undo.addEventListener("click", () => {
  if (!last) return;
  const { seq, item } = last;
  last = null;
  undo.disabled = true;
  post({ seq, undo: true })
    .then(() => {
      answered.delete(item);
      say("Undone", false);
    })
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
  drafts.delete(acts.dataset.item);
  acts.innerHTML = acts.dataset.pills || "";
  acts.classList.remove("replying");
  delete acts.dataset.pills;
  release();
}

main.addEventListener("click", (e) => {
  const pill = e.target.closest(".pill");
  const acts = pill?.closest(".acts");
  if (!acts) return;
  const label = pill.textContent.trim();
  if (pill.hasAttribute("data-reply")) openReply(acts);
  else if (pill.hasAttribute("data-discuss")) answer(acts, { discuss: true }, "athena will bring it up in HQ");
  else if (pill.dataset.option === "dismiss") answer(acts, { option: "dismiss" }, "Dismissed");
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
  if (acts) drafts.set(acts.dataset.item, e.target.value);
});
main.addEventListener("focusout", (e) => {
  const acts = e.target.closest(".reply")?.closest(".acts");
  if (acts && !e.target.value.trim()) setTimeout(() => acts.isConnected && !acts.contains(document.activeElement) && closeReply(acts));
  setTimeout(release);
});
main.addEventListener("pointerdown", () => (pressing = true));
addEventListener("pointerup", () => setTimeout(() => ((pressing = false), release())));
addEventListener("pointercancel", () => ((pressing = false), release()));

const events = new EventSource("/events");
events.addEventListener("main", (e) => render(e.data));
events.onopen = () => document.body.classList.remove("offline");
events.onerror = () => document.body.classList.add("offline");
setInterval(tick, 30000);
tick();
