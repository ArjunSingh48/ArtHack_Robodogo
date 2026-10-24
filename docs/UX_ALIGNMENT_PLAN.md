# Plan: align the repository with the good boy encounter

Saved 2026-10-24. Product authority: [UX contract](UX.md). Contributor rules:
[AGENTS.md](../AGENTS.md). This is the current six-step UX alignment plan;
[the earlier MCP integration plan](IMPLEMENTATION_PLAN.md) remains technical
background and does not override the agreed experience or this work order.

## Execution boundary

Steps 1 and 2 were authorized and completed separately. The current request
authorizes **step 3**, delegated to a subagent and reviewed in the main thread.
Steps 4–6 remain planned. Documentation changes do not establish that the runtime
implements the new experience. The presentation clarification defines the setup
for later integration; the current meme direction permits familiar meme images
in this noncommercial presentation with accurate credits and provenance.

## Confirmed decisions

- The robot is the main interface, with occasional timid speech and a physical
  finale. Telegram displays its selected archive image and short message.
- Presentation is part of the intended experience: the dog and a computer
  connected to a beamer. The presentation flow uses the main dog encounter;
  the audience view and operator/debug tools have separate roles.
- The computer plays the dog's voice; onboard robot audio integration is not
  required.
- The final pose is a supported pose based on the selected image, not one fixed
  good-boy pose. Hold it for 10 seconds after reaching/settling into it, unless
  interrupted by stop or safety controls.
- A stable human pose automatically triggers the encounter after operator setup
  and recipient pairing. The visitor does not trigger it through Telegram.
- One sustained pose produces one encounter. Observation must reset before a new
  encounter; movement and delivery must not loop while a person holds still.

## Ordered work

### 1. Finish the experience definition

Files: `docs/UX.md`, `docs/DEMO_SCRIPT.md`; retain the concise rules in `AGENTS.md`.

- Define one primary encounter, interface roles, local camera data flow, exact
  caption examples, occasional speech, image-based pose, and 10-second hold.
- Replace the old gallery script's visitor menus, feedback buttons, memory cards,
  and optional conversations with this encounter.
- Specify automatic stable-pose triggering, completion/reset, and failure behavior
  without pretending the observation loop already exists.
- Keep historical performance and talk modes as separate experiments.

Acceptance: the UX contract and rehearsal script describe the same complete
encounter; confirmed decisions and implementation gaps are explicit; local links
and diffs are checked. No runtime changes. **Stop for user review after this step.**

### 2. Separate robot speech from Telegram captions

Files: `src/is_this_us/dialogue/engine.py`, `src/is_this_us/dialogue/locales/*.yaml`,
`tests/test_dialogue.py`.

- Produce independent optional speech and image-caption outputs.
- Use the approved short good-boy lines; make silence a valid outcome.
- Keep painting and meme captions distinct. Never copy long narration into chat.

Acceptance: tests cover approved captions, separate speech, silent encounters,
and the supported locale/fallback behavior. Existing experiments remain usable.

### 3. Simplify Telegram delivery

Files: `src/is_this_us/bot/flows.py`, `src/is_this_us/bot/app.py`,
`src/is_this_us/notifier.py`, `src/is_this_us/image_message.py`,
`tests/test_flows_e2e.py`, `tests/test_primary_delivery.py`; meme assets,
`data/memes/annotations/*.json`, meme schema/repository/validation, and tests.
Small orchestrator and talk-tool changes distinguish primary delivery from
existing experimental flows; automatic observation remains in step 5.

- Send one archive image, one short caption, and italic attribution in the primary
  flow. Remove its extra introductory messages, buttons, and voice notes.
- Keep setup, recipient pairing, privacy, and operator controls separate.
- Make console previews match Telegram output, escape metadata correctly, and
  report failed/ambiguous delivery accurately without replaying movement.
- Resolve the meme asset gap with the five familiar meme images for this
  noncommercial presentation. Per the user's updated direction, licensing
  clearance is not a blocker. Record source URLs, original creator credits, and
  actual image provenance under an explicit presentation-use status; do not
  claim a license was obtained or relabel the images as project recreations.

Acceptance: message formatting and destination tests pass; no cross-recipient
delivery or unintended extra messages. Separately verify real Telegram delivery.

### 4. Implement the physical finale

Files: `src/is_this_us/motion/planner.py`, `src/is_this_us/motion/guard.py`,
`src/is_this_us/robot/base.py`, `src/is_this_us/robot/mock.py`,
`src/is_this_us/robot/unitree_go2.py`, `src/is_this_us/robot/zhaw_bridge.py`,
`src/is_this_us/domain.py` if needed, `src/is_this_us/config.py`, `.env.example`.
Tests: `tests/test_motion_safety.py`, `tests/test_go2_adapter.py`,
`tests/test_zhaw_bridge.py`.

- Select a supported, holdable pose based on the matched image.
- Represent entry, settling, a 10-second hold, and safe completion explicitly.
- Preserve interruption, arming, motion ownership, and honest execution status.
- Reconcile transitions and the hold with the current 12-second default timeout;
  do not implement this as an uninterruptible sleep.

