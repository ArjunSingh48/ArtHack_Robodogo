# The robot's second memory — dog memes

| Folder | What it is |
|---|---|
| `annotations/` | One JSON file per **approved** meme. These are loaded by the installation. |
| `drafts/` | Drafts from `itu memes discover` (Imgflip). **Never loaded.** |
| `presentation/` | Five familiar meme images selected by the project owner for this noncommercial presentation, each with creator/source attribution and download provenance in its annotation. |
| `recreations/` | Team-made recreations or licensed files, each recorded in its annotation's `image.license_note`. |

The five curated entries use `presentation_use`, which records the owner's
selection without claiming a license or recreation. The primary encounter can
send these local images; existing photo/card experiments still use references.
All downloaded bytes are unchanged. `image.source_url`, `image.source_page`,
`image.retrieved_on`, and `image.sha256` record each file's provenance. This Is
Fine retains its source's 300×168 resolution. Validate with `itu memes validate`.
See [`docs/MEMES.md`](../../docs/MEMES.md) for scope and attribution details.
