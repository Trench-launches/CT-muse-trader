"""Draws VIGIL's brand art: the orb (avatar, favicons) and the orb rising over the horizon (link card, X banner).

The orb is a black disc with a thin white rim and a cool-white corona, brightest at the bottom where the horizon
light hits it. The website draws the same orb in SVG; this script makes the raster copies.

Run from the repo root:  .venv/Scripts/python.exe scripts/make_brand.py
Writes web/favicon.ico, web/assets/icon-{16,32,48,64,180}.png, web/assets/og-vigil.png, brand/x-avatar.png, brand/x-banner.png.
Text uses Segoe UI from C:/Windows/Fonts (falls back to Pillow's built-in font elsewhere).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
GLOW = (222, 230, 255)  # the cool white of the light
FONTS = Path("C:/Windows/Fonts")


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.load_default(size)


def ring(size, cx, cy, r, width, blur=0.0) -> Image.Image:
    """A circle outline as a light mask, optionally blurred into a glow."""
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r, cy + r), outline=255, width=max(1, round(width)))
    return m.filter(ImageFilter.GaussianBlur(blur)) if blur else m


def vramp(size, y0, y1, a, b) -> Image.Image:
    """Vertical ramp mask: a at y0, b at y1, held flat outside."""
    w, h = size
    col = Image.new("L", (1, h))
    col.putdata([round(a + (b - a) * min(1.0, max(0.0, (y - y0) / max(1, y1 - y0)))) for y in range(h)])
    return col.resize(size)


def hfade(size, lo=40, power=1.6) -> Image.Image:
    """Horizontal mask: full in the middle, fading to `lo` at both edges."""
    w, h = size
    row = Image.new("L", (w, 1))
    row.putdata([round(lo + (255 - lo) * (1 - abs(x - w / 2) / (w / 2)) ** power) for x in range(w)])
    return row.resize(size)


def light(base: Image.Image, mask: Image.Image, strength=1.0, color=GLOW) -> Image.Image:
    """Screen `color` onto base wherever the mask is lit."""
    if strength != 1:
        mask = mask.point(lambda v: min(255, round(v * strength)))
    lit = Image.composite(Image.new("RGB", base.size, color), Image.new("RGB", base.size, 0), mask)
    return ImageChops.screen(base, lit)


def orb(img: Image.Image, cx, cy, r, small=False, rim_w=None) -> Image.Image:
    """The VIGIL orb: corona, black core, then a crisp rim lit from below. rim_w: rim width as a share of r."""
    if small:  # favicons: one tight glow and a thick rim, so it still reads at 16px
        img = light(img, ring(img.size, cx, cy, r * 1.02, r * .14, blur=r * .16), 1.25)
    else:
        img = light(img, ring(img.size, cx, cy, r * 1.05, r * .12, blur=r * .34), 1.5)
        img = light(img, ring(img.size, cx, cy, r * 1.01, r * .05, blur=r * .09), 1.4)
    ImageDraw.Draw(img).ellipse((cx - r * .985, cy - r * .985, cx + r * .985, cy + r * .985), fill=(0, 0, 0))
    rim = ring(img.size, cx, cy, r, r * (rim_w or (.085 if small else .03)))
    rim = ImageChops.multiply(rim, vramp(img.size, cy - r, cy + r, 190 if small else 120, 255))
    img = light(img, rim, 1.0, (255, 255, 255))
    if not small:  # a faint inner rim, so the core reads as a sphere and not a hole
        img = light(img, ring(img.size, cx, cy, r * .93, r * .025, blur=r * .02), .16)
    return img


def horizon(img: Image.Image, apex_y, sag, glow=1.0) -> tuple[Image.Image, tuple[float, float, float]]:
    """The atmosphere above a planet's edge, brightest in the middle. Returns the planet circle for `planet`."""
    w, h = img.size
    big_r = (w / 2) ** 2 / (2 * sag) + sag / 2  # circle through the apex and both lower corners
    cx, cy = w / 2, apex_y + big_r
    fade = hfade(img.size)
    img = light(img, ImageChops.multiply(ring(img.size, cx, cy, big_r, h * .02, blur=h * .09), fade), 1.3 * glow)
    img = light(img, ImageChops.multiply(ring(img.size, cx, cy, big_r, h * .008, blur=h * .02), fade), 1.2 * glow)
    return img, (cx, cy, big_r)


