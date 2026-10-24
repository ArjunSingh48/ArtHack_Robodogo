"""Presentation rounds — one per era: the person takes any pose, the robot picks the closest picture
from that era, echoes the painted dog next to them, and a photo of the two goes to the beamer and
Telegram (with consent). Also the images the robot makes itself: the meme (internet round) and the
rescue card (finale).

Everything here is local and deterministic given its inputs. Picking uses only what the curators
wrote (catalog `human` / `dog` / `dog_side` / pose tags, or the matcher annotations) and the coarse
posture the camera reads — no similarity scores are invented or shown.
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from .catalog import PerformanceCatalog, ShowArtifact

# camera posture (HumanPosture value) → the round's coarse pose group
POSE_GROUP = {"standing": "standing", "leaning_forward": "standing", "sitting": "sitting",
              "kneeling": "kneeling", "crouching": "kneeling", "reclining": "lying", "lying": "lying"}
NEAR = {("sitting", "kneeling"), ("kneeling", "lying"), ("sitting", "lying"), ("kneeling", "standing")}
BEHAVIOR_FOR_TAGS = (  # painted dog's tags → a supported robot pose (first match wins)
    ({"at_feet", "curled", "lying", "resting", "under_chair"}, "lie_down"),
    ({"jumping_up", "standing_up", "greeting"}, "greet"),
    ({"running", "hunting"}, "stretch"),
    ({"sitting", "on_lap", "beside"}, "sit"),
)
# the internet round has no archive picture: the robot just echoes the person's posture
MEME_BEHAVIOR = {"standing": "greet", "sitting": "sit", "kneeling": "stretch", "lying": "lie_down"}


def pose_group(posture: str | None) -> str:
    return POSE_GROUP.get(str(posture or ""), "")


def human_pose(a: ShowArtifact, art_repo=None) -> str:
    """What the person in the picture does (curated; never guessed from pixels)."""
    if a.human:
        return a.human
    scene = art_repo.get(a.id) if art_repo is not None else None
    if scene is not None:
        return pose_group(str(getattr(scene.human.posture, "value", scene.human.posture)))
    tags = set(a.pose_tags)
    if a.subject != "human_and_dog":
        return ""
    for g in ("kneeling", "sitting"):
        if g in tags:
            return g
    if tags & {"standing", "accompanying", "walking", "hunting", "running", "touching_head"}:
        return "standing"
    return ""


def behavior_for(a: ShowArtifact | None, art_repo=None) -> str:
    """A supported robot pose that echoes the painted dog."""
    if a is None:
        return "stand"
    if a.dog:
        return a.dog
    if a.interactive and a.interactive.robot_behavior:
        return a.interactive.robot_behavior
    scene = art_repo.get(a.id) if art_repo is not None else None
    if scene is not None:
        return str(getattr(scene.dog.robot_behavior, "value", scene.dog.robot_behavior))
    tags = set(a.pose_tags)
    for keys, b in BEHAVIOR_FOR_TAGS:
        if tags & keys:
            return b
    return "stand"


def side_for(a: ShowArtifact | None, art_repo=None) -> str:
    """Where the robot goes next to the person (as the audience sees it)."""
    if a is None:
        return "left"
    if a.dog_side:
        return a.dog_side
    if a.interactive and a.interactive.robot_side:
        return a.interactive.robot_side
    scene = art_repo.get(a.id) if art_repo is not None else None
    if scene is not None:
        rel = str(getattr(scene.dog.relation, "value", scene.dog.relation))
        return "front" if rel in ("at_feet", "in_front") else "left"
    return "front" if set(a.pose_tags) & {"at_feet", "curled"} else "left"


@dataclass
class Pick:
    artifact: ShowArtifact
    why: str  # for the operator only: which curated facts made it the closest


def candidates(catalog: PerformanceCatalog, era: str) -> list[ShowArtifact]:
    """Pictures of this era the robot may pick and send: open-access art with a local image."""
    return [a for a in catalog.for_chapter(era) if a.kind == "art" and a.displayable]


def pick_for_round(catalog: PerformanceCatalog, era: str, posture: str | None, *, head_tilt: bool = False,
                   gestures: set[str] | None = None, exclude: set[str] | None = None,
                   rng: random.Random | None = None) -> Pick | None:
    """The closest picture of this era to how the person is posing. None if the era has no pictures."""
    group = pose_group(posture)
    gestures = gestures or set()
    pool = candidates(catalog, era)
    if not pool:
        return None
    fresh = [a for a in pool if a.id not in (exclude or set())] or pool

    def score(a: ShowArtifact) -> tuple[float, list[str]]:
        why, s = [], 0.0
        hp = human_pose(a, catalog.art_repo)
        if group and hp == group:
            s += 3
            why.append(f"person {hp} in both")
        elif group and hp and ((group, hp) in NEAR or (hp, group) in NEAR):
            s += 1.5
            why.append(f"person {hp} ≈ {group}")
        if a.subject == "human_and_dog":
            s += 0.5
        else:
            s -= 1
        tags = set(a.pose_tags)
        if "extending_arm" in gestures and tags & {"arms_raised", "hunting", "pointing", "open_hand", "touching_head"}:
            s += 1
            why.append("an arm reaching out")
        if "arms_holding" in gestures and tags & {"holding", "in_arms", "arms_holding", "held"}:
            s += 1
            why.append("arms holding")
        if head_tilt and tags & {"head_tilted"}:
            s += 0.5
            why.append("head tilted")
        return s, why

    scored = [(score(a), a) for a in fresh]
    best = max(s for (s, _), _ in scored)
    top = [(w, a) for (s, w), a in scored if s == best]
    why, a = (rng or random).choice(top) if len(top) > 1 else top[0]
    return Pick(a, ", ".join(why) or "closest available in this era")


def montage_order(catalog: PerformanceCatalog, era: str, chosen: ShowArtifact | None) -> list[ShowArtifact]:
    """What flashes by while the robot 'remembers': the era's other pictures (the chosen one comes last,
    as the reveal)."""
    return [a for a in candidates(catalog, era) if chosen is None or a.id != chosen.id]


# ---------------------------------------------------------------------- telegram captions
def credit_line(kind: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    what = {"photo": "A participant and a robot dog. Photo taken with consent",
            "meme": "Made by the robot dog from a photo taken with consent",
            "rescue": "A participant and a robot dog, after the rescue. Photo taken with consent"}[kind]
    return f"{what}. Is This Us?, {when.day} {when:%B %Y}."


def photo_caption(caption: str, kind: str, when: datetime | None = None) -> str:
    """Caption for an image the robot made with the participant: short line + italic credit (HTML)."""
    return f"{html.escape(caption)}\n\n<i>{html.escape(credit_line(kind, when))}</i>"


# ---------------------------------------------------------------------- images the robot makes
MEME_FONTS = [
    "/System/Library/Fonts/Supplemental/Comic Sans MS Bold.ttf",
    "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
    "C:/Windows/Fonts/comicbd.ttf",
    "C:/Windows/Fonts/comic.ttf",
    "/Library/Fonts/Comic Sans MS.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Comic_Sans_MS_Bold.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
MEME_COLORS = [(255, 64, 160), (90, 230, 90), (60, 200, 255), (255, 220, 40), (255, 140, 30), (190, 120, 255)]


def meme_phrases(lines, group: str, rng: random.Random) -> list[str]:
    """2 phrases about the pose + 2 general ones + "wow" (all from lines.<lang>.yaml → meme_phrases)."""
    data = lines.data.get("meme_phrases") or {}
    own = list(data.get(group) or [])
    general = list(data.get("any") or [])
    rng.shuffle(own)
    rng.shuffle(general)
    out = own[:2] + [g for g in general if g not in own][:2]
    return out[:5] + [str(data.get("wow", "wow"))]


def make_meme(photo: bytes, phrases: list[str], *, font_path: str = "", seed: int = 0) -> bytes:
    """The internet round's picture: the consented photo with scattered doge-style phrases.
    Team-made (no third-party meme image is used)."""
    from PIL import Image, ImageDraw

    from ..cards import _font

    img = Image.open(BytesIO(photo)).convert("RGB")
    w = 1600
    img = img.resize((w, int(img.height * w / img.width)))
    d = ImageDraw.Draw(img)
    rng = random.Random(seed)
    size = max(40, min(img.width // 19, img.height // 10))
    font = _font(MEME_FONTS, size, font_path)
    colors = MEME_COLORS[:]
    rng.shuffle(colors)
    n = max(1, len(phrases))
    band = (img.height * 0.88) / n  # one horizontal band per phrase: they never overlap
    left_first = rng.random() < 0.5
    for i, text in enumerate(phrases):
        tw = d.textlength(text, font=font)
        y = int(img.height * 0.05 + i * band + rng.uniform(0, max(0.0, band - size * 1.2)))
        margin = int(img.width * rng.uniform(0.04, 0.14))
        x = margin if (i % 2 == 0) == left_first else int(img.width - tw - margin)
        x = max(12, min(x, int(img.width - tw - 12)))
        d.text((x, y), text, font=font, fill=colors[i % len(colors)], stroke_width=max(2, size // 14),
               stroke_fill=(20, 20, 20))
    out = BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def make_rescue_card(photo: bytes, *, heading: str, note: str, font_path: str = "") -> bytes:
    """The finale's picture: the consented photo on a museum-label card."""
    from PIL import Image, ImageDraw

    from ..cards import ITALIC_CANDIDATES, SANS_CANDIDATES, _font, fit
    from .composer import THEMES, _paw

    t = THEMES["ivory"]
    W, H = 1800, 1400
    card = Image.new("RGB", (W, H), t["bg"])
    d = ImageDraw.Draw(card)
    pic = fit(Image.open(BytesIO(photo)).convert("RGB"), W - 240, H - 420)
    px, py = (W - pic.width) // 2, 110
    card.paste(pic, (px, py))
    d.rectangle([px - 1, py - 1, px + pic.width, py + pic.height], outline=t["hair"], width=1)
    big = _font(ITALIC_CANDIDATES, 96, font_path)
    small = _font(SANS_CANDIDATES, 26)
    d.text(((W - d.textlength(heading, font=big)) / 2, H - 270), heading, font=big, fill=t["ink"])
    d.text(((W - d.textlength(note, font=small)) / 2, H - 140), note, font=small, fill=t["muted"])
    _paw(d, W // 2, H - 60, 12, t["accent"])
    out = BytesIO()
    card.save(out, format="JPEG", quality=92)
    return out.getvalue()
