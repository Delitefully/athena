"""athena's menu bar icon prototypes (phase 1): four directions, four states each.

Run `python3 assets/src/menubar.py` to rebuild:
  assets/menubar/<direction>-<state>.svg   one macOS template glyph per direction and state
  assets/menubar/board/index.html          the self-contained preview board

Every glyph sits on a 22 x 22 canvas (the menu bar's 22 px at 1x) with an 18-19 px live area and is drawn in one
colour: the files use black, which is all a template image needs (macOS keeps only the alpha). Strokes are 1.75 px
with round caps and joins, the same family as the logo mark. "Needs you" cuts a ring out of the glyph and puts a dot
in it at the top right, so the dot reads apart from the glyph without a second colour. The static file for
"working" is one key frame and carries a mark of its own; the board animates it in CSS.
"""
import html
import pathlib

OUT = pathlib.Path(__file__).resolve().parents[1] / "menubar"
SW = 1.75
STATES = ["idle", "working", "needs-you", "stopped"]
STATE_NAMES = {"idle": "idle", "working": "workers working", "needs-you": "needs you", "stopped": "stopped"}
BADGE = (18, 4, 2.75, 4.6)  # dot centre x, y, radius, radius of the ring cut out of the glyph


def f(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


def with_badge(inner, uid):
    cx, cy, r, cut = BADGE
    return (f'<defs><mask id="{uid}-badge" maskUnits="userSpaceOnUse" x="0" y="0" width="22" height="22">'
            f'<rect width="22" height="22" fill="#fff"/><circle cx="{f(cx)}" cy="{f(cy)}" r="{f(cut)}" fill="#000"/>'
            f'</mask></defs><g mask="url(#{uid}-badge)">{inner}</g>'
            f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r)}" fill="currentColor"/>')


# 1. Knot: the logo mark's three strands, scaled from its 100-unit box to 18.5 px.
KNOT = [
    "M41.7 30.25C35.32 38.38 31.46 48.82 31.14 58.41C30.81 68 34 76.57 39.13 81.42C44.26 86.27 51.19 87.32 57 84.23"
    "C62.81 81.14 67.35 73.96 68.55 64.98",
    "M65.47 42.68C61.63 33.09 54.51 24.52 46.37 19.45C38.23 14.38 29.21 12.85 22.45 14.86C15.68 16.88 11.31 22.36"
    " 11.08 28.94C10.85 35.51 14.8 43.03 21.98 48.56",
    "M42.83 57.05C53.05 58.51 64.03 56.63 72.49 52.12C80.96 47.61 86.79 40.56 88.43 33.69C90.06 26.83 87.5 20.3"
    " 81.92 16.81C76.34 13.33 67.86 12.99 59.47 16.44",
]
KNOT_SCALE = 0.215


def knot(state, uid):
    if state == "working":     # a gap runs along each strand: the thread being drawn through
        extra = ' pathLength="100" stroke-dasharray="74 26" stroke-dashoffset="-34" class="kn-flow"'
    elif state == "stopped":   # the knot lets go: each strand keeps only its middle, nothing interlocks
        extra = ' pathLength="100" stroke-dasharray="52 100" stroke-dashoffset="-24"'
    else:
        extra = ""
    paths = "".join(f'<path d="{d}"{extra}/>' for d in KNOT)
    sw = SW / KNOT_SCALE
    g = (f'<g transform="translate(11 11.1) scale({KNOT_SCALE}) translate(-50 -50)" fill="none" stroke="currentColor"'
         f' stroke-width="{f(sw)}" stroke-linecap="round">{paths}</g>')
    return with_badge(g, uid) if state == "needs-you" else g


