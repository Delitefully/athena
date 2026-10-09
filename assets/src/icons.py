"""The watch.svg event icons: one family, stroke 2 with round caps and joins, drawn as paths.

Routine progress is an outline icon on a 16 px grid, centred on 0,0, drawn in muted on a ground-coloured
bead. An event athena acts on is a badge: a solid ink disc with its glyph knocked out in the ground colour,
a little larger, so the eye finds it first. Strokes inherit from the <use>.
"""

BEAD = 9      # ground-coloured backing of a routine icon; cuts the lane rule
DISC = 10     # ink disc of a badge
HALO = 12     # ground-coloured backing of a badge

ROUTINE = {
    "pr":     '<circle cx="-4.5" cy="-4.5" r="1.75"/><circle cx="-4.5" cy="4.5" r="1.75"/><circle cx="4.5" cy="4.5" r="1.75"/>'
              '<path d="M-4.5 -2.75V2.75M4.5 2.75V-1.5Q4.5 -4.5 1.5 -4.5H.5M2.5 -6.5L.5 -4.5L2.5 -2.5"/>',
    "ok":     '<circle r="6.25"/><path d="M-2.75 .25L-.75 2.25L3 -1.75"/>',
    "review": '<path d="M-6.5 -5.5H6.5V3H-.5L-4 6.25V3H-6.5ZM-3.25 -1.25H3.25"/>',
    "rebase": '<path d="M-5.5 -1A5.5 5.5 0 0 1 4.6 -3M5.5 1A5.5 5.5 0 0 1 -4.6 3M4.6 -6.5V-3H1.1M-4.6 6.5V3H-1.1"/>',
    "ready":  '<path d="M-7.25 .25L-4 3.5L2.25 -3.25M-.5 2.75L.25 3.5L6.5 -3.25"/>',
    "merged": '<path d="M-7 -5C-2.5 -5 -2.5 0 1.5 0M-7 5C-2.5 5 -2.5 0 1.5 0"/><circle cx="4.5" r="2.25" fill="currentColor"/>',
}
BADGE = {
    "blocked": '<path d="M0 -5V.75"/><circle cy="4.25" r="1.3" style="fill:var(--bg);stroke:none"/>',
    "fail":    '<path d="M-3.25 -3.25L3.25 3.25M3.25 -3.25L-3.25 3.25"/>',
    "done":    '<path d="M-3.25 5.25V-5M-3.25 -4.75H4L2 -1.75L4 1.25H-3.25"/>',
    "main":    '<circle cx="-3.75" r="1.75" style="fill:var(--bg);stroke:none"/><path d="M-.75 0H5M2.5 -2.5L5 0L2.5 2.5"/>',
}
NAMES = {"pr": "PR opened", "ok": "checks green", "review": "review comments", "rebase": "rebase",
         "ready": "ready to merge", "merged": "merged", "blocked": "blocked", "fail": "CI failed",
         "done": "done", "main": "main moved"}
ORDER = list(ROUTINE) + list(BADGE)


def defs(only=None):
    out = []
    for k, glyph in ROUTINE.items():
        if only is None or k in only:
            out.append(f'<g id="i-{k}"><circle r="{BEAD}" style="fill:var(--bg);stroke:none"/>{glyph}</g>')
    for k, glyph in BADGE.items():
        if only is None or k in only:
            out.append(f'<g id="i-{k}"><circle r="{HALO}" style="fill:var(--bg);stroke:none"/>'
                       f'<circle r="{DISC}" fill="currentColor" stroke="none"/><g style="stroke:var(--bg)">{glyph}</g></g>')
    return "".join(out)
