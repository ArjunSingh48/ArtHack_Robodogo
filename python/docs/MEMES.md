# Meme Memory — the robot discovers internet culture

The robot knows dogs through two cultural sources. Museum paintings show people and dogs over five
centuries; memes show dogs looking back at the camera, at the scroll, at us. The installation treats
both with **equal sincerity**: the same matcher interface, the same safety path, the same quiet voice,
the same memory card. The contrast between a Van Dyck greyhound and a dog drinking coffee in a burning
room is the point, and the robot never winks at it.

The primary presentation sends the selected meme image with `hehe or not hehe?`
and italic attribution. The longer dialogue, memory cards, and consented photos
described below remain experiments; see [UX.md](UX.md) for the primary encounter.

## How it fits the existing pipeline

```
SceneLibrary ─┬─ YamlArtworkRepository  (paintings, data/artworks)
              └─ MemeRepository         (memes, data/memes/annotations/*.json)

SceneMatcher(source) ─┬─ art      → WeightedRelationshipMatcher
                      ├─ meme     → MemeMatcher (same interface, its own reading)
                      ├─ both     → both rankings merged on the same 0–1 scale
                      └─ surprise → one archive chosen at random, wider near-tie pool
```

Both scene types expose the same surface: `id`, `kind`, `title`, `human: HumanAnnotation`,
`dog: DogAnnotation`. Everything downstream (planner, guard, robot, dialogue, Telegram, cards) works
on that surface, while each keeps its own metadata.

### Why a separate MemeMatcher

Paintings are mostly *dog-and-person* scenes. Memes are mostly *dog-and-camera* scenes: the visitor
stands where the camera stood. So `MemeMatcher` reads the visitor's body as an invitation to a **look**:

| Visitor | Invites | Example |
|---|---|---|
| sitting, forearms together (a cup) | `enduring` | This Is Fine |
| leaning forward | `peering` | Long Nose Borzoi |
| turned away | `regarding` (side-eye) | Side Eye Dog |
| standing, turned toward | `regarding` / `posing` | Doge |
| sitting, calm | `posing` | Cheems |

Gesture weighs more than exact posture for memes (`MemeMatcher` weights in `matching/matcher.py`).

## Annotation schema (one JSON file per meme)

```json
{
  "id": "meme:this-is-fine",
  "name": "This Is Fine",
  "also_known_as": ["On Fire"],
  "subject": "a cartoon dog in a hat",
  "year": "2013",
  "origin": "One factual sentence from a primary source.",
  "attribution": "Comic: KC Green, 'On Fire' (Gunshow, 2013).",
  "sources": [{"label": "NPR — …", "url": "https://…"}],
  "cultural_meaning": "What the meme means to the people who use it.",
  "human": {"posture": "sitting", "gestures": ["arms_holding"], "confidence": 0.85},
  "dog": {"posture": "sitting", "relation": "facing_viewer", "interaction": "enduring",
          "gaze_at_human": false, "reproducible": "yes", "robot_behavior": "sit", "confidence": 0.9},
  "image": {"rights": "reference_only", "license_note": "Copyrighted image; not stored or sent."},
  "reflection": {"en": "…", "ru": "…", "de": "…"},
  "curation_status": "approved",
  "discovered_via": "manual",
  "notes": "Curator notes."
}
```

* `human` — the visitor body that *summons* this meme (vocabulary: `HumanPosture`, `HumanGesture`).
* `dog` — the dog's posture, relation (`facing_viewer` for camera scenes), interaction (`regarding`,
  `posing`, `enduring`, `peering`, or any painting interaction), and the **permitted robot action**.
* `robot_behavior` must be a member of the existing vocabulary:
  `stillness, stand, sit, lie_down, stretch, greet, look_aside, peer`. Nothing else validates.
* `reflection` — the robot's own sincere sentence about this meme, per language.

**Adding a meme never touches robot-control code.** Drop a JSON file into `data/memes/annotations/`, run
`itu memes validate`, restart the bot. A meme can only *name* an approved behavior; it cannot define motion.

## Image provenance and presentation use

For this noncommercial presentation, the project owner selected the five familiar
memes themselves. Their actual files are included under `data/memes/presentation/`
with original creator/source attribution and download provenance. This selection
is recorded as `presentation_use`, separately from a license or a team recreation.