# 2. Owl: Athena's owl. Ear tufts and brow in one stroke, two eyes, a beak.
def owl(state, uid):
    brow = '<path d="M2.6 3.4C4.4 6.6 8.3 7.3 11 9.6C13.7 7.3 17.6 6.6 19.4 3.4"/>'
    beak = '<path d="M9.8 16.1L11 18.3L12.2 16.1"/>'
    if state == "stopped":     # asleep: the eyes close
        eyes = '<path d="M4 12.4Q7 15.1 10 12.4M12 12.4Q15 15.1 18 12.4"/>'
        pupils = ""
    else:
        eyes = '<circle cx="7" cy="12.6" r="3.15"/><circle cx="15" cy="12.6" r="3.15"/>'
        r, dx, dy = {"idle": (1.2, 0, 0), "working": (1.2, 1.15, 0.35), "needs-you": (1.65, 0, -0.1)}[state]
        cls = ' class="ow-look"' if state == "working" else ""
        pupils = (f'<g fill="currentColor" stroke="none"{cls}><circle cx="{f(7 + dx)}" cy="{f(12.6 + dy)}" r="{f(r)}"/>'
                  f'<circle cx="{f(15 + dx)}" cy="{f(12.6 + dy)}" r="{f(r)}"/></g>')
    g = (f'<g fill="none" stroke="currentColor" stroke-width="{SW}" stroke-linecap="round" stroke-linejoin="round">'
         f'{brow}{eyes}{beak}{pupils}</g>')
    return with_badge(g, uid) if state == "needs-you" else g


# 3. Weave: athena's thread woven through the workers, a 3 x 3 plain weave of over and under.
LINES = (5.5, 11, 16.5)   # warps (vertical, the workers) and wefts (horizontal, athena's thread) share a grid
ENDS = (2.75, 19.25)
GAP = SW + 2              # the knock-out where one thread passes under another


def weave(state, uid):
    rows = {"stopped": ()}.get(state, (0, 1, 2))   # stopped: the wefts are out, the bare warps stand
    lo, hi = ENDS

    def weft(j, colour):
        # while working the middle weft is woven across, again and again; the key frame catches it half way
        anim = ' pathLength="100" stroke-dasharray="53 100" class="wv-weave"' if state == "working" and j == 1 else ""
        return f'<path d="M{f(lo)} {f(LINES[j])}H{f(hi)}"{anim}/>' if colour else ""

    def mask(name, cuts):
        return (f'<mask id="{uid}-{name}" maskUnits="userSpaceOnUse" x="0" y="0" width="22" height="22">'
                f'<rect width="22" height="22" fill="#fff"/><g fill="none" stroke="#000" stroke-width="{f(GAP)}">'
                f'{cuts}</g></mask>')

    defs, out = [], []
    for i, x in enumerate(LINES):            # warp i passes under weft j when i + j is even
        cuts = "".join(weft(j, True) for j in rows if (i + j) % 2 == 0)
        line = f'<path d="M{f(x)} {f(lo)}V{f(hi)}"/>'
        if cuts:
            defs.append(mask(f"w{i}", cuts))
            line = f'<g mask="url(#{uid}-w{i})">{line}</g>'
        out.append(line)
    for j in rows:
        cuts = "".join(f'<path d="M{f(x)} 1V21"/>' for i, x in enumerate(LINES) if (i + j) % 2 == 1)
        defs.append(mask(f"t{j}", cuts))
        out.append(f'<g mask="url(#{uid}-t{j})">{weft(j, True)}</g>')
    g = (f'<defs>{"".join(defs)}</defs><g fill="none" stroke="currentColor" stroke-width="{SW}"'
         f' stroke-linecap="round">{"".join(out)}</g>')
    return with_badge(g, uid) if state == "needs-you" else g


# 4. Helm: Athena's Corinthian helmet, front on, crest up, a T-shaped visor cut through.
HELM_BODY = ("M11 4.2C15.35 4.2 18.25 7 18.25 11.1V14.1C18.25 16.9 16.55 19.35 13.6 20.5H8.4"
             "C5.45 19.35 3.75 16.9 3.75 14.1V11.1C3.75 7 6.65 4.2 11 4.2Z")
