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

const events = new EventSource("/events");
events.addEventListener("main", (e) => {
  const before = new Set([...main.querySelectorAll("li[id]")].map((n) => n.id));
  main.innerHTML = e.data;
  document.title = main.querySelector("[data-title]")?.dataset.title || document.title;
  tick();
  if (motion.matches) {
    let i = 0;
    for (const n of main.querySelectorAll("li[id]")) {
      if (before.has(n.id)) continue;
      n.style.animationDelay = i++ * 70 + "ms";
      n.classList.add("enter");
    }
  }
});
events.onopen = () => document.body.classList.remove("offline");
events.onerror = () => document.body.classList.add("offline");
setInterval(tick, 30000);
tick();
