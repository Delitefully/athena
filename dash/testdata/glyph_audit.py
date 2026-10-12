"""Measure the row glyphs on a running dash's /glyphs page, and optionally screenshot it and the dash.

    python3 dash/testdata/glyph_audit.py http://127.0.0.1:PORT [SHOTS_DIR]

Run it against a dash on a throwaway state dir (`dash/testdata/fresh.py`), never the real one. Each glyph's ink,
stroke included, must fill the 16 px square (its larger side 16, centred on 0,0), and every glyph must sit at the
same offset from its first line's cap-height centre. With SHOTS_DIR it saves glyphs-{light,dark}.png (2x) and
dash-{light,dark}.png. Needs Playwright and Chrome; `make test` does not run it.
"""
import sys

from playwright.sync_api import sync_playwright

MEASURE = r"""
() => [...document.querySelectorAll('li.item')].map(li => {
  const svg = li.querySelector('svg.icon'), use = svg.querySelector('use');
  const box = use.getBBox(), rect = svg.getBoundingClientRect(), k = rect.height / 24;
  const line = li.querySelector('.line, .note-text');
  const probe = document.createElement('span');
  probe.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
  line.prepend(probe);
  const baseline = probe.getBoundingClientRect().top;
  probe.remove();
  const cs = getComputedStyle(line), ctx = document.createElement('canvas').getContext('2d');
  ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  const capCentre = baseline - ctx.measureText('H').actualBoundingBoxAscent / 2;
  const stroke = svg.classList.contains('badge') ? 0 : parseFloat(getComputedStyle(svg).strokeWidth);
  return {id: use.getAttribute('href').slice(3), w: box.width + stroke, h: box.height + stroke,
          cx: box.x + box.width / 2, cy: box.y + box.height / 2,
          offset: rect.top + (box.y + 12 + box.height / 2) * k - capCentre};
})
"""


def main(base, shots=None):
    bad = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1480, "height": 1000})
        page.goto(base + "/glyphs")
        page.wait_for_timeout(800)
        rows = page.evaluate(MEASURE)
        offsets = {round(r["offset"], 2) for r in rows}
        for r in rows:
            ok = max(r["w"], r["h"]) == 16 and r["w"] <= 16 and r["h"] <= 16 and abs(r["cx"]) <= .25 and abs(r["cy"]) <= .25
            print(f'{r["id"]:<8} ink {r["w"]:5.2f} x {r["h"]:5.2f}  centre {r["cx"]:+.2f},{r["cy"]:+.2f}  '
                  f'off the cap-height centre {r["offset"]:+.2f} px  {"ok" if ok else "OUTLIER"}')
            if not ok:
                bad.append(r["id"])
        print(f"{len(rows)} glyphs, offsets {sorted(offsets)}")
        if len(offsets) != 1:
            bad.append("offsets")
        if shots:
            for scheme in ("light", "dark"):
                sheet = browser.new_page(viewport={"width": 520, "height": 900}, device_scale_factor=2)
                sheet.goto(f"{base}/glyphs?scheme={scheme}")
                sheet.wait_for_timeout(800)
                sheet.locator("section.group").screenshot(path=f"{shots}/glyphs-{scheme}.png")
                dash = browser.new_page(viewport={"width": 1480, "height": 1100})
                dash.goto(f"{base}/?scheme={scheme}")
                dash.wait_for_timeout(1500)
                dash.screenshot(path=f"{shots}/dash-{scheme}.png", full_page=True)
        browser.close()
    if bad:
        sys.exit("outliers: " + " ".join(bad))


if __name__ == "__main__":
    main(*sys.argv[1:3])