HELM_CREST = "M9.7 5.2V2.6C9.7 1.85 10.25 1.3 11 1.3C11.75 1.3 12.3 1.85 12.3 2.6V5.2Z"
HELM_VISOR = ('<rect x="5.3" y="9" width="11.4" height="3" rx="1.5"/>'
              '<rect x="9.75" y="9" width="2.5" height="13"/>')


def helm(state, uid):
    if state == "stopped":     # an empty helm: the same silhouette, outlined
        mask = (f'<mask id="{uid}-visor" maskUnits="userSpaceOnUse" x="0" y="0" width="22" height="22">'
                f'<g fill="#000" stroke="#fff" stroke-width="{f(SW * 2)}" stroke-linejoin="round">'
                f'<path d="{HELM_BODY}"/><path d="{HELM_CREST}"/>{HELM_VISOR}</g>'
                f'<g fill="#000">{HELM_VISOR}</g></mask>')
    else:
        mask = (f'<mask id="{uid}-visor" maskUnits="userSpaceOnUse" x="0" y="0" width="22" height="22">'
                f'<rect width="22" height="22" fill="#fff"/><g fill="#000">{HELM_VISOR}</g></mask>')
    body = (f'<g mask="url(#{uid}-visor)" fill="currentColor"><path d="{HELM_BODY}"/>'
            f'<path d="{HELM_CREST}"/></g>')
    glint = ""
    if state == "working":     # a light sweeps the visor
        glint = '<rect x="6.1" y="9.75" width="3.6" height="1.5" rx=".75" fill="currentColor" class="hm-glint"/>'
    g = f'<defs>{mask}</defs>{body}{glint}'
    return with_badge(g, uid) if state == "needs-you" else g


DIRECTIONS = [
    ("knot", "Knot", knot,
     "The logo itself: one calm mind holding three threads.",
     "A gap runs along each thread while workers work; the knot comes undone when athena stops."),
    ("owl", "Owl", owl,
     "Athena's owl keeps watch over the workers.",
     "Its eyes glance across the work, open wide when it needs you, and close when athena sleeps."),
    ("weave", "Weave", weave,
     "athena's thread woven through the workers: over, under, over.",
     "Working weaves the middle row across, again and again; stopped leaves only the bare warps."),
    ("helm", "Helm", helm,
     "Athena's helmet: the chief of staff on duty.",
     "A light sweeps the visor while workers run; the helm stands empty when athena stops."),
]


def svg(inner, size=22, cls="", label="", ns=False, extra=""):
    """ns adds the SVG namespace, which a standalone file needs and inline SVG in HTML does not."""
    a = f' class="{cls}"' if cls else ""
    x = ' xmlns="http://www.w3.org/2000/svg"' if ns else ""
    t = f'<title>{html.escape(label)}</title>' if label else ""
    return f'<svg{x} viewBox="0 0 22 22" width="{size}" height="{size}"{a}{extra} role="img">{t}{inner}</svg>'


def file_svg(key, state, fn, ns=True):
    """A template glyph: black on transparent, no CSS. Without ns it is the board's hidden copy for the rasteriser."""
    body = fn(state, f"{key}-{state}").replace("currentColor", "#000")
    extra = "" if ns else f' data-key="{key}-{state}"'
    return svg(body, label=f"athena menu bar: {key}, {STATE_NAMES[state]}", ns=ns, extra=extra)


# --- the board ---------------------------------------------------------------------------------------------------

WIFI = ('<svg viewBox="0 0 18 14" width="18" height="14" fill="currentColor" aria-hidden="true">'
        '<path d="M9 2.2c2.6 0 5 1 6.8 2.7l1.1-1.2C14.8 1.7 12 .6 9 .6S3.2 1.7 1.1 3.7l1.1 1.2C4 3.2 6.4 2.2 9 2.2z'
        'M9 5.6c1.7 0 3.2.6 4.4 1.7l1.1-1.2C13 4.7 11.1 4 9 4s-4 .7-5.5 2.1l1.1 1.2C5.8 6.2 7.3 5.6 9 5.6z'
        'M9 9c.8 0 1.5.3 2.1.8L9 12.2 6.9 9.8C7.5 9.3 8.2 9 9 9z"/></svg>')
