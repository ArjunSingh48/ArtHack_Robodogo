# Shared project instructions

## Product direction

Read `docs/UX.md` before changing visitor-facing behavior, dialogue, Telegram
messages, robot interaction timing, presentation flow, or web UI. It is the
canonical UX and presentation contract.
Older feature descriptions and implementation plans describe existing or proposed
capabilities; they do not override that contract. Follow explicit user changes to
the direction and update the contract in the same change.

- The robot is the main communication interface: its body and occasional voice
  carry the encounter. For this presentation prototype, the computer plays the
  dog's voice; onboard robot audio is not required.
- Telegram receives the robot's selected painting or meme, one short caption,
  and italic image attribution. Keep onboarding minimal and operator controls
  separate from those image messages.
- Presentation is part of the intended experience: a robot dog and a computer
  connected to a beamer. Build the presentation flow around the main dog
  interaction scenario, with projection supporting the audience's view of it.
- Keep the audience presentation view separate from operator/debug tools,
  including substitute camera input. A web view may serve either role; visitors
  should not need to operate a screen to take part.
- The final expressive pose is held for 10 seconds, interruptible by existing
  stop and safety controls. Do not equate command acceptance with reaching a pose.
- Do not add visitor modes, menus, feedback buttons, narration, or conversation
  flows unless the task explicitly asks for them. Existing experimental features
  do not automatically belong in the primary experience.
- Keep spoken lines and Telegram captions separate. Preserve the approved tone
  and examples in `docs/UX.md`; do not expand image captions into explanations.
- Use the familiar memes in this noncommercial presentation without making
  licensing clearance a blocker. Keep original creator/source credits and image
  provenance accurate; presentation use must not be labeled as a license or an
  original recreation.

## Implementation and review

- Preserve the motion guard, approved behavior vocabulary, operator arming, and
  stop handling. Character dialogue and image content must not issue arbitrary
  robot commands. Hardware-unverified behavior must remain labeled as such.
- Keep visitor camera frames local and transient in the primary flow. Telegram
  receives the selected archive image, not the visitor frame.
- Keep the UX contract, actual behavior, and README consistent. Mark gaps
  explicitly; documentation of a requirement is not proof of implementation.
- Make focused changes and preserve unrelated work. Do not delete existing
  experimental modes solely because they are outside the primary journey.
- Run checks appropriate to the change. For code, use `.venv/bin/python -m pytest`
  with the relevant tests; run archive validation when changing archive data.
- In review, state which step of the journey changes, whether it introduces a
  visitor choice, and what was verified. Distinguish simulation, live service
  integration, and physical robot verification.
