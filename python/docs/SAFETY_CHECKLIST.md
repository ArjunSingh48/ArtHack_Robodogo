# Safety checklist — live robot

A Telegram `/stop` is a **software** stop. It is **not** an emergency stop.

## Before opening
- [ ] A trained operator is present for the entire session, holding the Unitree remote / physical e-stop.
- [ ] Exhibition area marked on the floor (≥ 1.5 m clear radius around the robot); barrier or rope if children attend.
- [ ] Floor flat, dry, non-slip; no cables in the robot's footprint.
- [ ] Battery ≥ `MIN_BATTERY_PERCENT` (default 25 %). Spare battery charged.
- [ ] `itu robot-check` shows fresh state, error code 0, battery reading (direct SDK), or ✓ bridge answers / actions / camera (ZHAW bridge).
- [ ] ZHAW bridge: battery is not reported — check it in the Unitree app before and during the show. Their MCP server stays off.
- [ ] Each behavior tested once with `itu robot-test <behavior> --i-have-physical-estop` with nobody inside the area.
- [ ] Camera-notice sign visible at the entrance (posture only, no recording, no face recognition).
- [ ] `.env`: `APP_MODE=installation`, `ROBOT_MODEL=go2`, `ROBOT_MOTION_ENABLED=true` only on show days.
- [ ] `look_aside` and `peer` tested once each (direction noted) before any `/approve`. Leave them off if unsure.
- [ ] If an imitation camera is used: it watches the robot area only; visitors are told photos happen only on request.

- [ ] Talk mode: microphone sign visible ("your voice is sent to OpenAI to answer you; nothing is recorded here").
- [ ] Talk mode with the real robot: `itu talk --robot go2 … --i-have-physical-estop`; the operator stays by the robot.

## Performance mode (`itu show`)
- [ ] Default is a **stationary** robot: no `--motion`. The story never needs the robot to move.
- [ ] If motion cues are used: `SHOW_MOTION_ENABLED=true`, all gates above, `--motion --i-have-physical-estop`,
      a second person with the remote. Cues are only `stand` (BalanceStand) through the guard.
- [ ] The participant's spot is ≥ 1.5 m from the robot; nobody needs to touch or approach it.
- [ ] Photo consent and send confirmation are given by a person, not by the software (§7 of docs/PERFORMANCE.md).
- [ ] `/cancel` (or `q`) ends the performance and issues a software stop — still **not** an e-stop.

### Walking to the participant (`itu show --walk`)
- [ ] Tested first with nobody near: `itu show walk-test --sim`, then `--calibrate`, then a real `walk-test`.
- [ ] Clear floor between the robot's start and the chair; no bags, cables, children's feet.
- [ ] The person who holds the remote stands where they can see the robot AND the participant.
- [ ] The participant is seated/standing still before Ready is pressed; tell them the robot will come.
- [ ] Anything unexpected: physical stop first, then End / `/cancel` (stops the walk, software stop).

### The browser show with the real robot (`itu web-link`, README → The real robot)
- [ ] Bridge started with `python/scripts/start_bridge.sh` (10 s SDK timeout); `itu web-link` printed `ARMED`.
- [ ] The page's badge says **REAL GO2 · armed**; the operator knows **Esc** stops the dog and pauses the show.
- [ ] Only one page drives (a second tab is refused). Reloading the page disarms: press Arm again on purpose.
- [ ] Walking is open loop (by time): a clear floor between the start and the person; the remote in hand.

## During the show
- [ ] Operator arms with `/arm` only when the area is clear and they are watching.
- [ ] Visitors interact from the edge of the marked area. The robot walks ONLY in the show's `--walk` mode
      (camera-guided approach, below); everywhere else it never translates.
- [ ] Any unexpected behavior → physical stop first, then `/disarm`.
- [ ] After any failure, the guard disarms automatically; investigate before `/arm` again.

## Closing
- [ ] `/disarm`, `/close`, robot lowered (StandDown via remote), powered off.
- [ ] `data/runtime/` is never copied off the machine; memories stay until visitors `/forget`.

## Built-in software guards (do not replace the above)
Static hardware gates · operator arming · operator approval of body poses · Euler angles capped at ±0.3 rad · one motion at a time · cooldown · per-motion timeout →
software stop · battery / error / state-freshness checks · stop latch requiring re-arm · photo flow
never moves the real robot · approved-call allowlist in the Go2 adapter.