def planet(img: Image.Image, circle, edge=1.0) -> Image.Image:
    """The dark planet in front (it hides the bottom of a rising orb), with a crisp lit edge."""
    cx, cy, big_r = circle
    ImageDraw.Draw(img).ellipse((cx - big_r, cy - big_r, cx + big_r, cy + big_r), fill=(0, 0, 0))
    line = ImageChops.multiply(ring(img.size, cx, cy, big_r, max(2, img.size[1] * .0035)), hfade(img.size, lo=0, power=1.2))
    return light(img, line, edge, (255, 255, 255))


def tracked(d: ImageDraw.ImageDraw, cx, baseline, text, f, fill, tracking) -> None:
    """Centred text with letter-spacing (a share of the font size)."""
    gap = f.size * tracking
    widths = [f.getlength(c) for c in text]
    x = cx - (sum(widths) + gap * (len(text) - 1)) / 2
    for c, cw in zip(text, widths):
        d.text((x, baseline), c, font=f, fill=fill, anchor="ls")
        x += cw + gap


def render(size, ss, draw) -> Image.Image:
    """Draw at `ss` times the size, then scale down (smooth edges everywhere)."""
    big = draw(Image.new("RGB", (size[0] * ss, size[1] * ss), (0, 0, 0)), ss)
    return big.resize(size, Image.LANCZOS)


def icon(px: int, round_mask=True) -> Image.Image:
    small = px <= 48
    img = render((px, px), 8, lambda im, s: orb(im, px * s / 2, px * s / 2, px * s * (.33 if small else .3), small=small,
                                                         rim_w=.17 if px <= 16 else .11 if px <= 32 else None))
    if not round_mask:
        return img
    mask = render((px, px), 8, lambda im, s: (ImageDraw.Draw(im).ellipse((0, 0, px * s - 1, px * s - 1), fill=(255, 255, 255)), im)[1])
    out = img.convert("RGBA")
    out.putalpha(mask.convert("L"))
    return out


def og(ss=2) -> Image.Image:
    def draw(im, s):
        w, h = im.size
        im, circle = horizon(im, h * .76, h * .22)
        im = orb(im, w / 2, h * .70, h * .15)
        im = planet(im, circle)
        d = ImageDraw.Draw(im)
        tracked(d, w / 2, h * .27, "VIGIL", font("seguisb.ttf", 92 * s), (250, 250, 250), .34)
        tracked(d, w / 2, h * .35, "POWERED BY GROK", font("segoeui.ttf", 20 * s), (150, 150, 150), .32)
        tracked(d, w / 2, h * .45, "Watches every memecoin launch on Solana. Trades in public, losses included.",
                font("segoeuisl.ttf", 25 * s), (205, 205, 205), 0)
        return im
    return render((1200, 630), ss, draw)


def banner(ss=2) -> Image.Image:
    def draw(im, s):
        w, h = im.size
        im, circle = horizon(im, h * .84, h * .30, glow=.9)
        im = orb(im, w * .5, h * .80, h * .11)
        im = planet(im, circle, .9)
        d = ImageDraw.Draw(im)
        tracked(d, w * .5, h * .50, "VIGIL", font("seguisb.ttf", 88 * s), (250, 250, 250), .34)
        tracked(d, w * .5, h * .62, "POWERED BY GROK", font("segoeui.ttf", 21 * s), (150, 150, 150), .32)
        return im
    return render((1500, 500), ss, draw)


def avatar() -> Image.Image:
    return render((400, 400), 4, lambda im, s: orb(im, 200 * s, 200 * s, 116 * s))


def main() -> None:
    web, brand = ROOT / "web", ROOT / "brand"
    (web / "assets").mkdir(parents=True, exist_ok=True)
    brand.mkdir(exist_ok=True)
    for px in (16, 32, 48, 64):
        icon(px).save(web / "assets" / f"icon-{px}.png")
    icon(180, round_mask=False).save(web / "assets" / "icon-180.png")  # iOS rounds the corners itself
    ico = [icon(px) for px in (16, 32, 48)]
    ico[-1].save(web / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)], append_images=ico[:-1])
    og().save(web / "assets" / "og-vigil.png", optimize=True)  # a new name, so X drops its cached link card
    avatar().save(brand / "x-avatar.png", optimize=True)
    banner().save(brand / "x-banner.png", optimize=True)
    print("wrote favicon.ico, icon-16/32/48/64/180.png, og-vigil.png, brand/x-avatar.png, brand/x-banner.png")


if __name__ == "__main__":
    main()
