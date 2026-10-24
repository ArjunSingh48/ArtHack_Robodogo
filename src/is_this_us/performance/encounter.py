"""The good boy encounter — the show's finale (docs/UX.md).

Helpers used by the StoryDirector:
* `StabilityTracker` — the encounter starts by itself once a person holds a pose steadily.
* `telegram_message` — the one image message: short caption + italic attribution (HTML).
* `deliverable` — only images the archive's rights policy lets us send.
* `side_for` — where the robot goes next to the person, from the painted dog's relation.

The visitor frame is never part of the message: Telegram receives the archive image.
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass

from .catalog import ShowArtifact
from .pose_session import PoseReading


@dataclass
class StabilityTracker:
    """True once the same person keeps the same posture, roughly in place, for `hold_s` seconds."""

    hold_s: float = 3.0
    move_tolerance: float = 0.08  # fraction of the image the body centre may drift
    clock: object = None
    _since: float | None = None
    _posture: str | None = None
    _centre: tuple[float, float] | None = None

    def reset(self) -> None:
        self._since, self._posture, self._centre = None, None, None

    def progress(self, now: float) -> float:
        """0..1 — for the operator's live checklist."""
        if self._since is None or self.hold_s <= 0:
            return 0.0
        return min(1.0, (now - self._since) / self.hold_s)

    def update(self, r: PoseReading | None, now: float) -> bool:
        if r is None or not r.person_visible or r.framing != "ok" or r.posture is None:
            self.reset()
            return False
        lm = getattr(r, "landmarks", None)
        centre = None
        if lm is not None:
            x0, y0, x1, y1 = lm.bbox()
            w, h = lm.image_size
            centre = ((x0 + x1) / 2 / w, (y0 + y1) / 2 / h)
        moved = (centre is not None and self._centre is not None
                 and max(abs(centre[0] - self._centre[0]), abs(centre[1] - self._centre[1])) > self.move_tolerance)
        if self._since is None or r.posture != self._posture or moved:
            self._since, self._posture, self._centre = now, r.posture, centre
            return self.hold_s <= 0
        return now - self._since >= self.hold_s


def deliverable(a: ShowArtifact | None) -> bool:
    """May this image be sent? Open-access art with a local file; memes only when licensed/recreated."""
    return bool(a and a.displayable and (a.kind == "art" or a.rights_statement in ("licensed", "recreation")))


def attribution(a: ShowArtifact) -> str:
    """Plain attribution: Artist — Title, Date. Collection. Rights."""
    who = a.artist.strip() or "Unknown artist"
    work = a.title.strip() + (f", {a.date.strip()}" if a.date.strip() else "")
    rights = a.rights_status.strip()
    if a.kind == "art" and a.rights_statement:
        rights = f"{rights} ({a.rights_statement.split(':')[0].strip()})" if ":" in a.rights_statement else rights
    parts = [f"{who} — {work}.", f"{a.museum.strip()}."]
    if rights:
        parts.append(f"{rights}.")
    return " ".join(p.replace("..", ".") for p in parts)


def telegram_message(a: ShowArtifact, captions: dict[str, list[str]], rng: random.Random | None = None) -> str:
    """The image caption: one short line, a blank line, then the attribution in italics (Telegram HTML)."""
    rng = rng or random.Random()
    pool = captions.get("meme" if a.kind == "meme" else "painting") or ["us?👉👈"]
    return f"{html.escape(rng.choice(pool))}\n\n<i>{html.escape(attribution(a))}</i>"


def side_for(scene, artifact: ShowArtifact | None = None) -> str:
    """Where the robot stops next to the person (as seen from where it starts)."""
    if artifact is not None and artifact.interactive and artifact.interactive.robot_side:
        return artifact.interactive.robot_side
    rel = str(getattr(getattr(scene, "dog", None), "relation", "") or "")
    rel = getattr(rel, "value", rel)
    return "front" if rel in ("at_feet", "in_front") else "left"
