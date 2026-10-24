# The robot's memory — curated artworks

| File | What it is |
|---|---|
| `curation_notes.yaml` | Hand annotations: human posture, dog posture, relation, interaction, robot feasibility, original poem. Edit this. |
| `annotations.yaml` | **Generated** by `itu artworks build` from the notes + live museum API metadata. Do not edit by hand. |
| `images/` | Cached web-size images (`itu artworks cache`) so the installation works offline. |

## Rights

Every artwork's rights were checked against the museum API at build time (`rights.verified_on`):

* **The Metropolitan Museum of Art** — objects with `isPublicDomain: true`, released under the
  Met Open Access policy (CC0). <https://www.metmuseum.org/about-the-met/policies-and-documents/open-access>
* **Cleveland Museum of Art** — records with `share_license_status: CC0`.
  <https://www.clevelandart.org/open-access>

Credit is always shown with the artwork (artist, title, date, museum, link), even where not legally required.
The poems in `curation_notes.yaml` are original text written for this project (MIT, like the code).