Acceptance: meaningful timing/interruption tests pass with mock hardware; the
actual unit separately qualifies pose entry, holding, stop, and completion.

### 5. Connect the complete automatic encounter

Files: `src/is_this_us/orchestrator.py`, `tests/test_flows_e2e.py`,
`src/is_this_us/vision/frame_sources.py` as needed. Introduce a focused stable-pose
observer/tracker module if necessary; extend `tests/test_vision.py` for it.
Review recipient/session ownership in `src/is_this_us/storage.py` and
`src/is_this_us/bot/flows.py` where needed by automatic observation.

- Run capture → stable human pose → match → optional speech and image delivery →
  final pose and hold → ready after reset.
- Observe locally after operator setup; do not require a visitor command or reply.
- Ensure a sustained pose triggers once, participant loss/ambiguity prevents stale
  motion, and pose change or departure/reacquisition can enable a later encounter
  only after completion and cooldown.
- Keep exclusive ownership through the hold and cancellation effective across
  stages. Delivery retries must never repeat robot movement.

Acceptance: complete mock encounters cover stability, duplicate suppression,
participant loss, stop, delivery failure, reset, and correct recipient routing.
Retain the existing coarse matcher initially; this step adds temporal observation,
not a new landmark-similarity retrieval system.

### 6. Align presentation, launchers, and remaining documentation

Files: `run_simulation.sh`, `src/is_this_us/cli.py`,
`src/is_this_us/hardware_cli.py`, `README.md`, `docs/ARCHITECTURE.md`,
`docs/IMPLEMENTATION_PLAN.md`; adapt existing projector components in
`src/is_this_us/performance/stage.py` or add a focused presentation view as needed.

- Make the primary rehearsal launch this encounter with mock hardware and
  substitute camera input. The current launcher runs the historical show.
- Build the computer-and-beamer presentation around the same encounter pipeline,
  showing its selected archive image and credit. Reuse existing projector
  components where useful; keep operator/debug controls in a separate view.
- Any web debug view exercises the same pipeline and exposes useful diagnostics.
  Neither view adds screen actions required of the visitor.
- Document one clear rehearsal path and one installation path. Label the historical show,
  open conversation, and other secondary modes as experiments.
- Reconcile earlier plans and readiness claims with implemented behavior.

Acceptance: a complete rehearsal follows the UX contract; launch instructions and
relevant checks pass. Rehearse the main scenario with the computer and beamer.
Record simulated, live-service, presentation-setup, and hardware verification
separately. Preserve unrelated existing work.

## Progress

- Step 1: complete — UX contract and demo script aligned by subagents, reviewed,
  and checked for valid local links and whitespace. Automatic triggering and
  image-based final poses are confirmed. No runtime code changed.
- Step 2: implemented — `DialogueEngine.compose_encounter` returns a short caption
  and independent optional speech. Painting and meme captions remain distinct;
  English, Russian, and German speech and English fallback are covered by tests.
  Silence is the default unless the caller requests speech; frequency remains an
  open integration decision. Legacy `compose` and experimental dialogue remain
  available. Step 3 now connects captions to installation delivery; the complete
  automatic encounter remains in step 5.
  Verification: 75 tests passed across dialogue, memes, flows, and speech. The
  broader regression run plus the localhost-permitted web rerun passed 169 tests;
  four talk/show SDK-schema tests remain blocked by the missing optional `openai`
  package. `git diff --check` passed. No visitor choice was added.
- Step 3: implemented and reviewed — installation encounters send one archive
  photo with an approved short caption and italic attribution, without an intro,
  buttons, or voice note. Console previews use the same caption. Recipient checks
  reject wrong, expired, or forgotten interactions; bounded delivery distinguishes
  rejection from unknown outcomes, and retries never recapture or replay movement.
  Concurrent/cancelled sends are covered. Photo-upload and explicit installation
  experiments retain their existing dialogue and callbacks.
  All five familiar memes now have local presentation assets with source URLs,
  creator credits, retrieval dates, and checksums. This Is Fine retains the source
  image's 300×168 resolution; projection quality should be reviewed in step 6.
  Verification: 115 focused tests passed across dialogue, flows, primary delivery,
  memes, privacy, speech, and matching; meme archive validation, added local
  documentation links, and `git diff --check` passed. A broader run passed 123
  tests; two talk tests were blocked by the missing optional `openai` package.
  Installation speech remains silent pending the cadence decision. No visitor
  choice was added. Live Telegram delivery remains unverified.
- Steps 4–6: not started.
- Presentation clarification: documentation now includes the dog + computer +
  beamer setup and a presentation flow based on the main encounter. Runtime
  integration remains planned in step 6; no visitor choice was added.

Step 3 does not include live Telegram sends or physical robot verification.

Run tests alongside the code changes they verify. Step 1 needs document review and
link/whitespace checks, not a new test suite or hardware/API calls.
