"""The Flame page's body. The dash wraps it in its own head and foot (dash/server.py) and serves `static/flame.css` and
`static/flame.js` from this folder under its /static/ path, as its Content-Security-Policy allows scripts and styles only
from there. Everything on the page comes from `/flame/data`, one request per scope.
"""
from pathlib import Path

STATIC = Path(__file__).with_name("static")
FILES = ("flame.css", "flame.js")

BODY = """<main class="full flame" data-flame>
<div class="fl-bar">
 <div class="fl-seg" role="group" aria-label="Metric" id="fl-metric"><button type="button" data-m="time" aria-pressed="true">Time</button><button type="button" data-m="tokens" aria-pressed="false">Tokens</button><button type="button" data-m="cost" aria-pressed="false">Est. $</button><button type="button" data-m="output" aria-pressed="false">Output tokens</button></div>
 <label>Scope <select id="scope"><option value="athena">All athena</option><option value="hq">HQ only</option><option value="workers">Workers only</option><option value="all">Everything, with other sessions</option></select></label>
 <div class="fl-seg" role="group" aria-label="Dates" id="fl-span"><button type="button" data-d="1" aria-pressed="false">Today</button><button type="button" data-d="7" aria-pressed="false">7 days</button><button type="button" data-d="30" aria-pressed="false">30 days</button><button type="button" data-d="0" aria-pressed="true">All</button><button type="button" data-d="custom" aria-pressed="false">Custom</button></div>
 <span class="fl-range" id="fl-range" hidden><label>From <input type="date" id="since"></label><label>To <input type="date" id="until"></label></span>
</div>
<div class="fl-grid">
 <div>
  <div class="fl-head"><nav class="fl-crumbs" id="fl-crumbs" aria-label="Zoom path"></nav><div class="fl-sum" id="fl-sum" aria-live="polite"></div></div>
  <div class="ice" id="ice" role="tree" aria-label="Where the time and tokens went"></div>
  <div class="fl-legend" id="fl-legend"></div>
  <p class="fl-note" id="fl-status" aria-live="polite">Loading…</p>
 </div>
 <section class="fl-top" aria-label="Top frames">
  <h2>Top frames<small id="fl-topby"></small></h2>
  <div class="fl-seg" role="group" aria-label="Top frames by" id="fl-tabs"><button type="button" data-t="tool" aria-pressed="true">Tools</button><button type="button" data-t="bash" aria-pressed="false">Bash</button><button type="button" data-t="agent" aria-pressed="false">Subagents</button><button type="button" data-t="skill" aria-pressed="false">Skills</button></div>
  <table><tbody id="fl-rows"></tbody></table>
 </section>
</div>
<div class="fl-tip" id="fl-tip" hidden></div>
</main>"""


def body() -> str:
    return BODY


def static_file(name):
    """One of this page's own files, or None."""
    return STATIC / name if name in FILES else None
