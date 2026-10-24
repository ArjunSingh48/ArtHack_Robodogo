"""Meme annotation schema — the robot's second visual memory.

A meme is described by the same scene vocabulary as a painting (`human`, `dog`), so it
flows through the same matcher, planner and safety guard. Its metadata is different:
origin, attribution, cultural meaning, and — crucially — image rights.

Solo-dog photo opportunities use a separate `photo_pose` review and random selector;
they do not acquire pair-matching eligibility or a fictional human annotation.

Image policy: most famous meme images are copyrighted photographs or comics. Being
listed on Imgflip, Know Your Meme or anywhere else does NOT grant permission. By default
a meme is `reference_only`: we describe it, credit it and link to it, but never store
or send the image. An image is shown only when the annotation records `licensed` or
`recreation` (an original re-staging made by the team) AND the file is present.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from ..domain import (DogAnnotation, EligibilityReview, HumanAnnotation, Reproducibility,
                      validate_reviewed_dog)


class ImageRights(StrEnum):
    REFERENCE_ONLY = "reference_only"  # link + description only (default)
    LICENSED = "licensed"  # written permission / licence recorded in license_note
    RECREATION = "recreation"  # original work made for this installation
    UNKNOWN = "unknown"  # drafts from discovery tools — never displayed


DISPLAYABLE = {ImageRights.LICENSED, ImageRights.RECREATION}


class MemeImage(BaseModel):
    rights: ImageRights = ImageRights.REFERENCE_ONLY
    local_file: str | None = None  # relative to the memes data dir; only for DISPLAYABLE
    license_note: str = ""

    @model_validator(mode="after")
    def _policy(self):
        if self.rights in DISPLAYABLE and not (self.local_file and self.license_note.strip()):
            raise ValueError("licensed/recreation images need local_file and a license_note")
        if self.rights not in DISPLAYABLE and self.local_file:
            raise ValueError("reference_only/unknown memes must not ship an image file")
        return self


class MemeSource(BaseModel):
    label: str
    url: HttpUrl


class _Rights(BaseModel):
    status: str


class PhotoPoseReview(BaseModel):
    """Suitability for a random camera-facing imitation, separate from pair review.

    The mapped action is dog.robot_behavior; this review never grants hardware
    approval or permission to reproduce the source image.
    """

    status: Literal["accepted", "rejected", "needs_review"] = "needs_review"
    cue: str = Field(min_length=1, pattern=r"\S")
    approximation: str = Field(min_length=1, pattern=r"\S")
    reason: str = Field(min_length=1, pattern=r"\S")
    reviewer: str = Field(min_length=1, pattern=r"\S")
    reviewed_on: date
    review_basis: Literal["existing_annotations", "visual_review"]


class Meme(BaseModel):
    kind: Literal["meme"] = "meme"
    id: str = Field(pattern=r"^meme:[a-z0-9_\-]+$")
    name: str
    also_known_as: list[str] = Field(default_factory=list)
    subject: str  # the dog (name, breed) or the drawn character
    year: str  # when the source image/comic appeared
    origin: str  # one factual sentence
    attribution: str  # credit line shown to visitors
    sources: list[MemeSource] = Field(min_length=1)  # verified reference pages
    cultural_meaning: str
    human: HumanAnnotation  # depicted human; unknown/0 when none, ignored for photo poses
    dog: DogAnnotation
    image: MemeImage = MemeImage()
    reflection: dict[str, str] = Field(default_factory=dict)  # robot's sincere line per language
    curation_status: Literal["approved", "draft"] = "draft"
    discovered_via: str = "manual"
    notes: str = ""
    is_fixture: bool = False
    review: EligibilityReview | None = None
    photo_pose: PhotoPoseReview | None = None

    @model_validator(mode="after")
    def _review_consistency(self):
        validate_reviewed_dog(self.review, self.dog)
        return self

    # --- the shared "scene" surface used by matcher, dialogue, cards, Telegram ---
    @property
    def title(self) -> str:
        return self.name

    @property
    def artist(self) -> str:
        return self.attribution

    @property
    def date(self) -> str:
        return self.year

    @property
    def museum(self) -> str:
        return "Internet culture"

    @property
    def page_url(self) -> HttpUrl:
        return self.sources[0].url

    @property
    def poem(self) -> str:
        return self.reflection.get("en", "")

    @property
    def rights(self) -> _Rights:
        return _Rights(status="image: " + self.image.rights.value.replace("_", " "))

    @property
    def displayable(self) -> bool:
        return self.image.rights in DISPLAYABLE

    @property
    def photo_pose_eligible(self) -> bool:
        return (self.curation_status == "approved" and self.photo_pose is not None
                and self.photo_pose.status == "accepted"
                and self.image.rights is not ImageRights.UNKNOWN
                and self.dog.reproducible is not Reproducibility.NO)

    def reflection_for(self, lang: str) -> str:
        return self.reflection.get(lang) or self.reflection.get("en", "")

    def credit_line(self) -> str:
        return f"{self.name} — {self.subject}, {self.year}\n{self.attribution}"
