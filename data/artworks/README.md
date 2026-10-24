# The robot's memory — curated artworks

| File | What it is |
|---|---|
| `curation_notes.yaml` | Visual eligibility reviews and scene annotations for all 24 candidates. Edit this. |
| `museum_metadata.yaml` | Preserved museum metadata for all 24 candidates, including original rights-verification dates. Used by offline builds. |
| `annotations.yaml` | **Generated**, accepted scenes only. Do not edit by hand. |
| `images/` | Cached web-size images (`itu artworks cache`) so the installation works offline. |

The pilot collection requires exactly one visible human and one visible dog,
with the dog supported by the ground or floor and both bodies clearly distinguishable.
Overlap that obscures their separate poses is a reason for exclusion. The full review procedure and
delegation instructions are in [Dataset curation](../../docs/DATASET_CURATION.md).
Missing, rejected, or unresolved reviews are excluded from matching.

To rebuild from the preserved metadata without network access:

```bash
itu artworks build --offline
itu artworks validate
itu artworks list
```

`itu artworks build` (without `--offline`) fetches museum metadata and verifies
rights live. Offline builds retain the snapshot's original verification dates;
they do not reverify rights. Build errors leave the previous generated file
intact. All candidate notes, source metadata, and existing images are retained.

## Rights

The preserved records state when rights were checked against the museum API
(`rights.verified_on`):

* **The Metropolitan Museum of Art** — objects with `isPublicDomain: true`, released under the
  Met Open Access policy (CC0). <https://www.metmuseum.org/about-the-met/policies-and-documents/open-access>
* **Cleveland Museum of Art** — records with `share_license_status: CC0`.
  <https://www.clevelandart.org/open-access>

Credit is always shown with the artwork (artist, title, date, museum, link), even where not legally required.
The poems in `curation_notes.yaml` are original text written for this project (MIT, like the code).
