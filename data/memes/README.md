# The robot's second memory — dog memes

| Folder | What it is |
|---|---|
| `annotations/` | One JSON file per curated meme. Catalog approval and visual matching eligibility are separate. |
| `drafts/` | Drafts from `itu memes discover` (Imgflip). **Never loaded.** |
| `recreations/` | Images the project is allowed to show: team-made recreations or licensed files, each recorded in its annotation's `image.license_note`. Empty by default. |

No meme image is included in this repository. All five curated memes are `reference_only`: they are
described, credited and linked, never reproduced. See [`docs/MEMES.md`](../../docs/MEMES.md).

The five references are approved as **approximate photo poses** for the
`photographing_robot` event. A solo dog can qualify; the source need not depict a
human or ground contact. They remain excluded from ordinary human–dog relationship
matching. `human: unknown` with confidence zero records the lack of a human pose.

`photo_pose` records the scenario review; `dog.robot_behavior` records the action.
This review used the existing descriptions and annotations, not new inspection of
remote source images. `itu memes list` displays relationship eligibility and
photo-pose eligibility separately. The Buff Doge vs. Cheems draft is explicitly
rejected for this scenario and stays in `drafts/`.

See the [branch and behavior mapping](../../docs/MEMES.md) for all six decisions,
approximation limits, and the pending automatic photography detector. Rehearse:

```bash
itu simulate --photographing --seed 7 --another
```
