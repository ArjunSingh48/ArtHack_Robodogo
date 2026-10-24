# Shared project instructions

## Product direction

Read `docs/UX.md` before changing visitor-facing behavior, dialogue, Telegram
messages, robot interaction timing, or web UI. It is the canonical UX contract.
Older feature descriptions and implementation plans describe existing or proposed
capabilities; they do not override that contract. Follow explicit user changes to
the direction and update the contract in the same change.

- The robot is the main communication interface: its body and occasional voice
  carry the encounter.
- Telegram receives the robot's selected painting or meme, one short caption,
  and italic image attribution. Keep onboarding minimal and operator controls
  separate from those image messages.
- The web interface is currently a debugging tool, including substituting for
  unavailable robot-camera input. It is not a required visitor screen.
- The final expressive pose is held for 10 seconds, interruptible by existing
  stop and safety controls. Do not equate command acceptance with reaching a pose.
- Do not add visitor modes, menus, feedback buttons, narration, or conversation
  flows unless the task explicitly asks for them. Existing experimental features
  do not automatically belong in the primary experience.
- Keep spoken lines and Telegram captions separate. Preserve the approved tone
  and examples in `docs/UX.md`; do not expand image captions into explanations.

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
