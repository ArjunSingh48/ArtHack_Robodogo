"""Artwork repository: the robot's artistic memory.

Annotations live in a human-editable YAML file (source of truth, reviewed by people).
Images can be cached locally so the installation keeps working offline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Protocol

import yaml

from ..domain import Artwork, scene_is_eligible

ANNOTATION_FILE = "annotations.yaml"


class ArtworkRepository(Protocol):
    def all(self) -> list[Artwork]: ...
    def get(self, artwork_id: str) -> Artwork | None: ...
    def local_image(self, artwork: Artwork) -> Path | None: ...


class YamlArtworkRepository:
    def __init__(self, data_dir: Path, filename: str = ANNOTATION_FILE):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / filename
        self.image_dir = self.data_dir / "images"
        self._items: dict[str, Artwork] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"No artwork annotations at {self.path}")
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        items = [Artwork.model_validate(entry) for entry in raw.get("artworks", [])]
        ids = [a.id for a in items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate artwork ids in annotations")
        self._items = {a.id: a for a in items}

    def all(self) -> list[Artwork]:
        """Only reviewed, eligible scenes may enter matching."""
        return [a for a in self._items.values() if scene_is_eligible(a)]

    def all_records(self) -> list[Artwork]:
        """Loaded records, including ineligible records, for audit and history."""
        return list(self._items.values())

    def get(self, artwork_id: str) -> Artwork | None:
        return self._items.get(artwork_id)

    def image_path_for(self, artwork: Artwork) -> Path:
        digest = hashlib.sha1(str(artwork.image_url).encode()).hexdigest()[:10]
        safe = artwork.id.replace(":", "_")
        return self.image_dir / f"{safe}_{digest}.jpg"

    def local_image(self, artwork: Artwork) -> Path | None:
        p = self.image_path_for(artwork)
        return p if p.exists() else None


def dump_artworks(artworks: Iterable[Artwork], path: Path, header: str = "") -> None:
    data = {"version": 1, "artworks": [a.model_dump(mode="json") for a in artworks]}
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((header + "\n" if header else "") + text, encoding="utf-8")
