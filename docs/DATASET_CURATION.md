# Dataset curation: one person and one grounded dog

## Scope

This branch implements visual eligibility review, an approved artwork pilot collection, and corrected scene annotations. The artwork criteria below apply to relationship matching. A later, separate photography-event branch uses solo-dog meme imitations under the review policy in [MEMES.md](MEMES.md). Image storage, IIIF integration, matching weights, and robot control are outside these changes. Keep all existing source records and image files for traceability.

## Review instructions

1. Inspect the actual full image, not just its title, poem, existing labels, or a cropped subject. Count all visible humans and dogs, including background and partially visible figures. Do not crop away disqualifying figures. Use a larger view when a contact point or background figure is unclear.
2. Accept only exactly one human and exactly one dog, with the dog visibly supported by the ground or floor. Standing, sitting, lying, and walking on the ground qualify. Being held, sitting on a lap or furniture, or being airborne does not. A low position in the image alone is not evidence of ground contact.
   Prefer clearly distinguishable human and dog bodies. Reject overlap or occlusion that makes their separate poses hard to read, as in `met:437966`. A hand touching the dog does not by itself fail this criterion if both bodies remain distinguishable. Record the separation decision in the review reason; acceptance status controls inclusion.
3. Use `accepted`, `rejected`, or `needs_review`. If a required observation is uncertain, record an unknown value and use `needs_review`, unless another observed criterion already decisively rejects the image. Do not guess or accept to meet a target count. Record a concrete reason, reviewer, review date, and the image inspected.
4. Assess human posture and gestures, dog posture, relation to the human, interaction, gaze, and confidence from visual evidence. Use the existing vocabulary. Describe the depicted pose faithfully; keep robot approximation separate in reproducibility and robot behavior. Do not label walking as standing just because the robot stays still.
5. Preserve every museum ID and museum-provided title, artist, date, URLs, and rights. Correct interpretive notes in `data/artworks/curation_notes.yaml`; generate `annotations.yaml` through the build function. Keep rejected and unresolved candidates in the notes.
6. Aim for 6–10 accepted existing images with useful posture variety, but accept fewer if that is what the evidence supports. Report missing coverage instead of inventing labels or relaxing eligibility. New museum discovery is deferred until the existing audit is complete.

## Implementation instructions

- Add a structured eligibility review with human and dog counts, ground contact, status, reason, reviewer, review date, and image reference. Unknown counts/contact remain explicit. Acceptance requires counts of one, confirmed ground contact, and review provenance.
- Build only accepted entries. Unreviewed entries must not silently enter the approved collection. Validate review data before overwriting generated output. Preserve the current live museum metadata/rights path; an explicit offline rebuild may reuse existing verified metadata and its original verification date, with missing IDs reported as errors.
- Relationship-matching eligibility must fail closed for unreviewed or rejected production scenes, including legacy generated records and meme/mixed/surprise archive modes. Retain historical lookup where practical. Catalog or photo-pose approval is not evidence that an image meets this collection's criteria; absent pair review means ineligible for relationship matching. The photography branch requires its own accepted `photo_pose` review and never bypasses the pair criteria in ordinary matching.
- Keep synthetic test scenes clearly separate from claims about real-image review. Test accepted, rejected, unknown, missing-review, contradictory, empty-collection, and mixed-archive cases. Update obsolete collection-size assumptions. Preserve meaningful tests of scoring, rights, and delivery using explicit test fixtures where needed.
- Do not edit image files, `.gitignore`, matching weights, or movement behavior. Do not delete source candidates, rewrite Git history, publish, or push.

## Delegation and completion

The visual curator owns artwork notes and the audit findings. The implementation agent owns schema, build/repository eligibility, and focused implementation tests. An independent reviewer checks the visual decisions and reports disagreements without editing the curator's files. The coordinating agent resolves discrepancies, regenerates the approved collection, updates documentation and integration fixtures, and runs the relevant tests.

Completion means every existing artwork has a recorded visual decision, all accepted artworks have reviewed annotations, only accepted scenes can be selected, generated metadata preserves source identity and attribution, and automated checks pass. The final report must state the accepted count, uncertainties, coverage gaps, and whether metadata was rebuilt live or from the existing snapshot.

## Review result — 2026-10-24

Two AI subagents performed the initial visual curation and independent crosscheck. Following user corrections, all 24 source images have decisions: **5 accepted, 18 rejected, 1 needs review**. The user confirmed the seated human in `cma:169662` and excluded `met:437966` because the human and dog overlap. Other visual decisions remain AI judgments. The review fields in `curation_notes.yaml` record provenance, counts, reasons, and inspected filenames.

| Accepted artwork ID | Human posture | Dog posture | Annotation notes |
|---|---|---|---|
| `met:437173` | standing | sitting | Corrected dog posture; hand resting on head. |
| `met:436252` | standing | sitting | Corrected dog posture; head near the person's hand. |
| `met:437890` | standing | standing | Hand at neck/shoulder; `accompanying` avoids falsely labeling a head touch. |
| `met:436077` | sitting | sitting | Corrected dog posture and gaze. |
| `cma:169662` | sitting | lying | User confirmed the woman is sitting on a chair. |

`met:437648` remains `needs_review`: the dark dog's extended legs and obscured body make ground contact and pose uncertain. It is excluded from the generated collection. The 18 rejections show multiple humans/dogs, carried dogs, dogs supported by furniture, or insufficient separation of the human and dog. Rejected interpretive labels remain historical; they are not reviewed labels for training or matching.

The pilot covers three standing humans and two seated humans; three sitting dogs, one standing dog, and one lying dog. It has no confirmed kneeling, crouching, leaning-forward, reclining, or lying human examples. Similarity scoring can still return an approximate match for such observations; exact posture coverage has not been established and matching weights were not tuned.

The five existing meme references have no pair-eligibility review and remain excluded from relationship matching. They now have separate `photo_pose` scenario reviews for random imitation when a visitor photographs the robot; these allow solo, cropped, or drawn dogs. Explicit `/meme` archive requests outside that event still use the existing fallback to eligible art. See [MEMES.md](MEMES.md) for the mapping audit, trigger contract, and pending automatic detector.

## Rebuild and validation

```bash
itu artworks build --offline
itu artworks validate
itu artworks list
pytest
```

The delivered `annotations.yaml` was generated offline from `museum_metadata.yaml`, which retains all 24 candidates' original museum metadata and rights-verification dates. The offline build makes no fresh museum or rights-verification claim. The default build still fetches live museum metadata; update the preserved snapshot deliberately when refreshing that source evidence.

Invalid review/schema data, missing accepted IDs in the snapshot, and rights/build failures prevent replacement of the generated file. Missing or unresolved reviews never become eligible by default. Repository `all()` exposes relationship-eligible scenes; `photo_poses()` exposes separately reviewed meme imitations. `all_records()` and `get()` retain access to records loaded in that repository. All original candidate metadata and notes remain in the source files even when an artwork is absent from the generated collection.

Image files and storage remain unchanged. IIIF, cache migration, new image discovery, and ranking calibration are deferred.
