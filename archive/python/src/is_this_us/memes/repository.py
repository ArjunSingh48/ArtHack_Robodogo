"""MemeRepository: one JSON file per meme in `data/memes/annotations/`.

Adding a meme = adding a JSON file. No robot-control code changes: a meme can only
name a behavior from the existing `RobotBehavior` vocabulary, and the operator decides
which of those behaviors are approved at runtime.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from .model import Meme

log = logging.getLogger(__name__)


class MemeRepository:
    def __init__(self, data_dir: Path, *, include_drafts: bool = False):
        self.data_dir = Path(data_dir)
        self.annotation_dir = self.data_dir / "annotations"
        self.include_drafts = include_drafts
        self.errors: dict[str, str] = {}
        self._items: dict[str, Meme] = {}
        self.reload()

    def reload(self) -> None:
        self._items, self.errors = {}, {}
        if not self.annotation_dir.exists():
            return
        for path in sorted(self.annotation_dir.glob("*.json")):
            try:
                meme = Meme.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (ValidationError, json.JSONDecodeError) as exc:
                self.errors[path.name] = str(exc)
                log.error("skipping meme %s: %s", path.name, exc)
                continue
            if meme.id in self._items:
                self.errors[path.name] = f"duplicate id {meme.id}"
                continue
            if meme.curation_status != "approved" and not self.include_drafts:
                continue
            if meme.presentation_displayable and not (self.data_dir / meme.image.local_file).is_file():
                self.errors[path.name] = f"image file missing: {meme.image.local_file}"
                continue
            self._items[meme.id] = meme

    def all(self) -> list[Meme]:
        return list(self._items.values())

    def get(self, meme_id: str) -> Meme | None:
        return self._items.get(meme_id)

    def local_image(self, meme: Meme, *, presentation: bool = False) -> Path | None:
        """Return a local asset; presentation-use files require explicit scope."""
        allowed = meme.presentation_displayable if presentation else meme.displayable
        if not allowed or not meme.image.local_file:
            return None
        p = self.data_dir / meme.image.local_file
        return p if p.is_file() else None
