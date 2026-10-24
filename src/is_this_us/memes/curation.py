"""Meme curation: discovery via the Imgflip API, validation, listing.

Imgflip's `GET https://api.imgflip.com/get_memes` (no key needed; verified 2026-10-24)
returns the ~100 currently popular caption templates. It is a DISCOVERY aid only:

* An image being served by an API does NOT mean we may reproduce it. Drafts are
  written with `image.rights = "unknown"` and are never loaded by the installation.
* Imgflip lists only what is popular right now. On 2026-10-24 it contained "This Is
  Fine" and "Buff Doge vs. Cheems" — not Side Eye Dog or the Borzoi — so curators
  also add memes by hand.
* Turning a draft into an approved meme is a human act: verify origin and
  attribution from primary sources, annotate the scene, choose an approved robot
  behavior, decide image rights, then move the file to `annotations/`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from ..artworks.museum_clients import USER_AGENT
from ..domain import scene_is_eligible
from .model import Meme
from .repository import MemeRepository

IMGFLIP_URL = "https://api.imgflip.com/get_memes"
DOG_KEYWORDS = ("dog", "doge", "cheems", "shiba", "borzoi", "puppy", "pupper", "hound", "this is fine", "side eye")

DRAFT_NOTICE = ("DRAFT from Imgflip discovery. Listing on Imgflip is not permission to reproduce the image. "
                "Verify origin/attribution from primary sources, annotate, decide rights, then move to annotations/.")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fetch_imgflip(http: httpx.Client | None = None) -> list[dict]:
    http = http or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    r = http.get(IMGFLIP_URL)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Imgflip API error: {data.get('error_message', 'unknown')}")
    return list(data["data"]["memes"])


def discover(data_dir: Path, *, keywords=DOG_KEYWORDS, http: httpx.Client | None = None) -> list[Path]:
    """Write a draft JSON for every dog-related Imgflip template not already curated."""
    data_dir = Path(data_dir)
    drafts_dir = data_dir / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    known = {m.name.lower() for m in MemeRepository(data_dir, include_drafts=True).all_records()}
    known |= {a.lower() for m in MemeRepository(data_dir, include_drafts=True).all_records() for a in m.also_known_as}
    written = []
    for t in fetch_imgflip(http):
        name = t.get("name", "")
        if not any(k in name.lower() for k in keywords) or name.lower() in known:
            continue
        slug = slugify(name)
        draft = {
            "id": f"meme:{slug}",
            "name": name,
            "subject": "TODO: who/what is depicted",
            "year": "TODO",
            "origin": "TODO: one factual sentence from a primary source",
            "attribution": "TODO: photographer / artist / original poster",
            "sources": [{"label": "Imgflip template (discovery only)", "url": f"https://imgflip.com/meme/{t['id']}"}],
            "cultural_meaning": "TODO",
            "human": {"posture": "unknown", "gestures": [], "confidence": 0.3},
            "dog": {"posture": "sitting", "relation": "facing_viewer", "interaction": "posing", "gaze_at_human": False,
                    "reproducible": "no", "robot_behavior": "stillness", "confidence": 0.3},
            "image": {"rights": "unknown", "license_note": "Not cleared. Do not store or send this image."},
            "reflection": {},
            "curation_status": "draft",
            "discovered_via": f"imgflip:{t['id']}",
            "notes": DRAFT_NOTICE,
        }
        path = drafts_dir / f"{slug}.json"
        if not path.exists():
            path.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            written.append(path)
    return written


def validate(data_dir: Path) -> list[str]:
    """Problems with approved memes (schema, rights policy, robot safety)."""
    from ..artworks.curation import NEVER_LITERAL
    from ..domain import Reproducibility, RobotBehavior

    repo = MemeRepository(data_dir)
    problems = [f"{name}: {err}" for name, err in repo.errors.items()]
    for m in repo.all_records():
        if m.image.rights.value == "unknown":
            problems.append(f"{m.id}: approved memes must have decided image rights (not 'unknown')")
        if m.dog.posture in NEVER_LITERAL and m.dog.reproducible is Reproducibility.YES:
            problems.append(f"{m.id}: dog posture '{m.dog.posture}' cannot be reproduced literally")
        if m.dog.reproducible is Reproducibility.NO and m.dog.robot_behavior is not RobotBehavior.STILLNESS:
            problems.append(f"{m.id}: non-reproducible pose must use 'stillness'")
        if "en" not in m.reflection:
            problems.append(f"{m.id}: needs at least an English reflection line")
        if any("TODO" in str(v) for v in m.model_dump().values()):
            problems.append(f"{m.id}: still contains TODO fields")
    return problems


def describe(m: Meme) -> str:
    return (f"{m.id:24} {m.human.posture:15} | dog {m.dog.posture:9} {m.dog.interaction:9} → "
            f"{m.dog.robot_behavior:10} | image: {m.image.rights.value:14} | "
            f"curation: {m.curation_status} | eligible: {'yes' if scene_is_eligible(m) else 'no'} | "
            f"photo pose: {'yes' if m.photo_pose_eligible else 'no'} | {m.name}")