| `image.rights` | What the installation does |
|---|---|
| `presentation_use` (the five curated memes) | Uses the recorded local image for the primary presentation encounter and its Telegram delivery. Requires source URL, source page, retrieval date, checksum, and a note recording the project owner's selection. |
| `reference_only` | Experimental flows may name, describe and link the meme. No local image is stored or sent. Primary matching excludes it. |
| `licensed` | Shows `local_file`. Requires a `license_note` recording the permission (who granted it, when, terms). |
| `recreation` | Shows `local_file`, an original re-staging made by the team (e.g. the team's own dog photographed in the pose). Requires a `license_note`. |
| `unknown` | Drafts only. Never loaded. |

The schema keeps these statuses distinct. `reference_only` entries cannot point
to an image. `licensed`, `recreation`, and `presentation_use` need a local file and
their appropriate provenance note. Presentation files are exposed through
`local_image(..., presentation=True)`; ordinary photo-upload/card experiments keep
their previous reference behavior. Validation checks file decoding, Telegram photo
dimensions/size, and recorded checksums. Tests also check that every shipped meme
image belongs to an annotation.

The actual files are the Kabosu photograph, a sitting Cheems cutout template,
the captioned Side Eye Dog variant (“you think you can fool us?”), the October
2018 Lapsha photograph from @loopsnoot, and the archived This Is Fine lead image.
The latter retains its source's 300×168 resolution. Caption author information
unavailable in the Side Eye source is recorded honestly. Each annotation's
`image.source_url` identifies the exact downloaded file; `image.source_page`
identifies the page used to establish its provenance. No image was AI-recreated.

## Discovery with Imgflip

```bash
itu memes discover     # writes drafts to data/memes/drafts/ (never loaded)
itu memes validate     # schema, rights, robot-safety and TODO checks for approved memes
itu memes list [--drafts]
```

`discover` calls `GET https://api.imgflip.com/get_memes` (public, no key; verified 2026-10-24), keeps
dog-related templates not already curated, and writes a draft with `image.rights: "unknown"`,
`curation_status: "draft"` and an explicit notice. Imgflip lists only the ~100 templates popular right now.
On 2026-10-24 that included "This Is Fine" and "Buff Doge vs. Cheems", but not Side Eye Dog or the Borzoi,
so curators also add memes by hand.

To approve a draft: verify origin and attribution from primary sources; fill `human`/`dog`; choose a
behavior; decide image rights; write reflections; set `curation_status: "approved"`; move the file into
`annotations/`; run `itu memes validate`.

## Robot actions for memes

Memes use the same vocabulary as paintings. Two new gentle body poses came with this feature:

| Behavior | SDK call | Default |
|---|---|---|
| `look_aside` | `SportClient.Euler(0, 0, +0.25 rad)`, held 2.5 s, then back to `Euler(0, 0, 0)` | **off** |
| `peer` | `SportClient.Euler(0, +0.15 rad, 0)`, held 2.5 s, then back | **off** |

Angles are fixed constants in `robot/base.py`, capped at ±0.3 rad (Unitree documents roughly ±0.6–0.75), and
checked again by `check_args` in both adapters. They are **hardware-unverified**, including the sign
conventions. Test them with `itu robot-test --sequence --i-have-physical-estop`, then an operator
enables them with `/approve look_aside` / `/approve peer` (or `MOTION_ALLOWED_BEHAVIORS`). Until then the
planner falls back to `stand`, and the robot honestly says it couldn't quite manage it.

## Imitation photo (diptych), with consent

In the explicit experimental installation path
(`observe_installation(..., experimental=True)`), when `IMITATION_CAMERA_SOURCE`
points to a camera that can see the robot, moments get a **📷 Photograph the imitation**
button. The primary encounter has no such button. Pressing it asks for consent first;
only after **"Yes, one photo"** is a single frame captured. It is composed into a diptych —
*the memory* (the painting, a licensed/recreated meme image, or the typographic reference panel) beside
*the attempt* — and sent only to that visitor. The frame and the diptych exist only in memory.
Photos sent from elsewhere (photo mode) never get this button: the robot isn't there.

## Dialogue

The primary encounter uses `compose_encounter` (`encounter` locale section): one
`hehe or not hehe?` caption independent of optional short speech. Legacy
experimental dialogue remains in the `meme` locale section:

* first meme of a visit: "I found another kind of painting. Humans call it a meme."
* "I think this dog understands something I don't." (used when a meme has no reflection)
* "I have been practicing this expression."
* "Did I do the meme correctly?" … then, as always, "Is this us?"

## Sources used for the five curated memes (checked 2026-10-24)

Doge — Wikipedia "Doge (meme)"; Know Your Meme interview with Atsuko Sato.
Cheems — Wikipedia "Cheems"; Know Your Meme "Cheems".
This Is Fine — NPR (2023-01-16); Know Your Meme "This Is Fine".
Side Eye Dog — Know Your Meme editorial on the Side-Eyeing Dog / "sus dog".
Long Nose Borzoi — Know Your Meme "Borzoi Long Nose Dog"; NBC News on TikTok borzois.
