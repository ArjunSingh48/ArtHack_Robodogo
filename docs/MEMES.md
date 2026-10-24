# Meme Memory — the robot discovers internet culture

The robot knows dogs through two cultural sources. Museum paintings show people and dogs over five
centuries; memes show dogs looking back at the camera, at the scroll, at us. The installation treats
both with **equal sincerity**: the same matcher interface, the same safety path, the same quiet voice,
the same memory card. The contrast between a Van Dyck greyhound and a dog drinking coffee in a burning
room is the point, and the robot never winks at it.

> "I found another kind of painting. Humans call it a meme."

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

## Image rights policy

Famous meme images are copyrighted photographs or comics. Their owners are real people: Atsuko Sato
photographed Kabosu (Doge); Balltze's owner photographed him (Cheems); KC Green drew "On Fire".
**Being listed on Imgflip, Know Your Meme or a search engine is not permission to reproduce an image.**

| `image.rights` | What the installation does |
|---|---|
| `reference_only` (default, all five curated memes) | Names, describes and credits the meme; links to the source with a Telegram link preview. **No image is stored or sent.** Memory cards show a typographic reference panel. |
| `licensed` | Shows `local_file`. Requires a `license_note` recording the permission (who granted it, when, terms). |
| `recreation` | Shows `local_file`, an original re-staging made by the team (e.g. the team's own dog photographed in the pose). Requires a `license_note`. |
| `unknown` | Drafts only. Never loaded. |

The schema enforces this: `reference_only` files cannot point to an image; `licensed`/`recreation`
require both a file and a licence note; a test fails if any image file appears under `data/memes/`
without that record.

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

When `IMITATION_CAMERA_SOURCE` points to a camera that can see the robot (not the robot's own camera),
installation moments get a **📷 Photograph the imitation** button. Pressing it asks for consent first;
only after **"Yes, one photo"** is a single frame captured. It is composed into a diptych —
*the memory* (the painting, a licensed/recreated meme image, or the typographic reference panel) beside
*the attempt* — and sent only to that visitor. The frame and the diptych exist only in memory.
Photos sent from elsewhere (photo mode) never get this button: the robot isn't there.

## Dialogue

Same character, same restraint (`dialogue/locales/*.yaml`, section `meme`):

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
