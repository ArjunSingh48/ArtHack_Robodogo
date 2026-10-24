"""Memory cards and diptychs: a quiet exhibition-label layout around the scene.

Cream paper, a single lavender accent, generous whitespace, the image untouched.
Paintings and memes get the same typography and the same care. When a meme's image
may not be reproduced, a typographic reference panel stands in its place.
Credits are always included. The diptych (memory | attempt) is made only after the
visitor consents, in memory, and is never written to disk.
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


def meta_line(scene) -> str:
    if getattr(scene, "kind", "art") == "meme":
        return scene.attribution
    return scene.artist + (f", {scene.date}" if scene.date else "")


KIND_LABEL = {"art": "FROM THE MUSEUM", "meme": "FROM THE INTERNET"}


def reference_panel(size: tuple[int, int], scene, *, font_path: str = "", note: str = "image not reproduced — see source") -> Image.Image:
    """A typographic stand-in for an image we may not reproduce."""
    w, h = size
    panel = Image.new("RGB", (w, h), (240, 234, 244))
    d = ImageDraw.Draw(panel)
    d.rectangle([0, 0, w - 1, h - 1], outline=LAVENDER, width=2)
    name_f = _font(ITALIC_CANDIDATES, max(28, w // 14), font_path)
    body_f = _font(SERIF_CANDIDATES, max(18, w // 32), font_path)
    small = _font(SANS_CANDIDATES, max(15, w // 44))
    lines = wrap_px(d, scene.title, name_f, w - 80, 2)
    y = h // 2 - 40 * len(lines)
    for line in lines:
        d.text(((w - d.textlength(line, font=name_f)) / 2, y), line, font=name_f, fill=INK)
        y += int(name_f.size * 1.25)
    sub = f"{getattr(scene, 'subject', scene.artist)} · {scene.date}"
    for line in wrap_px(d, sub, body_f, w - 80, 2):
        d.text(((w - d.textlength(line, font=body_f)) / 2, y + 12), line, font=body_f, fill=MUTED)
        y += int(body_f.size * 1.3)
    d.text(((w - d.textlength(note, font=small)) / 2, h - 48), note, font=small, fill=LAVENDER)
    return panel


def ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def render_diptych(reference: Image.Image | None, attempt: Image.Image, scene, *, caption: str, session_id: str,
                   labels: tuple[str, str] = ("the memory", "the attempt"), when: datetime | None = None,
                   font_path: str = "") -> bytes:
    """Scene (or its reference panel) beside a consented photo of the robot's imitation."""
    when = when or datetime.now()
    W2, H2, M = 1680, 1120, 80
    card = Image.new("RGB", (W2, H2), CREAM)
    d = ImageDraw.Draw(card)
    label = _font(SANS_CANDIDATES, 20)
    small = _font(SANS_CANDIDATES, 19)
    cap_f = _font(ITALIC_CANDIDATES, 30, font_path)
    _spaced(d, (M, 50), "IS THIS US?  ·  " + KIND_LABEL.get(getattr(scene, "kind", "art"), ""), label, LAVENDER, spacing=4)
    stamp = f"{when:%Y.%m.%d}  ·  {session_id}"
    d.text((W2 - M - d.textlength(stamp, font=small), 52), stamp, font=small, fill=MUTED)
    box_w, box_h, top = (W2 - 3 * M) // 2, 760, 110
    left = fit(trim_border(reference), box_w, box_h) if reference is not None else reference_panel((box_w, box_h), scene, font_path=font_path)
    right = fit(attempt.convert("RGB"), box_w, box_h)
    for i, (img, text) in enumerate(((left, labels[0]), (right, labels[1]))):
        x0 = M + i * (box_w + M)
        card.paste(img, (x0 + (box_w - img.width) // 2, top + (box_h - img.height) // 2))
        _spaced(d, (x0, top + box_h + 18), text.upper(), small, MUTED, spacing=3)
    y = top + box_h + 70
    d.line([(M, y), (W2 - M, y)], fill=HAIRLINE, width=1)
    d.line([(M, y), (M + 48, y)], fill=LAVENDER, width=3)
    for line in wrap_px(d, caption, cap_f, W2 - 2 * M, 2):
        y += 22
        d.text((M, y), line, font=cap_f, fill=LAVENDER)
        y += 20
    credit = f"{scene.title} — {meta_line(scene)} · {scene.museum} · {scene.page_url}"
    d.text((M, H2 - 50), ellipsize(d, credit, small, W2 - 2 * M), font=small, fill=MUTED)
    out = BytesIO()
    card.save(out, format="JPEG", quality=90)
    return out.getvalue()


def render_memory_card(painting: Image.Image | None, artwork: Artwork, *, caption: str, session_id: str,
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
    _spaced(d, (MARGIN, 58), "IS THIS US?  ·  " + KIND_LABEL.get(getattr(artwork, "kind", "art"), ""),
            label, LAVENDER, spacing=4)
    stamp = f"{when:%Y.%m.%d}  ·  {session_id}"
    d.text((W - MARGIN - d.textlength(stamp, font=small), 60), stamp, font=small, fill=MUTED)

    # painting
    box_w, box_h = W - 2 * MARGIN, 820
    img = (fit(trim_border(painting), box_w, box_h) if painting is not None
           else reference_panel((box_w, int(box_h * 0.8)), artwork, font_path=font_path))
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
    meta = meta_line(artwork)
    for line in wrap_px(d, meta, body_f, text_w, 2):
        d.text((MARGIN, y + 4), line, font=body_f, fill=INK)
        y += 36
    d.text((MARGIN, y + 6), artwork.museum, font=body_f, fill=MUTED)
    y += 54

    for line in wrap_px(d, caption, cap_f, text_w, 3):
        d.text((MARGIN, y), line, font=cap_f, fill=LAVENDER)
        y += 38

    credit = f"{artwork.rights.status} · {artwork.page_url}"
    d.text((MARGIN, H - 64), ellipsize(d, credit, small, W - 2 * MARGIN), font=small, fill=MUTED)

    out = BytesIO()
    card.save(out, format="JPEG", quality=90)
    return out.getvalue()
