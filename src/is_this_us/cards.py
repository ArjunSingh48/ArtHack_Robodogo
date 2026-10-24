"""Memory-card generator: a quiet exhibition-label layout around the painting.

Cream paper, a single lavender accent, generous whitespace, the painting untouched.
Credits are always included. (An opt-in diptych with a consented visitor photo is
planned for Phase 6 and intentionally not implemented here.)
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .domain import Artwork

CREAM = (247, 242, 233)
INK = (52, 47, 58)
MUTED = (128, 121, 132)
LAVENDER = (167, 150, 201)
HAIRLINE = (218, 210, 228)

W, H = 1080, 1440
MARGIN = 96

SERIF_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/Library/Fonts/Georgia.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]
ITALIC_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
    "C:/Windows/Fonts/georgiai.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
]
SANS_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(candidates: list[str], size: int, override: str = "") -> ImageFont.ImageFont:
    for path in ([override] if override else []) + candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _spaced(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, spacing: int = 3) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def trim_border(img: Image.Image, tolerance: int = 28) -> Image.Image:
    """Remove a uniform dark/light photographic border (common in museum web images)."""
    img = img.convert("RGB")
    corner = img.getpixel((0, 0))
    if not (max(corner) < 40 or min(corner) > 225):
        return img
    diff = ImageChops.difference(img, Image.new("RGB", img.size, corner)).convert("L")
    bbox = diff.point(lambda v: 255 if v > tolerance else 0).getbbox()
    if not bbox:
        return img
    w, h = img.size
    if (bbox[2] - bbox[0]) < 0.6 * w or (bbox[3] - bbox[1]) < 0.6 * h:
        return img  # would cut into the painting itself
    return img.crop(bbox)


def fit(img: Image.Image, box_w: int, box_h: int, max_upscale: float = 2.0) -> Image.Image:
    scale = min(box_w / img.width, box_h / img.height, max_upscale)
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)


def wrap_px(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,;") + "…"
    return lines


def render_memory_card(painting: Image.Image, artwork: Artwork, *, caption: str, session_id: str,
                       when: datetime | None = None, font_path: str = "") -> bytes:
    when = when or datetime.now()
    card = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(card)

    label = _font(SANS_CANDIDATES, 20)
    small = _font(SANS_CANDIDATES, 19)
    title_f = _font(ITALIC_CANDIDATES, 38, font_path)
    body_f = _font(SERIF_CANDIDATES, 26, font_path)
    cap_f = _font(ITALIC_CANDIDATES, 28, font_path)

    # header
    _spaced(d, (MARGIN, 58), "IS THIS US?", label, LAVENDER, spacing=4)
    stamp = f"{when:%Y.%m.%d}  ·  {session_id}"
    d.text((W - MARGIN - d.textlength(stamp, font=small), 60), stamp, font=small, fill=MUTED)

    # painting
    box_w, box_h = W - 2 * MARGIN, 820
    img = fit(trim_border(painting), box_w, box_h)
    px = (W - img.width) // 2
    py = 120 + (box_h - img.height) // 2
    card.paste(img, (px, py))

    # hairline + accent
    y = 120 + box_h + 44
    d.line([(MARGIN, y), (W - MARGIN, y)], fill=HAIRLINE, width=1)
    d.line([(MARGIN, y), (MARGIN + 48, y)], fill=LAVENDER, width=3)
    y += 30

    # label text
    text_w = W - 2 * MARGIN
    for line in wrap_px(d, artwork.title, title_f, text_w, 2):
        d.text((MARGIN, y), line, font=title_f, fill=INK)
        y += 48
    meta = artwork.artist + (f", {artwork.date}" if artwork.date else "")
    for line in wrap_px(d, meta, body_f, text_w, 2):
        d.text((MARGIN, y + 4), line, font=body_f, fill=INK)
        y += 36
    d.text((MARGIN, y + 6), artwork.museum, font=body_f, fill=MUTED)
    y += 54

    for line in wrap_px(d, caption, cap_f, text_w, 3):
        d.text((MARGIN, y), line, font=cap_f, fill=LAVENDER)
        y += 38

    credit = f"{artwork.rights.status} · {artwork.page_url}"
    d.text((MARGIN, H - 64), credit[:110], font=small, fill=MUTED)

    out = BytesIO()
    card.save(out, format="JPEG", quality=90)
    return out.getvalue()
