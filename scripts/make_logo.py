"""Draws VIGIL's logo and X profile art from the character cut-outs in web/assets.

The logo is VIGIL's head (the robot with the headset) with the name beside it, wide-tracked like the site's
wordmark. The X banner puts the waving VIGIL on the horizon with the name and the one-line pitch.

Run from the repo root:  .venv/Scripts/python.exe scripts/make_logo.py
Writes brand/vigil-logo.png (transparent, white text), brand/vigil-logo-dark.png (transparent, dark text),
brand/vigil-logo-on-black.png, brand/vigil-mark.png (transparent),
brand/x-avatar.png (400x400) and brand/x-banner.png (1500x500).
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

from make_brand import GLOW, ROOT, font, horizon, light, planet, render, ring, tracked

ASSETS = ROOT / "web" / "assets"
WHITE, GREY = (250, 250, 250), (150, 150, 150)


def cutout(name: str, height: int) -> Image.Image:
    im = Image.open(ASSETS / name).convert("RGBA")
    return im.resize((round(im.width * height / im.height), height), Image.LANCZOS)


def glow_behind(img: Image.Image, cx, cy, r, strength=0.55) -> Image.Image:
    """A soft cool light behind the character, so the black of the headset doesn't vanish into the background."""
    return light(img, ring(img.size, cx, cy, r * .5, r, blur=r * .6), strength, GLOW)


def shadow_paste(base: Image.Image, fig: Image.Image, x: int, y: int, blur: float) -> None:
    """Paste a cut-out with a soft drop shadow under it (as the site does with drop-shadow)."""
    sh = Image.new("RGBA", fig.size, (0, 0, 0, 0))
    sh.putalpha(fig.getchannel("A").point(lambda v: round(v * .8)))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(sh, (x, y + round(blur * .8)))
    base.alpha_composite(fig, (x, y))


def mark(size=640) -> Image.Image:
    """The head alone on a transparent square."""
    head = cutout("vigil-head.webp", round(size * .80))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(head, ((size - head.width) // 2, (size - head.height) // 2))
    return out


def avatar(px=400) -> Image.Image:
    """X shows the avatar as a circle: the head sits well inside it, lit softly from behind."""
    bg = render((px, px), 2, lambda im, s: glow_behind(im, px * s / 2, px * s * .54, px * s * .42, .5)).convert("RGBA")
    head = cutout("vigil-head.webp", round(px * .62))
    shadow_paste(bg, head, (px - head.width) // 2, round(px * .21), px * .03)
    return bg.convert("RGB")


def lockup(on_black: bool, ink=WHITE, sub=GREY) -> Image.Image:
    """The head, then VIGIL, then "POWERED BY GROK" under the name. ink/sub: text colours (dark ones for light backgrounds)."""
    h = 360
    head = cutout("vigil-head.webp", round(h * .78))
    name_f, sub_f = font("seguisb.ttf", 150), font("segoeui.ttf", 34)
    gap = name_f.size * .34
    name_w = sum(name_f.getlength(c) for c in "VIGIL") + gap * 4
    w = round(head.width + 70 + name_w + 40)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 255 if on_black else 0))
    if on_black:
        out = glow_behind(out.convert("RGB"), head.width / 2 + 10, h / 2, h * .42, .45).convert("RGBA")
    if on_black:
        shadow_paste(out, head, 10, (h - head.height) // 2, 10)
    else:
        out.alpha_composite(head, (10, (h - head.height) // 2))
    d = ImageDraw.Draw(out)
    x0 = head.width + 70
    cx = x0 + name_w / 2
    tracked(d, cx, h * .60, "VIGIL", name_f, ink, .34)
    tracked(d, cx, h * .79, "POWERED BY GROK", sub_f, sub, .32)
    return out


def banner(ss=2) -> Image.Image:
    """1500x500. X covers the bottom-left with the avatar and crops the top and bottom on phones, so the
    name sits in the middle band and VIGIL stands to the right."""
    w, h = 1500, 500

    def draw(im, s):
        W, H = im.size
        im, circle = horizon(im, H * .88, H * .20, glow=.85)
        im = glow_behind(im, W * .755, H * .60, H * .40, .35)
        im = planet(im, circle, .9).convert("RGBA")
        fig = cutout("vigil-wave.webp", round(H * .74))  # clear of the top edge, which some phones crop
        shadow_paste(im, fig, round(W * .755 - fig.width / 2), round(H * .885 - fig.height), 14 * s)
        d = ImageDraw.Draw(im)
        cx = W * .455
        tracked(d, cx, H * .47, "VIGIL", font("seguisb.ttf", 104 * s), WHITE, .34)
        tracked(d, cx, H * .575, "POWERED BY GROK", font("segoeui.ttf", 20 * s), GREY, .32)
        tracked(d, cx, H * .675, "Watches every memecoin launch on Solana. Trades in public, losses included.",
                font("segoeuisl.ttf", 22 * s), (200, 200, 200), 0)
        return im.convert("RGB")

    return render((w, h), ss, draw)


def main() -> None:
    brand = ROOT / "brand"
    brand.mkdir(exist_ok=True)
    mark().save(brand / "vigil-mark.png", optimize=True)
    lockup(False).save(brand / "vigil-logo.png", optimize=True)
    lockup(True).convert("RGB").save(brand / "vigil-logo-on-black.png", optimize=True)
    lockup(False, (14, 14, 16), (110, 110, 116)).save(brand / "vigil-logo-dark.png", optimize=True)  # for light backgrounds
    avatar().save(brand / "x-avatar.png", optimize=True)
    banner().save(brand / "x-banner.png", optimize=True)
    print("wrote brand/vigil-mark.png, vigil-logo.png, vigil-logo-dark.png, vigil-logo-on-black.png, x-avatar.png, x-banner.png")


if __name__ == "__main__":
    main()
