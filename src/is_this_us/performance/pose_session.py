"""Guided pose: framing checks, gentle hints, and an honest qualitative comparison.

Reuses the existing pipeline (MediaPipe landmarks → `GeometricSceneInterpreter`). What
the camera can tell: whether a person is visible and framed, roughly where they stand,
whether they sit or stand, and whether their head is tilted. Nothing else.

Comparison policy:
* human target poses: the participant's pose (read by the camera) is compared with the
  curated description of the figure; if the same pipeline can also read the painted
  figure and agrees with the curators, we say so ("read from the painting too").
* dog target poses: semantic only (sitting, upright, …). Human and dog skeletons are
  never compared.
* no similarity numbers anywhere; nothing about the person's body, looks or ability.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..domain import HumanObservation, HumanPosture
from ..vision.interpreter import GeometricSceneInterpreter
from ..vision.pose import L_EAR, L_EYE, R_EAR, R_EYE, select_primary_person
from .catalog import InteractiveSpec, ShowArtifact

HEAD_TILT_DEG = 9.0
POSTURE_WORD = {"sitting": "seated", "standing": "standing"}


@dataclass
class PoseReading:
    person_visible: bool
    framing: str = "none"  # ok | none | partial | too_close
    side: str = "center"  # center | move_left | move_right  (participant's own left/right)
    posture: str | None = None  # HumanPosture value or None
    head_tilt: bool | None = None
    observation: HumanObservation | None = None
    people: int = 0

    def summary(self) -> dict:
        """Structured observation for a voice model — no numbers, no appearance."""
        return {"person_visible": self.person_visible, "framing": self.framing, "posture": self.posture,
                "head_tilted": self.head_tilt, "people_in_view": self.people}


def head_tilt_deg(xy: np.ndarray, vis: np.ndarray) -> float | None:
    for a, b in ((L_EYE, R_EYE), (L_EAR, R_EAR)):
        if vis[a] >= 0.5 and vis[b] >= 0.5:
            dx, dy = xy[a] - xy[b]
            if abs(dx) < 1e-6:
                return None
            ang = abs(math.degrees(math.atan2(dy, dx)))
            return min(ang, 180 - ang)
    return None


class PoseReader:
    def __init__(self, estimator, interpreter: GeometricSceneInterpreter | None = None, *, mirrored: bool = False):
        self.estimator = estimator
        self.interpreter = interpreter or GeometricSceneInterpreter()
        self.mirrored = mirrored

    def read(self, rgb: np.ndarray, source: str = "camera") -> PoseReading:
        people = self.estimator.estimate(rgb) if self.estimator is not None else []
        person, _ = select_primary_person(people)
        if person is None:
            return PoseReading(person_visible=False, people=len(people))
        obs = self.interpreter.interpret_person(person, source=source)
        obs.people_detected = len(people)
        feats = self.interpreter.features(person)
        w, h = person.image_size
        x0, y0, x1, y1 = person.bbox()
        if feats is None:
            framing = "partial"
        elif (y1 - y0) / max(h, 1) > 0.97 and y0 <= 0.01 * h:
            framing = "too_close"
        elif not feats.legs_visible:
            framing = "partial"
        else:
            framing = "ok"
        cx = (x0 + x1) / 2 / max(w, 1)
        # Camera facing the participant: someone on the image's left stands to their own right,
        # so they should move to THEIR left. A mirrored (selfie) image flips this.
        side = "center"
        if cx < 0.33:
            side = "move_right" if self.mirrored else "move_left"
        elif cx > 0.67:
            side = "move_left" if self.mirrored else "move_right"
        tilt = head_tilt_deg(person.xy, person.visibility)
        posture = obs.posture.value if obs.posture is not HumanPosture.UNKNOWN else None
        return PoseReading(person_visible=True, framing=framing, side=side, posture=posture,
                           head_tilt=None if tilt is None else tilt >= HEAD_TILT_DEG, observation=obs,
                           people=len(people))


def posture_ok(target: str, posture: str | None) -> bool:
    if posture is None:
        return False
    if target == "sitting":
        return posture in ("sitting", "kneeling", "crouching")  # a low, seated-like pose counts
    return posture == target


def pose_reached(spec: InteractiveSpec, r: PoseReading) -> bool:
    return (r.person_visible and r.framing == "ok" and posture_ok(spec.posture, r.posture)
            and (not spec.head_tilt or bool(r.head_tilt)))


class PoseCoach:
    """Chooses the next gentle hint (a key in lines → pose), or None. Rate-limited."""

    def __init__(self, spec: InteractiveSpec, *, min_gap_s: float = 8.0, max_hints: int = 7, clock=time.monotonic):
        self.spec = spec
        self.min_gap_s = min_gap_s
        self.max_hints = max_hints
        self.clock = clock
        self.given: list[str] = []
        self.last_at = -1e9
        self.reached = False
        self._suggested: set[str] = set()

    def next_hint(self, r: PoseReading) -> str | None:
        now = self.clock()
        if now - self.last_at < self.min_gap_s:
            return None
        key = self._choose(r)
        if key is None:
            return None
        if key != "got_it" and len(self.given) >= self.max_hints:
            return None
        if self.given[-2:] == [key, key]:  # never nag with the same words
            return None
        self.given.append(key)
        self.last_at = now
        return key

    def _choose(self, r: PoseReading) -> str | None:
        if not r.person_visible:
            return "not_visible"
        if r.framing == "too_close":
            return "too_close"
        if r.framing == "partial":
            return "partly_visible"
        if r.side != "center":
            return r.side
        if not posture_ok(self.spec.posture, r.posture):
            return "sit" if self.spec.posture == "sitting" else "stand"
        if self.spec.head_tilt and not r.head_tilt:
            return "tilt_head"
        for s in self.spec.suggest:
            if s not in self._suggested:
                self._suggested.add(s)
                return s
        if not self.reached:
            self.reached = True
            return "got_it"
        return None


@dataclass
class Comparison:
    method: str  # "camera+painting" | "camera+curators" | "meaning" | "none"
    shared: list[str] = field(default_factory=list)
    different: list[str] = field(default_factory=list)
    line_keys: list[str] = field(default_factory=list)  # lines → compare
    note: str = ""  # one honest sentence about how this was compared

    def label(self) -> str:
        if self.method == "none":
            return "A pose taken together"
        parts = [f"both {p}" for p in self.shared] + [p for p in self.different]
        return " · ".join(parts) if parts else "two ways of holding still"


def read_painted_figure(artifact: ShowArtifact, estimator, interpreter=None) -> PoseReading | None:
    """Try to read the human figure in the artwork with the same pipeline. None if unreliable."""
    spec = artifact.interactive
    if (estimator is None or spec is None or spec.semantic_only or not artifact.displayable
            or not getattr(estimator, "reads_paintings", True)):
        return None
    from PIL import Image

    img = Image.open(artifact.image_path).convert("RGB")
    if spec.crop:
        w, h = img.size
        x0, y0, x1, y1 = spec.crop
        img = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    try:
        reading = PoseReader(estimator, interpreter).read(np.asarray(img), source="painting")
    except Exception:
        return None
    if not reading.person_visible or reading.observation is None or reading.observation.posture_confidence < 0.5:
        return None
    return reading


def compare(artifact: ShowArtifact, now: PoseReading | None, painted: PoseReading | None = None) -> Comparison:
    spec = artifact.interactive
    assert spec is not None
    if now is None or not now.person_visible or now.posture is None:
        return Comparison("none", line_keys=["understand"],
                          note="The camera could not read the pose, so no comparison was made.")
    shared, different = [], []
    who = "the dog" if spec.semantic_only else "the figure"
    if posture_ok(spec.posture, now.posture):
        shared.append(POSTURE_WORD.get(spec.posture, spec.posture))
    else:
        different.append(f"{who} {POSTURE_WORD.get(spec.posture, spec.posture)}, you {POSTURE_WORD.get(now.posture, now.posture)}")
    if spec.head_tilt and not spec.semantic_only:
        if now.head_tilt:
            shared.append("heads tilted")
        elif now.head_tilt is False:
            different.append("a tilted head, a straight one")
    if spec.semantic_only:
        method, note = "meaning", ("Compared by meaning only: a person and a dog are never measured "
                                   "against each other.")
    elif painted is not None and posture_ok(spec.posture, painted.posture):
        method, note = "camera+painting", "Your pose was read by the camera; the painted figure was read the same way."
    else:
        method, note = "camera+curators", "Your pose was read by the camera and compared with how the curators describe the picture."
    keys = ["shared", "differ", "okay"] if shared and not different else ["understand"]
    return Comparison(method, shared, different, keys, note)
