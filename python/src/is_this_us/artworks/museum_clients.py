"""Clients for museum open-access APIs.

Verified against the live services on 2026-10-24:

* The Met: ``/public/collection/v1.1/search`` (paginated with ``offset``/``limit``,
  max 500). The old ``/v1/search`` was retired on 2026-10-01. Objects come from
  ``/public/collection/v1/objects/{id}``. Requests without a User-Agent header are
  blocked with HTTP 403, so we always send one.
* Cleveland Museum of Art: ``/api/artworks/`` with ``cc0`` / ``has_image`` filters and
  ``/api/artworks/{id}``. We only accept records whose ``share_license_status`` is CC0.

Rijksmuseum is planned as an optional second stage (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

USER_AGENT = "is-this-us/0.2 (interactive art installation prototype)"


class RightsError(ValueError):
    """Raised when a record is not cleared for reuse."""


@dataclass(frozen=True)
class MuseumRecord:
    museum: str
    museum_object_id: str
    title: str
    artist: str
    date: str
    image_url: str
    page_url: str
    rights_status: str
    rights_statement: str
    verified_on: str
    classification: str = ""

    @property
    def key(self) -> str:
        prefix = {"The Metropolitan Museum of Art": "met", "Cleveland Museum of Art": "cma"}[self.museum]
        return f"{prefix}:{self.museum_object_id}"


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


class MetClient:
    BASE = "https://collectionapi.metmuseum.org/public/collection"
    MUSEUM = "The Metropolitan Museum of Art"

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or _client()

    def search(self, query: str, *, department_id: int | None = None, offset: int = 0,
               limit: int = 100) -> tuple[int, list[int]]:
        params: dict[str, object] = {"q": query, "hasImages": "true", "offset": offset, "limit": min(limit, 500)}
        if department_id is not None:
            params["departmentId"] = department_id
        r = self.http.get(f"{self.BASE}/v1.1/search", params=params)
        r.raise_for_status()
        data = r.json()
        return int(data.get("total") or 0), list(data.get("objectIDs") or [])

    def get_raw(self, object_id: int | str) -> dict:
        r = self.http.get(f"{self.BASE}/v1/objects/{object_id}")
        r.raise_for_status()
        return r.json()

    def get(self, object_id: int | str) -> MuseumRecord:
        o = self.get_raw(object_id)
        if not o.get("isPublicDomain"):
            raise RightsError(f"Met object {object_id} is not marked public domain")
        image = o.get("primaryImageSmall") or o.get("primaryImage")
        if not image:
            raise RightsError(f"Met object {object_id} has no open-access image")
        return MuseumRecord(
            museum=self.MUSEUM,
            museum_object_id=str(o["objectID"]),
            title=o.get("title") or "Untitled",
            artist=o.get("artistDisplayName") or "Unknown artist",
            date=o.get("objectDate") or "",
            image_url=image,
            page_url=o.get("objectURL") or f"https://www.metmuseum.org/art/collection/search/{object_id}",
            rights_status="Public Domain",
            rights_statement="The Met Open Access: isPublicDomain=true (CC0 image)",
            verified_on=date.today().isoformat(),
            classification=o.get("classification") or "",
        )


class ClevelandClient:
    BASE = "https://openaccess-api.clevelandart.org/api"
    MUSEUM = "Cleveland Museum of Art"

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or _client()

    def search(self, query: str, *, artwork_type: str | None = "Painting", limit: int = 50,
               skip: int = 0) -> list[dict]:
        params: dict[str, object] = {"q": query, "cc0": 1, "has_image": 1, "limit": limit, "skip": skip}
        if artwork_type:
            params["type"] = artwork_type
        r = self.http.get(f"{self.BASE}/artworks/", params=params)
        r.raise_for_status()
        return list(r.json().get("data") or [])

    def get_raw(self, artwork_id: int | str) -> dict:
        r = self.http.get(f"{self.BASE}/artworks/{artwork_id}")
        r.raise_for_status()
        return r.json()["data"]

    def get(self, artwork_id: int | str) -> MuseumRecord:
        a = self.get_raw(artwork_id)
        if a.get("share_license_status") != "CC0":
            raise RightsError(f"CMA artwork {artwork_id} is not CC0 ({a.get('share_license_status')})")
        image = ((a.get("images") or {}).get("web") or {}).get("url")
        if not image:
            raise RightsError(f"CMA artwork {artwork_id} has no web image")
        creators = a.get("creators") or []
        artist = "; ".join(c.get("description", "").split(" (")[0] for c in creators if c.get("description"))
        if not artist:
            artist = (a.get("culture") or ["Unknown artist"])[0] if isinstance(a.get("culture"), list) else "Unknown artist"
        return MuseumRecord(
            museum=self.MUSEUM,
            museum_object_id=str(a["id"]),
            title=a.get("title") or "Untitled",
            artist=artist,
            date=a.get("creation_date") or "",
            image_url=image,
            page_url=a.get("url") or f"https://clevelandart.org/art/{a.get('accession_number', '')}",
            rights_status="CC0",
            rights_statement="Cleveland Museum of Art Open Access: share_license_status=CC0",
            verified_on=date.today().isoformat(),
            classification=a.get("type") or "",
        )


def fetch_record(key: str, *, met: MetClient | None = None, cma: ClevelandClient | None = None) -> MuseumRecord:
    """Fetch a record by `<museum>:<id>` key, verifying rights."""
    prefix, _, object_id = key.partition(":")
    if prefix == "met":
        return (met or MetClient()).get(object_id)
    if prefix == "cma":
        return (cma or ClevelandClient()).get(object_id)
    raise ValueError(f"Unknown museum prefix in {key!r}")
