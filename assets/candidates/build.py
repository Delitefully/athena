"""Build logo-mark, light/dark lockups and the social card from one candidate.

usage: build.py <candidate.png> <outdir> [bg.png]
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT = os.path.expanduser('~/Library/Fonts/FiraCodeNerdFontMono-SemiBold.ttf')
FONT_REG = os.path.expanduser('~/Library/Fonts/FiraCodeNerdFontMono-Regular.ttf')
SLATE = (46, 56, 66)
TEAL = (43, 165, 140)
GREY = (138, 150, 163)
LIFT = (138, 150, 163)  # slate parts on a dark page
TEXT_LIGHT = (46, 56, 66)
TEXT_DARK = (236, 238, 240)


def trim(im):
    return im.crop(im.split()[3].point(lambda a: 255 if a > 8 else 0).getbbox())


def lift(im):
    """Recolour slate pixels to LIFT so the mark survives a dark background."""
    px = im.load()
    out = im.copy()
    po = out.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            d = lambda c: (r - c[0]) ** 2 + (g - c[1]) ** 2 + (b - c[2]) ** 2
            if d(SLATE) < d(TEAL) and d(SLATE) < d(GREY):
                po[x, y] = LIFT + (a,)
    return out


def fit(im, size):
    w, h = im.size
    k = size / max(w, h)
    return im.resize((round(w * k), round(h * k)), Image.LANCZOS)


def mark_png(m, out):
    m = fit(m, 420)
    c = Image.new('RGBA', (512, 512), (0, 0, 0, 0))
    c.paste(m, ((512 - m.width) // 2, (512 - m.height) // 2), m)
    c.save(out)


def lockup(m, color, out, word='athena'):
    H, margin, mh, gap = 490, 75, 340, 64
    m = fit(m, mh)
    font = ImageFont.truetype(FONT, 190)
    tb = font.getbbox(word)
    tw = tb[2] - tb[0]
    W = margin + m.width + gap + tw + margin
    c = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    c.paste(m, (margin, (H - m.height) // 2), m)
    d = ImageDraw.Draw(c)
    th = tb[3] - tb[1]
    d.text((margin + m.width + gap - tb[0], (H - th) // 2 - tb[1]), word, font=font, fill=color + (255,))
    c.save(out)
    return c


def social(dark_lockup, bg, out, tagline):
    W, H = 1280, 640
    if bg:
        b = Image.open(bg).convert('RGB')
        k = max(W / b.width, H / b.height)
        b = b.resize((round(b.width * k), round(b.height * k)), Image.LANCZOS)
        b = b.crop(((b.width - W) // 2, (b.height - H) // 2, (b.width - W) // 2 + W, (b.height - H) // 2 + H))
    else:
        b = Image.new('RGB', (W, H), (30, 36, 44))
    b = b.convert('RGBA')
    lk = dark_lockup.resize((720, round(dark_lockup.height * 720 / dark_lockup.width)), Image.LANCZOS)
    y = 172
    b.paste(lk, ((W - lk.width) // 2, y), lk)
    f = ImageFont.truetype(FONT_REG, 26)
    d = ImageDraw.Draw(b)
    tb = f.getbbox(tagline)
    d.text(((W - (tb[2] - tb[0])) // 2 - tb[0], y + lk.height + 4), tagline, font=f, fill=(160, 170, 180, 255))
    b.convert('RGB').save(out)


if __name__ == '__main__':
    src, outdir = sys.argv[1], sys.argv[2]
    bg = sys.argv[3] if len(sys.argv) > 3 else None
    m = trim(Image.open(src).convert('RGBA'))
    md = lift(m)
    mark_png(m, f'{outdir}/logo-mark.png')
    lockup(m, TEXT_LIGHT, f'{outdir}/logo-lockup.png')
    dl = lockup(md, TEXT_DARK, f'{outdir}/logo-lockup-dark.png')
    social(dl, bg, f'{outdir}/social-preview.png', 'a chief of staff for Claude Code in herdr')