BATTERY = ('<svg viewBox="0 0 27 13" width="27" height="13" aria-hidden="true">'
           '<rect x=".5" y=".5" width="22" height="12" rx="3.5" fill="none" stroke="currentColor" opacity=".45"/>'
           '<rect x="2" y="2" width="15" height="9" rx="2" fill="currentColor"/>'
           '<path d="M24 4.5v4c.9-.3 1.5-1.1 1.5-2s-.6-1.7-1.5-2z" fill="currentColor" opacity=".45"/></svg>')

CSS = """
:root{--bg:#F3EEE4;--panel:#FBF8F2;--ink:#1E2350;--mut:#5B5E78;--rule:#DED6C6;--thr:#B8321F;--glyph:#1E2350;
  color-scheme:light}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#14172E;--panel:#1A1E3A;--ink:#ECE5D6;
  --mut:#A3A3B8;--rule:#2C3150;--thr:#E0573D;--glyph:#ECE5D6;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#14172E;--panel:#1A1E3A;--ink:#ECE5D6;--mut:#A3A3B8;--rule:#2C3150;--thr:#E0573D;
  --glyph:#ECE5D6;color-scheme:dark}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Helvetica Neue",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:44px 40px 56px}
.serif{font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;font-weight:400}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap;
  border-bottom:1px solid var(--rule);padding-bottom:22px;margin-bottom:8px}
header h1{font-size:40px;line-height:1.05;margin:0;letter-spacing:-.01em}
header h1 em{font-style:normal;color:var(--thr)}
header p{margin:8px 0 0;color:var(--mut);max-width:60ch}
.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);font-size:13px}
.legend b{color:var(--ink);font-weight:600}
section.dir{padding:34px 0 30px;border-bottom:1px solid var(--rule)}
.head{display:grid;grid-template-columns:auto 1fr;column-gap:16px;align-items:baseline;margin-bottom:20px}
.num{font:600 13px/1 ui-monospace,"SF Mono",Menlo,monospace;color:var(--thr);letter-spacing:.04em}
.head h2{font-size:30px;margin:0;line-height:1.1}
.head .idea{grid-column:2;margin:6px 0 0;font-size:17px}
.head .how{grid-column:2;margin:2px 0 0;color:var(--mut);font-size:14px}
.body{display:grid;gap:18px}
.specs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.spec{background:var(--panel);border:1px solid var(--rule);border-radius:12px;padding:12px 10px 10px;
  display:grid;grid-template-columns:auto auto 1fr;align-items:center;gap:14px}
.spec .big{color:var(--glyph);display:block}
.spec .loupe{width:66px;height:66px;image-rendering:pixelated;color:var(--glyph);
  background:repeating-conic-gradient(color-mix(in srgb,var(--rule) 55%,transparent) 0 25%,transparent 0 50%) 0 0/6px 6px;
  border-radius:4px}
.spec .lbl{font-size:12px;color:var(--mut);line-height:1.3}
.spec .lbl b{display:block;color:var(--ink);font-weight:600;font-size:13px}
.bars{display:grid;gap:8px}
.bars .row{display:grid;grid-template-columns:44px repeat(4,minmax(0,1fr));gap:10px;align-items:center}
.tag{font:600 11px/1.2 ui-monospace,"SF Mono",Menlo,monospace;color:var(--mut);letter-spacing:.03em}
.tag span{display:block;font-weight:400}
.desk{border-radius:8px;overflow:hidden;height:40px;position:relative;border:1px solid var(--rule)}
.desk.light{background:linear-gradient(115deg,#c9d6e8 0%,#e9d9c6 55%,#d7c1b2 100%)}
.desk.dark{background:linear-gradient(115deg,#141a35 0%,#2b2547 55%,#3c2a33 100%)}
.mb{position:absolute;left:0;right:0;top:0;height:24px;display:flex;align-items:center;justify-content:flex-end;
  padding:0 8px 0 0;font:500 13px/24px system-ui,-apple-system,sans-serif;white-space:nowrap;
  -webkit-backdrop-filter:blur(20px) saturate(1.6);backdrop-filter:blur(20px) saturate(1.6)}
.light .mb{background:rgba(246,246,246,.72);color:rgba(0,0,0,.85)}
.dark .mb{background:rgba(28,28,30,.55);color:rgba(255,255,255,.92)}
.mb .it{display:flex;align-items:center;justify-content:center;height:22px;padding:0 6px;border-radius:5px}
.mb .ours{padding:0 5px}
.mb .ours.open{background:rgba(0,0,0,.1)}
.dark .mb .ours.open{background:rgba(255,255,255,.2)}
.mb canvas{width:22px;height:22px;image-rendering:pixelated;display:block}
.mb svg{display:block}
.mb .clock{padding:0 2px 0 6px}
.colhead{display:grid;grid-template-columns:44px repeat(4,minmax(0,1fr));gap:10px;font-size:12px;color:var(--mut)}
.colhead span{text-align:center}
footer{padding-top:22px;color:var(--mut);font-size:13px;display:grid;gap:4px}
footer code{font:12px ui-monospace,"SF Mono",Menlo,monospace;color:var(--ink)}
/* motion: working only */
@keyframes kn-flow{to{stroke-dashoffset:-134}}
.kn-flow{animation:kn-flow 2.6s linear infinite}
@keyframes ow-look{0%,18%{transform:translate(0,0)}26%,52%{transform:translate(-2.3px,0)}60%,100%{transform:translate(0,0)}}
.ow-look{animation:ow-look 3.2s ease-in-out infinite}
@keyframes wv-weave{0%{stroke-dasharray:0 100}62%,86%{stroke-dasharray:100 100}100%{stroke-dasharray:100 100;opacity:0}}
.wv-weave{animation:wv-weave 2.8s cubic-bezier(.45,0,.3,1) infinite}
@keyframes hm-glint{0%,100%{transform:translateX(0)}50%{transform:translateX(6.3px)}}
.hm-glint{animation:hm-glint 2.2s ease-in-out infinite}
@media (prefers-reduced-motion: reduce){.kn-flow,.ow-look,.wv-weave,.hm-glint{animation:none}}
.still .kn-flow,.still .ow-look,.still .wv-weave,.still .hm-glint{animation:none}
@media (max-width:640px){.wrap{padding:28px 16px 40px}.specs{grid-template-columns:1fr}
  .bars{overflow-x:auto}.bars .row,.colhead{min-width:880px}header h1{font-size:32px}}
"""

