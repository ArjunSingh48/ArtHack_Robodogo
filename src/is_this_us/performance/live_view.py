"""Live camera view for the participant: what the robot sees, next to the painting.

Shown only while the robot is looking (small moments, pose session, capture). The frame
is mirrored like a mirror, the detected skeleton is drawn on top, and a short checklist
says what already matches. Frames live in memory only: they are served to the local
page and replaced by the next one — never written to disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw

from .pose_session import PoseReading, posture_ok

BONES = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24), (23, 25), (25, 27),
         (24, 26), (26, 28), (7, 2), (2, 5), (5, 8)]
ACCENT = (183, 166, 214)
OK = (160, 214, 170)
MAX_W = 720


@dataclass
class Check:
    label: str
    ok: bool


def checks_for(reading: PoseReading | None, *, posture: str | None = None, head_tilt: bool = False,
               any_person: bool = False) -> list[Check]:
    """What the participant can act on — never numbers, never about their body or looks."""
    r = reading
    visible = bool(r and r.person_visible)
    out = [Check("I can see you", visible)]
    if any_person:
        return out
    out.append(Check("whole body in the picture", visible and r.framing == "ok"))
    out.append(Check("in the middle", visible and r.side == "center"))
    if posture:
        out.append(Check("sitting" if posture == "sitting" else "standing", visible and posture_ok(posture, r.posture)))
    if head_tilt:
        out.append(Check("head tilted", visible and bool(r.head_tilt)))
    return out


def render(frame_rgb: np.ndarray, reading: PoseReading | None, *, mirror: bool = True) -> bytes:
    img = Image.fromarray(frame_rgb).convert("RGB")
    scale = min(1.0, MAX_W / img.width)
    if scale < 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    d = ImageDraw.Draw(img)
    lm = getattr(reading, "landmarks", None) if reading is not None else None
    if lm is not None:
        xy = lm.xy * scale
        vis = lm.visibility
        color = OK if reading.framing == "ok" else ACCENT
        for a, b in BONES:
            if vis[a] > 0.5 and vis[b] > 0.5:
                d.line([tuple(xy[a]), tuple(xy[b])], fill=color, width=max(3, img.width // 160))
        for i in (0, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28):
            if vis[i] > 0.5:
                x, y = xy[i]
                r = max(4, img.width // 140)
                d.ellipse([x - r, y - r, x + r, y + r], fill=color)
    if mirror:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    out = BytesIO()
    img.save(out, format="JPEG", quality=72)
    return out.getvalue()
