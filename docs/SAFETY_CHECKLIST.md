# Safety checklist — live robot

A Telegram `/stop` is a **software** stop. It is **not** an emergency stop.

## Before opening
- [ ] A trained operator is present for the entire session, holding the Unitree remote / physical e-stop.
- [ ] Exhibition area marked on the floor (≥ 1.5 m clear radius around the robot); barrier or rope if children attend.
- [ ] Floor flat, dry, non-slip; no cables in the robot's footprint.
- [ ] Battery ≥ `MIN_BATTERY_PERCENT` (default 25 %). Spare battery charged.
- [ ] `itu robot-check` shows fresh state, error code 0, battery reading.
- [ ] Each behavior tested once with `itu robot-test <behavior> --i-have-physical-estop` with nobody inside the area.
- [ ] Camera-notice sign visible at the entrance (posture only, no recording, no face recognition).
- [ ] `.env`: `APP_MODE=installation`, `ROBOT_MODEL=go2`, `ROBOT_MOTION_ENABLED=true` only on show days.
- [ ] `look_aside` and `peer` tested once each (direction noted) before any `/approve`. Leave them off if unsure.
- [ ] If an imitation camera is used: it watches the robot area only; visitors are told photos happen only on request.

- [ ] Talk mode: microphone sign visible ("your voice is sent to OpenAI to answer you; nothing is recorded here").
- [ ] Talk mode with the real robot: `itu talk --robot go2 … --i-have-physical-estop`; the operator stays by the robot.

## During the show
- [ ] Operator arms with `/arm` only when the area is clear and they are watching.
- [ ] Visitors interact from the edge of the marked area; the robot never walks (no `Move` in the code).
- [ ] Any unexpected behavior → physical stop first, then `/disarm`.
- [ ] After any failure, the guard disarms automatically; investigate before `/arm` again.

## Closing
- [ ] `/disarm`, `/close`, robot lowered (StandDown via remote), powered off.
- [ ] `data/runtime/` is never copied off the machine; memories stay until visitors `/forget`.

## Built-in software guards (do not replace the above)
Static hardware gates · operator arming · operator approval of body poses · Euler angles capped at ±0.3 rad · one motion at a time · cooldown · per-motion timeout →
software stop · battery / error / state-freshness checks · stop latch requiring re-arm · photo flow
never moves the real robot · approved-call allowlist in the Go2 adapter.
