"""SceneLibrary: one interface over both cultural memories (paintings and memes).

Paintings and memes keep their own metadata, but share the scene surface the
matcher needs (`id`, `kind`, `human`, `dog`). Ids are prefixed by archive
(`met:`, `cma:` → art; `meme:` → meme), so a stored id always routes back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from .artworks.repository import YamlArtworkRepository
from .domain import ArchiveSource, Artwork
from .memes.model import Meme
from .memes.repository import MemeRepository

Scene = Union[Artwork, Meme]


class SceneLibrary:
    def __init__(self, art: YamlArtworkRepository, memes: MemeRepository | None = None):
        self.art = art
        self.memes = memes

    def archives(self) -> list[ArchiveSource]:
        out = [ArchiveSource.ART] if self.art.all() else []
        if self.memes and self.memes.all():
            out.append(ArchiveSource.MEME)
        return out

    def all(self, source: ArchiveSource | str = ArchiveSource.BOTH) -> list[Scene]:
        source = ArchiveSource(source)
        art = self.art.all() if source in (ArchiveSource.ART, ArchiveSource.BOTH, ArchiveSource.SURPRISE) else []
        memes = (self.memes.all() if self.memes else []) if source in (
            ArchiveSource.MEME, ArchiveSource.BOTH, ArchiveSource.SURPRISE) else []
        return [*art, *memes]

    def get(self, scene_id: str) -> Scene | None:
        if scene_id.startswith("meme:"):
            return self.memes.get(scene_id) if self.memes else None
        return self.art.get(scene_id)

    def local_image(self, scene: Scene, *, presentation: bool = False) -> Path | None:
        if getattr(scene, "kind", "art") == "meme":
            return self.memes.local_image(scene, presentation=presentation) if self.memes else None
        return self.art.local_image(scene)

    def remote_image(self, scene: Scene) -> str | None:
        """Paintings may be fetched from the museum; memes never are."""
        if getattr(scene, "kind", "art") == "meme":
            return None
        return str(scene.image_url)
