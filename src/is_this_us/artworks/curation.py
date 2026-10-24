"""Curation tools: search museums, draft annotations, build and validate the archive.

Workflow (see README):
    itu artworks search "dog" --museum met         # find candidates
    itu artworks draft met:437173                  # print an annotation stub to fill in
    (edit data/artworks/curation_notes.yaml)
    itu artworks build                             # fetch metadata + verify rights
    itu artworks validate                          # schema + vocabulary + rights checks
    itu artworks cache                             # download images for offline use
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, ValidationError, model_validator

from ..domain import (Artwork, DogAnnotation, DogPosture, EligibilityReview, HumanAnnotation,
                      Reproducibility, RobotBehavior, scene_is_eligible, validate_reviewed_dog)
from .museum_clients import USER_AGENT, ClevelandClient, MetClient, RightsError, fetch_record
from .repository import ANNOTATION_FILE, YamlArtworkRepository, dump_artworks

log = logging.getLogger(__name__)
NOTES_FILE = "curation_notes.yaml"

# Dog postures that can never be performed literally; they must map to an approximation.
NEVER_LITERAL = {DogPosture.HELD, DogPosture.JUMPING_UP, DogPosture.WALKING, DogPosture.LEANING_AGAINST}


def load_notes(data_dir: Path) -> dict[str, dict]:
    raw = yaml.safe_load((Path(data_dir) / NOTES_FILE).read_text(encoding="utf-8")) or {}
    return dict(raw.get("notes") or {})


class CurationNote(BaseModel):
    human: HumanAnnotation
    dog: DogAnnotation
    poem: str = ""
    review: EligibilityReview | None = None

    @model_validator(mode="after")
    def _review_consistency(self):
        validate_reviewed_dog(self.review, self.dog)
        return self


def build(data_dir: Path, *, offline: bool = False) -> tuple[list[Artwork], list[str]]:
    """Build accepted notes only; any error leaves the previous collection intact.

    Offline mode reuses museum_metadata.yaml (or the previous generated file when
    no snapshot exists), including its original rights verification date. It does
    not claim that museum metadata or rights were reverified.
    """
    data_dir = Path(data_dir)
    problems: list[str] = []
    notes: dict[str, CurationNote] = {}
    for key, raw in load_notes(data_dir).items():
        try:
            notes[key] = CurationNote.model_validate(raw)
        except ValidationError as exc:
            problems.append(f"{key}: invalid curation note — {exc}")
    if problems:
        return [], problems
    accepted = {key: note for key, note in notes.items() if note.review and note.review.eligible}
    snapshot: dict[str, dict] = {}
    snapshot_path = data_dir / "museum_metadata.yaml"
    if offline and accepted:
        if not snapshot_path.exists():
            snapshot_path = data_dir / ANNOTATION_FILE
        if not snapshot_path.exists():
            return [], ["Offline build needs museum_metadata.yaml or existing annotations.yaml"]
        raw = yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) or {}
        for entry in raw.get("artworks", []):
            key = entry.get("id")
            if not key or key in snapshot:
                problems.append(f"{snapshot_path.name}: missing or duplicate museum ID {key!r}")
            snapshot[key] = entry
        if problems:
            return [], problems
    met, cma = (None, None) if offline else (MetClient(), ClevelandClient())
    built: list[Artwork] = []
    metadata_fields = {"id", "museum", "museum_object_id", "title", "artist", "date",
                       "image_url", "page_url", "rights"}
    for key, note in accepted.items():
        try:
            if offline:
                if key not in snapshot:
                    raise ValueError("accepted ID missing from offline museum metadata snapshot")
                metadata = {k: v for k, v in snapshot[key].items() if k in metadata_fields}
            else:
                rec = fetch_record(key, met=met, cma=cma)
                if rec.key != key:
                    raise ValueError(f"museum returned different ID {rec.key}")
                metadata = dict(
                    id=rec.key, museum=rec.museum, museum_object_id=rec.museum_object_id,
                    title=rec.title, artist=rec.artist, date=rec.date,
                    image_url=rec.image_url, page_url=rec.page_url,
                    rights={"status": rec.rights_status, "source_statement": rec.rights_statement,
                            "verified_on": rec.verified_on},
                )
            art = Artwork(**metadata, human=note.human, dog=note.dog, poem=note.poem, review=note.review)
            if art.rights.status not in {"Public Domain", "CC0"}:
                raise ValueError(f"rights status {art.rights.status!r} is not cleared")
            if not art.rights.verified_on or not art.rights.source_statement.strip():
                raise ValueError("museum rights verification evidence is missing")
            if art.dog.posture in NEVER_LITERAL and art.dog.reproducible is Reproducibility.YES:
                raise ValueError(f"dog posture {art.dog.posture!r} cannot be reproduced literally")
            if art.dog.reproducible is Reproducibility.NO and art.dog.robot_behavior is not RobotBehavior.STILLNESS:
                raise ValueError("non-reproducible pose must use stillness")
            built.append(art)
        except (RightsError, httpx.HTTPError, ValueError) as exc:
            problems.append(f"{key}: build failed — {exc}")
    if problems:
        return [], problems
    source = f"offline {snapshot_path.name} (original rights dates retained)" if offline else "live museum APIs"
    command = "itu artworks build --offline" if offline else "itu artworks build"
    header = (f"# GENERATED by `{command}` from curation_notes.yaml + {source}.\n"
              "# Only visually accepted scenes. Edit curation_notes.yaml and rebuild.")
    dump_artworks(built, data_dir / ANNOTATION_FILE, header=header)
    return built, []


def validate(repo: YamlArtworkRepository) -> list[str]:
    """Return a list of human-readable problems (empty list == valid)."""
    problems: list[str] = []
    for a in repo.all_records():
        if not scene_is_eligible(a):
            problems.append(f"{a.id}: missing or non-accepted visual eligibility review")
        d = a.dog
        if d.posture in NEVER_LITERAL and d.reproducible is Reproducibility.YES:
            problems.append(f"{a.id}: dog posture '{d.posture}' cannot be reproduced literally")
        if d.reproducible is Reproducibility.NO and d.robot_behavior is not RobotBehavior.STILLNESS:
            problems.append(f"{a.id}: non-reproducible pose must use 'stillness'")
        if not a.is_fixture and a.rights.status not in {"Public Domain", "CC0"}:
            problems.append(f"{a.id}: rights status '{a.rights.status}' is not cleared")
        if not a.artist or not a.title:
            problems.append(f"{a.id}: missing artist or title")
    return problems


def cache_images(repo: YamlArtworkRepository, *, force: bool = False) -> list[str]:
    repo.image_dir.mkdir(parents=True, exist_ok=True)
    messages = []
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http:
        for a in repo.all():
            if a.is_fixture:
                continue
            target = repo.image_path_for(a)
            if target.exists() and not force:
                continue
            try:
                r = http.get(str(a.image_url))
                r.raise_for_status()
                target.write_bytes(r.content)
                messages.append(f"cached {a.id} ({len(r.content) // 1024} KB)")
            except httpx.HTTPError as exc:
                messages.append(f"failed {a.id}: {exc}")
    return messages


def search(query: str, museum: str = "met", limit: int = 20) -> list[str]:
    """List candidate records (rights-cleared only) for manual review."""
    lines: list[str] = []
    if museum == "met":
        client = MetClient()
        _, ids = client.search(query, department_id=11, limit=limit * 3)
        for object_id in ids:
            if len(lines) >= limit:
                break
            try:
                rec = client.get(object_id)
            except (RightsError, httpx.HTTPError):
                continue
            if rec.classification == "Paintings":
                lines.append(f"{rec.key}\t{rec.artist} — {rec.title} ({rec.date})")
    elif museum == "cma":
        for a in ClevelandClient().search(query, limit=limit):
            if a.get("share_license_status") == "CC0":
                lines.append(f"cma:{a['id']}\t{a.get('title')} ({a.get('creation_date')})")
    else:
        raise ValueError("museum must be 'met' or 'cma'")
    return lines


def draft(key: str) -> str:
    rec = fetch_record(key)
    stub = {
        key: {
            "review": {"status": "needs_review", "human_count": None, "dog_count": None,
                       "dog_on_ground": None, "reason": "Awaiting full-image visual review",
                       "reviewer": "TODO", "reviewed_on": None, "image_reference": str(rec.image_url)},
            "human": {"posture": "standing|sitting|crouching|kneeling|leaning_forward|reclining|lying|unknown",
                      "gestures": [], "confidence": 0.7},
            "dog": {"posture": "standing|sitting|lying|held|leaning_against|jumping_up|walking|stretching",
                    "relation": "in_arms|on_lap|beside|at_feet|in_front|behind|apart",
                    "interaction": "embrace|holding|touching_head|resting_together|gazing_up|waiting|"
                                   "accompanying|offering|greeting",
                    "gaze_at_human": False, "reproducible": "yes|approximate|no",
                    "robot_behavior": "stillness|stand|sit|lie_down|stretch|greet", "confidence": 0.7},
            "poem": "",
        }
    }
    return f"# {rec.artist} — {rec.title} ({rec.date})\n# {rec.page_url}\n" + yaml.safe_dump(stub, sort_keys=False)
