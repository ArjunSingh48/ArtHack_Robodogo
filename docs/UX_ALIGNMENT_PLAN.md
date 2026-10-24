# Plan: align the repository with the good boy encounter

Saved 2026-10-24. Product authority: [UX contract](UX.md). Contributor rules:
[AGENTS.md](../AGENTS.md). This is the current six-step UX alignment plan;
[the earlier MCP integration plan](IMPLEMENTATION_PLAN.md) remains technical
background and does not override the agreed experience or this work order.

## Execution boundary

The user requested saving this plan and using subagents to carry out **step 1
only**, then stopping for review. Steps 2–6 are planned, not started or authorized
for automatic continuation in this task. Documentation changes do not establish
that the runtime implements the new experience.

## Confirmed decisions

- The robot is the main interface, with occasional timid speech and a physical
  finale. Telegram displays its selected archive image and short message; the
  web interface is for debugging.
- This is a presentation prototype. The computer plays the dog's voice;
  onboard robot audio integration is not required.
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
`src/is_this_us/notifier.py`, `tests/test_flows_e2e.py`; meme assets and
`data/memes/annotations/*.json` as needed for legitimate image delivery.

- Send one archive image, one short caption, and italic attribution in the primary
  flow. Remove its extra introductory messages, buttons, and voice notes.
- Keep setup, recipient pairing, privacy, and operator controls separate.
- Make console previews match Telegram output, escape metadata correctly, and
  report failed/ambiguous delivery accurately without replaying movement.
- Resolve the meme asset gap: the five current annotations are reference-only.
  Obtain suitable licensed/recreated assets and record their actual provenance;
  do not merely relabel existing rights metadata.

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

### 6. Align launchers and remaining documentation

Files: `run_simulation.sh`, `src/is_this_us/cli.py`,
`src/is_this_us/hardware_cli.py`, `README.md`, `docs/ARCHITECTURE.md`,
`docs/IMPLEMENTATION_PLAN.md`; adapt or introduce a small debug view only as needed.

- Make the primary rehearsal launch this encounter with mock hardware and
  substitute camera input. The current launcher runs the historical show.
- Any web debug view exercises the same encounter pipeline and exposes useful
  diagnostics; it does not become a visitor interface.
- Document one clear rehearsal path and one installation path. Label performance,
  open conversation, and other secondary modes as experiments.
- Reconcile earlier plans and readiness claims with implemented behavior.

Acceptance: a complete rehearsal follows the UX contract; launch instructions and
relevant checks pass. Record simulated, live-service, and hardware verification
separately. Preserve unrelated existing work.

## Progress

- Step 1: complete — UX contract and demo script aligned by subagents, reviewed,
  and checked for valid local links and whitespace. Automatic triggering and
  image-based final poses are confirmed. No runtime code changed.
- Steps 2–6: not started.

Work stops here for user review, as requested.

Run tests alongside the code changes they verify. Step 1 needs document review and
link/whitespace checks, not a new test suite or hardware/API calls.