JS = r"""
(function(){
  var h = (location.hash || "").slice(1).split(",");          // #dark, #light, #dark,still
  if (h[0] === "dark" || h[0] === "light") document.documentElement.setAttribute("data-theme", h[0]);
  if (h.indexOf("still") >= 0) document.documentElement.classList.add("still");   // key frames only
  function raster(c){
    var px = +c.dataset.px, col = getComputedStyle(c).color;
    var t = document.querySelector('#templates svg[data-key="' + c.dataset.svg + '"]').cloneNode(true);
    t.setAttribute("width", px); t.setAttribute("height", px);
    var src = new XMLSerializer().serializeToString(t);   // the serialiser adds the SVG namespace
    var img = new Image();
    img.onload = function(){
      c.width = px; c.height = px;
      var x = c.getContext("2d");
      x.clearRect(0, 0, px, px);
      x.drawImage(img, 0, 0, px, px);
      x.globalCompositeOperation = "source-in";   // a template image: keep the alpha, take the bar's colour
      x.fillStyle = col; x.fillRect(0, 0, px, px);
      x.globalCompositeOperation = "source-over";
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(src);
  }
  function all(){ document.querySelectorAll("canvas[data-svg]").forEach(raster); }
  all();
  try { matchMedia("(prefers-color-scheme: dark)").addEventListener("change", all); } catch (e) {}
})();
"""


