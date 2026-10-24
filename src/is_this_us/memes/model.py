"""Meme annotation schema — the robot's second visual memory.

A meme is described by the same scene vocabulary as a painting (`human`, `dog`), so it
flows through the same matcher, planner and safety guard. Its metadata is different:
origin, attribution, cultural meaning, and — crucially — image rights.

Image provenance distinguishes licensed/recreated assets, references, and the
project owner's selection of familiar memes for this noncommercial presentation.
`presentation_use` records that selection without claiming a license. Such files
are exposed only when the caller explicitly requests the presentation scope.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from ..domain import DogAnnotation, HumanAnnotation


class ImageRights(StrEnum):
    REFERENCE_ONLY = "reference_only"  # link + description only (default)
    LICENSED = "licensed"  # written permission / licence recorded in license_note
    RECREATION = "recreation"  # original work made for this installation
    UNKNOWN = "unknown"  # drafts from discovery tools — never displayed
    PRESENTATION_USE = "presentation_use"  # project-owner selection for this noncommercial presentation


DISPLAYABLE = {ImageRights.LICENSED, ImageRights.RECREATION}


class MemeImage(BaseModel):
    rights: ImageRights = ImageRights.REFERENCE_ONLY
    local_file: str | None = None  # relative to the memes data dir
    license_note: str = ""
    source_url: HttpUrl | None = None
    source_page: HttpUrl | None = None
    retrieved_on: str = ""
    sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @model_validator(mode="after")
    def _policy(self):
        if self.rights in DISPLAYABLE | {ImageRights.PRESENTATION_USE} and not (self.local_file and self.license_note.strip()):
            raise ValueError("licensed/recreation/presentation images need local_file and a license_note")
        if self.rights not in DISPLAYABLE | {ImageRights.PRESENTATION_USE} and self.local_file:
            raise ValueError("reference_only/unknown memes must not ship an image file")
        if self.rights is ImageRights.PRESENTATION_USE and not (
            self.source_url and self.source_page and self.retrieved_on and len(self.sha256) == 64
        ):
            raise ValueError("presentation-use images need source URL/page, retrieval date, and SHA-256 provenance")
        return self


class MemeSource(BaseModel):
    label: str
    url: HttpUrl


class _Rights(BaseModel):
    status: str


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
    human: HumanAnnotation  # the visitor body that summons this meme
    dog: DogAnnotation
    image: MemeImage = MemeImage()
    reflection: dict[str, str] = Field(default_factory=dict)  # robot's sincere line per language
    curation_status: Literal["approved", "draft"] = "draft"
    discovered_via: str = "manual"
    notes: str = ""
    is_fixture: bool = False

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
    def presentation_displayable(self) -> bool:
        return self.displayable or self.image.rights is ImageRights.PRESENTATION_USE

    def reflection_for(self, lang: str) -> str:
        return self.reflection.get(lang) or self.reflection.get("en", "")

    def credit_line(self) -> str:
        return f"{self.name} — {self.subject}, {self.year}\n{self.attribution}"
