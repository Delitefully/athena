"""Build assets/watch.svg: `python3 assets/src/gen.py [out.svg] [--layout layout.json]`.

Labels are Instrument Sans outlined to paths, kept in labels.json; icons come from icons.py.
--layout writes where every shape is over time, for the verification scan.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from icons import BEAD, HALO, ROUTINE, defs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = json.load(open(os.path.join(HERE, "labels.json")))


def lab(name):
    return LABELS[name]["d"]


def at(name):
    return LABELS[name]["x"], LABELS[name]["y"]


# ---- geometry
LANE_Y = [92, 142, 192, 242]
X0, X1, ATH = 150, 694, 726
FADE = 36                          # lane ends fade over this many px
V = [23, 46, 23, 46]               # px/s, the linear routine stream
P = [230, 460, 230, 460]           # period = V x 10 s, so the loop is seamless
DUR = 10.0
# routine stream: (u, icon) per lane; u is the screen x at t=0, repeated every P
STREAM = [
    [(58, "ok"), (175, "ready")],
    [(130, "pr"), (270, "ok"), (400, "review")],
    [(70, "rebase"), (178, "merged")],
    [(170, "rebase"), (295, "review"), (440, "merged")],
]
# events athena acts on: icon, lane, x in the still, when it appears, its label and athena's reply
EVENTS = [
    dict(n=0, icon="blocked", lane=1, xb=520, t0=1.0, lab="blocked", oxb=520, rep="asks you"),
    dict(n=1, icon="fail", lane=3, xb=532, t0=3.4, lab="CI failed", oxb=532, rep="sends it back"),
    dict(n=2, icon="done", lane=0, xb=470, t0=5.8, lab="done", oxb=470, rep="starts a review"),
]
SPAN = 2.5                          # seconds an event rides before it leaves
MX, MY = 622, 274                   # main moved: its own slot, reserved in every frame


def check_layout():
    gap = HALO + BEAD + 6
    for e in EVENTS:
        ln, p = e["lane"], P[e["lane"]]
        for u_ev in (e["xb"] % p, (e["xb"] - V[ln] * e["t0"]) % p):
            for u, _ in STREAM[ln]:
                assert min((u - u_ev) % p, (u_ev - u) % p) >= gap, ("event on a routine icon", e["icon"], u)
    for ln, s in enumerate(STREAM):
        us = sorted(u for u, _ in s)
        for a, b in zip(us, us[1:] + [us[0] + P[ln]]):
            assert b - a >= 100, ("routine icons too close", ln, a, b)
        for u, _ in s:                # the still: no icon straddles a lane end
            for k in range(-2, 4):
                x = u + k * P[ln]
                assert abs(x - X0) >= BEAD + 3 and abs(x - X1) >= BEAD + 3, ("icon on a lane end in the still", ln, x)


check_layout()

# ---- css
ARR, EXIT, OVER = "cubic-bezier(.16,1,.3,1)", "cubic-bezier(.7,0,.84,0)", "cubic-bezier(.34,1.4,.64,1)"


def f(x):
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def kf(name, steps):
    """steps: (seconds, declarations, easing to the next step or None)."""
    out = []
    for t, props, ease in steps:
        e = f"animation-timing-function:{ease}" if ease else ""
        out.append(f"{f(100 * t / DUR)}%{{{';'.join(x for x in (props, e) if x)}}}")
    return f"@keyframes {name}{{{''.join(out)}}}.{name}{{animation:{name} {f(DUR)}s infinite}}"


def appear(name, t_in, t_held, t_out, t_gone, hidden, shown, ease_in=ARR):
    return kf(name, [(0, hidden, None), (t_in, hidden, ease_in), (t_held, shown, None),
                     (t_out, shown, EXIT), (t_gone, hidden, None), (DUR, hidden, None)])


css = [f"@keyframes s{i}{{to{{transform:translateX({P[i]}px)}}}}.s{i}{{animation:s{i} {f(DUR)}s linear infinite}}"
       for i in range(4)]
body = []
LAB_IN, LAB_SHOWN = "opacity:0;transform:translateX(-6px)", "opacity:1;transform:none"
REP_IN = "opacity:0;transform:translateX(-8px)"
for e in EVENTS:
    n, ln, t0 = e["n"], e["lane"], e["t0"]
    v, y, xb = V[ln], LANE_Y[ln], e["xb"]
    t1, ride = t0 + SPAN, V[ln] * SPAN
    # ride at exactly the stream's speed, so the event sits still in its lane
    css.append(kf(f"g{n}", [(0, "transform:none", None), (t0, "transform:none", "linear"),
                            (t1, f"transform:translateX({f(ride)}px)", None), (t1 + .001, "transform:none", None),
                            (DUR, "transform:none", None)]))
    css.append(appear(f"p{n}", t0, t0 + .45, t1 - .25, t1, "transform:scale(0)", "transform:none", OVER))
    css.append(appear(f"l{n}", t0 + .15, t0 + .65, t1 - .5, t1 - .2, LAB_IN, LAB_SHOWN))
    # athena's reply: drawn from its line out to the event, then tracks it as it rides
    end = xb + HALO + 1
    length = ATH - end
    ta, tb, tc = t0 + .3, t0 + .8, t0 + 1.9

    def off(t):
        return v * (t - t0) / length
    css.append(kf(f"r{n}", [(0, "stroke-dashoffset:1.5", None), (ta, "stroke-dashoffset:1.5", ARR),
                            (tb, f"stroke-dashoffset:{f(off(tb))}", "linear"), (tc, f"stroke-dashoffset:{f(off(tc))}", EXIT),
                            (tc + .38, "stroke-dashoffset:1.5", None), (DUR, "stroke-dashoffset:1.5", None)]))
    css.append(appear(f"q{n}", t0 + .6, t0 + 1.1, tc, tc + .3, REP_IN, LAB_SHOWN))
    lx, _ = at(e["lab"])
    body.append(f'<g class="g{n}"><g class="p{n} eg"><use href="#i-{e["icon"]}" x="{xb}" y="{y}" class="i k"/></g>'
                f'<g class="l{n}"><path class="inkf" transform="translate({f(lx - e["oxb"] + xb)} {y - 18})" d="{lab(e["lab"])}"/></g></g>'
                f'<path class="rh r{n}" pathLength="1" d="M{ATH} {y}H{end}"/><path class="rch r{n}" pathLength="1" d="M{ATH} {y}H{end}"/>'
                f'<g class="q{n}"><path class="inkf" transform="translate({f(at(e["rep"])[0])} {f(at(e["rep"])[1])})" d="{lab(e["rep"])}"/></g>')

# main moved, then "all rebase": one stub per lane, staggered 80 ms upward from main, the cause
css.append(appear("p4", 8.0, 8.45, 9.4, 9.7, "transform:scale(0)", "transform:none", OVER))
css.append(appear("l4", 8.1, 8.5, 9.4, 9.7, LAB_IN, LAB_SHOWN))
for j in range(4):
    dt = (3 - j) * .08
    css.append(appear(f"r{4 + j}", 8.1 + dt, 8.5 + dt, 9.4 + dt, 9.7 + dt, "stroke-dashoffset:1.5", "stroke-dashoffset:0"))
css.append(appear("q4", 8.35, 8.8, 9.45, 9.75, REP_IN, LAB_SHOWN))
mlx, mly = at("main moved")
rbx, rby = at("all rebase")
body.append(f'<g class="p4 eg"><use href="#i-main" x="{MX}" y="{MY}" class="i k"/></g>'
            f'<g class="l4"><path class="inkf" transform="translate({f(mlx)} {f(mly)})" d="{lab("main moved")}"/></g>'
            + "".join(f'<path class="rch r{4 + j}" pathLength="1" d="M{ATH} {y}H700"/>' for j, y in enumerate(LANE_Y))
            + f'<g class="q4"><path class="inkf" transform="translate({f(rbx)} {f(rby)})" d="{lab("all rebase")}"/></g>')

# ---- lanes: the four lane labels share "abc-10", drawn once
lane_names = ["abc-101", "abc-102", "abc-103", "abc-104"]
subs = [re.findall(r"M[^M]+", lab(nm)) for nm in lane_names]
prefix = subs[0][:-1]
for s in subs:
    assert s[:-1] == prefix and len(s) == len(prefix) + 1, "lane labels must share the abc-10 prefix byte for byte"
lane_svg = []
for nm, s in zip(lane_names, subs):
    x, y = at(nm)
    lane_svg.append(f'<g class="mut" transform="translate({f(x)} {f(y)})"><use href="#abc"/><path d="{s[-1]}"/></g>')
streams = []
for i, y in enumerate(LANE_Y):
    lane_svg.append(f'<path class="lane" d="M{X0} {y}H{X1}"/>')
    uses = []
    for u, k in STREAM[i]:
        x = u
        while x > X0 - BEAD - P[i]:
            x -= P[i]
        x += P[i]
        while x <= X1 + BEAD:
            uses.append(f'<use href="#i-{k}" x="{x}" y="{y}"/>')
            x += P[i]
    streams.append(f'<g class="s{i}">{"".join(uses)}</g>')
# the stream fades in and out at the lane ends; the mask stays put while the stream moves under it
fo = FADE / (X1 - X0)
lane_svg.append(f'<g mask="url(#fade)" class="i m">{"".join(streams)}</g>')

STYLE = (":root{--bg:#F3EEE4;--ink:#1E2350;--thr:#B8321F;--mut:#5B5E78;--rule:#D9D1C1}"
         "@media (prefers-color-scheme: dark){:root{--bg:#14172E;--ink:#ECE5D6;--thr:#E0573D;--mut:#A3A3B8;--rule:#2C3150}}"
         ".bg{fill:var(--bg)}.mut{fill:var(--mut)}.inkf{fill:var(--ink)}.thrf{fill:var(--thr)}"
         ".lane{stroke:var(--rule);stroke-width:2;fill:none}"
         ".i{stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}.m{color:var(--mut)}.k{color:var(--ink)}"
         ".rch{stroke:var(--thr);stroke-width:3;fill:none;stroke-linecap:round;stroke-dasharray:1 2}"
         f".rh{{stroke:var(--bg);stroke-width:{2 * BEAD + 4};fill:none;stroke-dasharray:1 2}}"
         ".wl{stroke:var(--thr);stroke-width:5;stroke-linecap:round;fill:none}"
         ".eg{transform-box:fill-box;transform-origin:center}"
         "@media (prefers-reduced-motion: no-preference){" + "".join(css) + "}")
DESC = ("athena watch streams every change from four workers. Routine progress flows by quietly as small muted icons: "
        "PR opened, checks green, review comments, rebase, ready to merge, merged. athena acts only on the few that matter, "
        "drawn as solid badges: a blocked worker (asks you), a failed check (sends it back), a finished worker "
        "(starts a review) and main moving (all rebase).")
MASK = (f'<linearGradient id="fg" gradientUnits="userSpaceOnUse" x1="{X0}" x2="{X1}" y1="0" y2="0">'
        f'<stop offset="0" stop-color="#000"/><stop offset="{f(fo)}" stop-color="#fff"/>'
        f'<stop offset="{f(1 - fo)}" stop-color="#fff"/><stop offset="1" stop-color="#000"/></linearGradient>'
        f'<mask id="fade" maskUnits="userSpaceOnUse" x="{X0}" y="60" width="{X1 - X0}" height="210">'
        f'<rect x="{X0}" y="60" width="{X1 - X0}" height="210" fill="url(#fg)"/></mask>')
tx, ty = at("athena watch")
svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 320" width="960" height="320" role="img" aria-labelledby="t d">'
       '<title id="t">athena: signal, not noise</title>'
       f'<desc id="d">{DESC}</desc><style>{STYLE}</style>'
       '<rect class="bg" width="960" height="320" rx="18"/>'
       f'<defs>{MASK}<path id="abc" d="{"".join(prefix)}"/>{defs()}</defs>'
       + "".join(lane_svg)
       + f'<path class="thrf" transform="translate({f(tx)} {f(ty)})" d="{lab("athena watch")}"/>'
       + "".join(body)
       + f'<path class="wl" d="M{ATH} 56V276"/></svg>')

args = sys.argv[1:]
layout_path = None
if "--layout" in args:
    i = args.index("--layout")
    layout_path = args[i + 1]
    del args[i:i + 2]
out = args[0] if args else os.path.join(HERE, "..", "watch.svg")
open(out, "w").write(svg)
print(out, len(svg.encode()), "bytes")

if layout_path:
    def bbox(d, x, y):
        n = list(map(float, re.findall(r"-?\d*\.?\d+", d)))
        return [x + min(n[0::2]), y + min(n[1::2]), x + max(n[0::2]), y + max(n[1::2])]
    moving = {e["lab"] for e in EVENTS}
    json.dump(dict(
        lanes=[dict(y=y, v=V[i], P=P[i], us=[u for u, _ in STREAM[i]]) for i, y in enumerate(LANE_Y)],
        clip=[X0, X1], fade=FADE, bead=BEAD, key=HALO, span=SPAN, ath=ATH, main=[MX, MY],
        events=[dict(lane=e["lane"], xb=e["xb"], t0=e["t0"],
                     label=bbox(lab(e["lab"]), at(e["lab"])[0] - e["oxb"] + e["xb"], LANE_Y[e["lane"]] - 18)) for e in EVENTS],
        texts=[bbox(lab(nm), *at(nm)) for nm in LABELS if nm not in moving],
        title=bbox(lab("athena watch"), tx, ty)), open(layout_path, "w"))