def bar(theme, key, state, fn, scale, uid):
    if scale == 1:   # rasterised at 22 px, as a non-Retina display draws it
        icon = f'<canvas width="22" height="22" data-svg="{key}-{state}" data-px="22" aria-label="{key} {state}"></canvas>'
    else:            # vector at 22 pt: 44 device px on a Retina display
        icon = svg(fn(state, uid), label=f"{key} {state}")
    return (f'<div class="desk {theme}"><div class="mb"><span class="it ours">{icon}</span>'
            f'<span class="it">{WIFI}</span><span class="it">{BATTERY}</span>'
            f'<span class="clock">Mon 9 Nov&#8194;18:34</span></div></div>')


def board(files):
    out = []
    for i, (key, name, fn, idea, how) in enumerate(DIRECTIONS, 1):
        specs = "".join(
            f'<div class="spec">{svg(fn(s, f"{key}-{s}-big"), 66, "big", f"{name}, {STATE_NAMES[s]}")}'
            f'<canvas class="loupe" width="22" height="22" data-svg="{key}-{s}" data-px="22"'
            f' aria-label="{name} {STATE_NAMES[s]} at 1x, magnified"></canvas>'
            f'<div class="lbl"><b>{STATE_NAMES[s]}</b>{"animated" if s == "working" else "&nbsp;"}</div></div>'
            for s in STATES)
        rows = []
        for theme in ("light", "dark"):
            for scale in (1, 2):
                cells = "".join(bar(theme, key, s, fn, scale, f"{key}-{s}-{theme}{scale}") for s in STATES)
                rows.append(f'<div class="row"><div class="tag">{theme}<span>{scale}x</span></div>{cells}</div>')
        heads = "".join(f"<span>{STATE_NAMES[s]}</span>" for s in STATES)
        out.append(
            f'<section class="dir" id="{key}"><div class="head"><span class="num">0{i}</span>'
            f'<h2 class="serif">{name}</h2><p class="idea">{html.escape(idea)}</p><p class="how">{html.escape(how)}</p></div>'
            f'<div class="body"><div><div class="specs">{specs}</div></div>'
            f'<div class="bars"><div class="colhead"><span></span>{heads}</div>{"".join(rows)}</div></div></section>')
    templates = "".join(files.values())
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Menu bar icons</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1 class="serif">athena <em>&middot;</em> menu bar icon</h1>
    <p>Four directions for the status item, each in the four states the app needs. Pick one; phase 2 builds it.
    Each glyph is a one-colour template image on a 22&nbsp;px canvas, so macOS tints it for light and dark bars.</p>
  </div>
  <div class="legend"><span><b>Big</b> vector at 3x</span><span><b>Small</b> the 1x raster, magnified</span>
  <span><b>Bars</b> 1x rasterised at 22&nbsp;px, 2x vector</span></div>
</header>
{"".join(out)}
<footer>
  <span>Glyphs: <code>assets/menubar/&lt;direction&gt;-&lt;state&gt;.svg</code>, black on transparent, ready to load as template images.</span>
  <span>Rebuild with <code>python3 assets/src/menubar.py</code>. Add <code>#dark</code> or <code>#light</code> to the URL to force a theme, and <code>,still</code> to stop the motion.</span>
</footer>
</div>
<div id="templates" hidden>{templates}</div>
<script>{JS}</script>
</body>
</html>
"""


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "board").mkdir(exist_ok=True)
    files = {}
    for key, _name, fn, _idea, _how in DIRECTIONS:
        for s in STATES:
            (OUT / f"{key}-{s}.svg").write_text(file_svg(key, s, fn) + "\n")
            files[f"{key}-{s}"] = file_svg(key, s, fn, ns=False)
    (OUT / "board" / "index.html").write_text(board(files))
    print(f"wrote {len(files)} glyphs and board/index.html to {OUT}")


if __name__ == "__main__":
    main()
