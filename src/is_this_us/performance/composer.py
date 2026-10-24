"""THEN / NOW / IS THIS US? — the exhibition print made with a participant.

Deterministic (same inputs → same pixels). Both images keep their proportions and are
never cropped; thin alignment marks sit at their corners. Museum-label typography,
warm ivory (default) or charcoal. 2400×1500 px: readable on a phone, sharp on a projector.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw

from ..cards import ITALIC_CANDIDATES, SANS_CANDIDATES, SERIF_CANDIDATES, _font, _spaced, ellipsize, fit, trim_border, wrap_px
from .catalog import ShowArtifact
from .pose_session import Comparison

THEMES = {
    "ivory": {"bg": (246, 241, 231), "ink": (45, 41, 47), "muted": (124, 116, 112), "accent": (150, 131, 186),
              "hair": (214, 205, 196), "mat": (238, 232, 221)},
    "charcoal": {"bg": (31, 29, 33), "ink": (238, 232, 222), "muted": (156, 149, 140), "accent": (183, 166, 214),
                 "hair": (70, 66, 74), "mat": (40, 38, 43)},
}
SIZE = (2400, 1500)


def _marks(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color, length: int = 22, gap: int = 12) -> None:
    x0, y0, x1, y1 = box
    for (cx, cy, sx, sy) in ((x0, y0, -1, -1), (x1, y0, 1, -1), (x0, y1, -1, 1), (x1, y1, 1, 1)):
        ox, oy = cx + sx * gap, cy + sy * gap
        d.line([(ox, oy), (ox - sx * length, oy)], fill=color, width=2)
        d.line([(ox, oy), (ox, oy - sy * length)], fill=color, width=2)


def _paw(d: ImageDraw.ImageDraw, x: int, y: int, s: int, color) -> None:
    """A tiny paw: one pad, four toes."""
    d.ellipse([x - s, y - int(0.2 * s), x + s, y + int(1.3 * s)], fill=color)
    for dx, dy in ((-1.25, -0.95), (-0.45, -1.5), (0.45, -1.5), (1.25, -0.95)):
        cx, cy, r = x + int(dx * s), y + int(dy * s), int(0.42 * s)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def compose(then_img: Image.Image, now_img: Image.Image, artifact: ShowArtifact, comparison: Comparison, *,
            commentary: str = "", when: datetime | None = None, theme: str = "ivory", font_path: str = "",
            session_id: str = "", now_note: str = "Photograph taken with consent") -> bytes:
    t = THEMES.get(theme, THEMES["ivory"])
    W, H = SIZE
    when = when or datetime.now()
    card = Image.new("RGB", (W, H), t["bg"])
    d = ImageDraw.Draw(card)

    caps = _font(SANS_CANDIDATES, 24)
    small = _font(SANS_CANDIDATES, 22)
    label_title = _font(ITALIC_CANDIDATES, 36, font_path)
    label_body = _font(SERIF_CANDIDATES, 28, font_path)
    big = _font(ITALIC_CANDIDATES, 112, font_path)
    comment_f = _font(ITALIC_CANDIDATES, 34, font_path)

    M, top, box_h = 150, 200, 780
    box_w = (W - 3 * M) // 2
    panels = (
        ("THEN", fit(trim_border(then_img), box_w, box_h)),
        ("NOW", fit(now_img.convert("RGB"), box_w, box_h)),
    )
    for i, (word, img) in enumerate(panels):
        x0 = M + i * (box_w + M)
        _spaced(d, (x0, top - 62), word, caps, t["accent"], spacing=10)
        px, py = x0 + (box_w - img.width) // 2, top + (box_h - img.height) // 2
        card.paste(img, (px, py))
        d.rectangle([px - 1, py - 1, px + img.width, py + img.height], outline=t["hair"], width=1)
        _marks(d, (px, py, px + img.width, py + img.height), t["hair"])

    # labels
    y_lab = top + box_h + 40
    lx = M
    for line in wrap_px(d, artifact.title, label_title, box_w, 1):
        d.text((lx, y_lab), line, font=label_title, fill=t["ink"])
        y_lab += 44
    meta = artifact.artist + (f", {artifact.date}" if artifact.date else "")
    d.text((lx, y_lab + 4), ellipsize(d, meta, label_body, box_w), font=label_body, fill=t["ink"])
    d.text((lx, y_lab + 46), ellipsize(d, f"{artifact.museum} · {artifact.rights_status}", small, box_w),
           font=small, fill=t["muted"])

    rx = 2 * M + box_w
    y_r = top + box_h + 40
    d.text((rx, y_r), "A participant and a robot dog", font=label_title, fill=t["ink"])
    d.text((rx, y_r + 48), f"Made together, {when.day} {when:%B %Y}", font=label_body, fill=t["ink"])
    d.text((rx, y_r + 90), now_note, font=small, fill=t["muted"])

    # comparison line (centre, small caps) + method note
    comp = comparison.label().upper()
    y_title = H - 278
    d.line([(M, y_title - 34), (W - M, y_title - 34)], fill=t["hair"], width=1)
    d.line([(M, y_title - 34), (M + 70, y_title - 34)], fill=t["accent"], width=3)
    title = "IS THIS US?"
    d.text(((W - d.textlength(title, font=big)) / 2, y_title - 6), title, font=big, fill=t["ink"])
    comp_w = d.textlength(comp, font=caps) + 4 * len(comp)
    _spaced(d, ((W - comp_w) / 2, y_title + 132), comp, caps, t["accent"], spacing=4)
    note = comparison.note
    d.text(((W - d.textlength(note, font=small)) / 2, y_title + 176), note, font=small, fill=t["muted"])

    if commentary:
        lines = wrap_px(d, commentary.replace("\n", " "), comment_f, W - 2 * M - 300, 1)
        if lines:
            d.text((M, H - 74), lines[0], font=comment_f, fill=t["muted"])
    sig = "— your robot dog"
    sx = W - M - d.textlength(sig, font=small)
    d.text((sx, H - 66), sig, font=small, fill=t["muted"])
    _paw(d, int(sx) - 34, H - 62, 9, t["accent"])
    if session_id:
        d.text((W - M - d.textlength(session_id, font=small), 64), session_id, font=small, fill=t["hair"])
    _spaced(d, (M, 64), "IS THIS US?  ·  A COLLABORATION", small, t["muted"], spacing=4)

    out = BytesIO()
    card.save(out, format="JPEG", quality=92, subsampling=0)
    return out.getvalue()
