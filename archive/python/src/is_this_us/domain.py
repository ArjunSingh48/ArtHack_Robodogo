"""Shared vocabulary of the installation.

Everything artistic (postures, relationships, gestures) is expressed through these
closed enumerations. The robot can only ever execute a `RobotBehavior`, which is a
member of a small pre-approved list — never free text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


# --------------------------------------------------------------------------- humans

class HumanPosture(StrEnum):
    STANDING = "standing"
    SITTING = "sitting"
    CROUCHING = "crouching"
    KNEELING = "kneeling"
    LEANING_FORWARD = "leaning_forward"
    RECLINING = "reclining"
    LYING = "lying"
    UNKNOWN = "unknown"


class HumanGesture(StrEnum):
    EXTENDING_ARM = "extending_arm"
    ARMS_HOLDING = "arms_holding"  # forearms together in front of the torso
    LOOKING_DOWN = "looking_down"
    TURNED_TOWARD = "turned_toward"
    TURNED_AWAY = "turned_away"


class Proximity(StrEnum):
    CLOSE = "close"
    MEDIUM = "medium"
    FAR = "far"
    UNKNOWN = "unknown"


class HumanObservation(BaseModel):
    """What the robot believes it saw. Deliberately coarse and uncertain."""

    posture: HumanPosture
    posture_confidence: float = Field(ge=0, le=1)
    gestures: dict[HumanGesture, float] = Field(default_factory=dict)
    proximity: Proximity = Proximity.UNKNOWN
    landmark_confidence: float = Field(default=0.0, ge=0, le=1)
    people_detected: int = 1
    notes: list[str] = Field(default_factory=list)
    source: str = "unknown"

    def has(self, gesture: HumanGesture, threshold: float = 0.5) -> bool:
        return self.gestures.get(gesture, 0.0) >= threshold


# --------------------------------------------------------------------------- paintings

class DogPosture(StrEnum):
    STANDING = "standing"
    SITTING = "sitting"
    LYING = "lying"
    HELD = "held"  # carried in arms or on a lap
    LEANING_AGAINST = "leaning_against"
    JUMPING_UP = "jumping_up"
    WALKING = "walking"
    STRETCHING = "stretching"


class SpatialRelation(StrEnum):
    IN_ARMS = "in_arms"
    ON_LAP = "on_lap"
    BESIDE = "beside"
    AT_FEET = "at_feet"
    IN_FRONT = "in_front"
    BEHIND = "behind"
    APART = "apart"
    FACING_VIEWER = "facing_viewer"  # memes: the visitor stands where the camera stood


class Interaction(StrEnum):
    EMBRACE = "embrace"
    HOLDING = "holding"
    TOUCHING_HEAD = "touching_head"
    RESTING_TOGETHER = "resting_together"
    GAZING_UP = "gazing_up"
    WAITING = "waiting"
    ACCOMPANYING = "accompanying"
    OFFERING = "offering"
    GREETING = "greeting"
    # internet-culture relationships (equally sincere)
    REGARDING = "regarding"  # a sideways, knowing look at the viewer
    POSING = "posing"  # presenting itself to the camera
    ENDURING = "enduring"  # calm while everything goes wrong
    PEERING = "peering"  # nose first, curious, very close


class Reproducibility(StrEnum):
    YES = "yes"
    APPROXIMATE = "approximate"
    NO = "no"


class RobotBehavior(StrEnum):
    """The complete, pre-approved movement vocabulary. Nothing else can be executed."""

    STILLNESS = "stillness"  # no motion command at all
    STAND = "stand"
    SIT = "sit"
    LIE_DOWN = "lie_down"
    STRETCH = "stretch"
    GREET = "greet"
    LOOK_ASIDE = "look_aside"  # small body yaw while standing (SportClient.Euler), operator-approved
    PEER = "peer"  # small nose-down body pitch while standing (SportClient.Euler), operator-approved


class Rights(BaseModel):
    status: str  # e.g. "Public Domain", "CC0"
    source_statement: str
    verified_on: str  # ISO date when the rights were checked against the museum API


class DogAnnotation(BaseModel):
    posture: DogPosture
    relation: SpatialRelation
    interaction: Interaction
    gaze_at_human: bool = False
    reproducible: Reproducibility
    robot_behavior: RobotBehavior
    confidence: float = Field(ge=0, le=1, description="How clearly the painting shows this")


class HumanAnnotation(BaseModel):
    posture: HumanPosture
    gestures: list[HumanGesture] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class Artwork(BaseModel):
    kind: Literal["art"] = "art"
    id: str  # "<museum>:<object id>"
    museum: str
    museum_object_id: str
    title: str
    artist: str
    date: str = ""
    image_url: HttpUrl
    page_url: HttpUrl
    rights: Rights
    human: HumanAnnotation
    dog: DogAnnotation
    poem: str = ""
    is_fixture: bool = False  # True for test/mock data that is NOT a real artwork

    def credit_line(self) -> str:
        date = f", {self.date}" if self.date else ""
        return f"{self.artist} — {self.title}{date}\n{self.museum}"


# --------------------------------------------------------------------------- matching

class ScoreBreakdown(BaseModel):
    posture: float
    relationship: float
    gesture: float
    feasibility: float
    confidence: float
    novelty: float
    total: float


class ArchiveSource(StrEnum):
    ART = "art"
    MEME = "meme"
    BOTH = "both"
    SURPRISE = "surprise"


class MatchCandidate(BaseModel):
    """A scored scene. `scene` is an Artwork or a Meme — both expose
    `id`, `kind`, `title`, `human: HumanAnnotation`, `dog: DogAnnotation`."""

    scene: Any
    scores: ScoreBreakdown

    @property
    def artwork(self):  # backwards-compatible name
        return self.scene


class MatchResult(BaseModel):
    chosen: MatchCandidate | None
    ranked: list[MatchCandidate]
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.chosen is not None


# --------------------------------------------------------------------------- motion

class MotionPlan(BaseModel):
    requested: RobotBehavior  # what the painting suggests
    behavior: RobotBehavior  # what will actually be attempted after safety mapping
    fidelity: Reproducibility
    explanation: str


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    SIMULATED = "simulated"
    SKIPPED = "skipped"  # motion not armed / stillness chosen
    REJECTED = "rejected"  # safety guard refused
    FAILED = "failed"
    STOPPED = "stopped"


class ExecutionResult(BaseModel):
    behavior: RobotBehavior
    status: ExecutionStatus
    detail: str = ""
    duration_s: float = 0.0
    # why, in one word (for logs/operators): confirmed | sdk_error | rpc_timeout_unknown | transport_timeout |
    # rejected | stopped | guard_timeout | "" (not reported by this adapter)
    diagnostic: str = ""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
