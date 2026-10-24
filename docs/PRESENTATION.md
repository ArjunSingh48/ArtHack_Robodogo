# The presentation — `itu show` (default programme)

Robot dog + laptop + beamer. The laptop camera watches the person (pose and photo), the robot's own
camera finds where to go, the beamer shows the app, and the laptop plays the dog's voice.
The contract is in [UX.md → Presentation programme](UX.md#presentation-programme-user-decision-2026-10-24-afternoon).
The text is in `data/performance/presentation.en.md` and `data/performance/lines.en.yaml`; you can edit both freely.

```
Intro (shy dog, first picture, "may I take pictures of us?")
  → Prehistory   ┐  each round: narration → "take any pose" → steady pose →
  → Middle Ages  │  the robot picks the closest picture of this era → REMEMBERING (the era's other pictures
  → Renaissance  │  flash by while the robot walks over and takes the painted dog's pose) → the picture →
  → Internet     ┘  hold + photo (laptop camera) → THEN/NOW on the beamer → Telegram → robot walks back
  → Rescue finale: someone "faints" → the robot comes, offers a paw, "please wake up" → they get up →
    it sits beside them → last photo → "Am I a good boy already?" … "Is this us?"
```

## 0. The normal way: two terminals

```bash
# Terminal 1 — the bridge (go2_main). Checks en6 first; calm actions only; SDK timeout 10 s.
cd ~/Projects/go2-dog/go2_main && ./scripts/start_bridge.sh en6

# Terminal 2 — the presentation (this repo). Checks network, robot, bridge, camera; prints the two URLs.
cd ~/Projects/go2-dog/Is_this_us && source .venv/bin/activate
itu present                                               # projector + camera + voice, the robot does not move
itu present --motion --i-have-physical-estop              # + poses in place (sit, lie down, paw, stretch)
itu present --motion --walk --i-have-physical-estop       # + walking — only after calibration (section 5)
```
Telegram is off unless you add `--telegram`. Without it, photos are shown on the projector only and the dog
never mentions Telegram. Nothing has to be edited in `.env` for these runs. If the laptop camera fails,
`--fallback zhaw_bridge` reads poses with the robot's camera instead. Then there are no photos of the
person with the robot, because the robot cannot photograph itself, and the operator page says so.

**Two cameras, two jobs, both on the projector.** The dog's own camera (via the bridge, "WHAT THE DOG SEES")
finds the person, reads their pose, and guides the walk. The laptop camera ("THE PHOTO CAMERA") stands at the
side and takes the photo of the two of them. To read poses with the laptop camera instead, use
`--pose-camera laptop`. If the dog's camera has no frames, the program says so and falls back to the laptop.

**Poses and walking are separate.** `--motion` only lets the robot change pose where it stands.
`--walk` additionally lets it walk to the person with its own camera. A successful pose never turns
walking on.

**If something fails:**
- `interface en6 does not exist`: the USB-Ethernet adapter is unplugged or got another name (normal on
  macOS). The message lists the interface that has the 192.168.123.x address.
- `camera: … macOS did not allow the camera`: System Settings → Privacy & Security → Camera → allow your
  terminal app, then quit and reopen the terminal.
- `hello … code 3104 / outcome unknown`: the bridge is still on the old 3 s timeout. Restart it with
  `start_bridge.sh`, which sets 10 s.

## 0b. Without the robot: the simulated dog

```bash
itu present --sim-robot                 # real laptop camera + voice + projector; the dog is simulated
itu show --dry-run --sim-robot --display web --source synthetic:sitting   # fully simulated rehearsal
itu show --dry-run --sim-robot --display web --source photo:sitting      # …with a real photo instead of the drawn figure
```
The simulated room is drawn in 3-D (three.js, loaded from jsDelivr): the Unitree Go2 from its official
URDF, trotting and taking its poses, and a rigged avatar posed from the photo camera's pose estimate
(mirrored, facing the audience; joints out of frame come from the posture preset). Drag to rotate,
right-drag to pan, scroll to zoom, double-click to return to auto-framing. Offline, the flat map is drawn instead.

**Start screen.** `itu show` / `itu present` (without `--auto`) wait on the operator page before anything
plays: ▶ *they agreed to photos* (only now does the camera open) · ▶ *an uploaded picture* · ▶ *sample
photos* (`data/standins/`, Unsplash, one matched to each round). Later: 📷 *Switch to the webcam* /
🖼 *Switch to sample photos*. Keyboard: `y` / `sample`, then `camera` / `samples`. The photo question later in the intro is then not asked again.
A drawn top-down room appears in the camera column ("SIMULATED DOG"). It shows where the dog walks, where the
person is, and what the dog is doing (sitting down, giving a paw, walking…). The same pose and walking logic
runs as with the real robot, through the same safety guard, but with a mock robot: nothing real moves.

**While the dog "remembers"**, the era's pictures stay on screen together and its attention moves slowly
from one to the next (`SHOW_MONTAGE_FLASH_SECONDS`, default 2.8 s). **After a photo appears**, the dog
says something happy about it, and the photo stays until the operator presses **Next**.

## 1. Rehearse (no robot, no keys)

```bash
itu show --dry-run --auto --fast 0.05 --no-voice              # whole presentation, simulated person, terminal
itu show --dry-run --display web --source webcam              # with your webcam on the projector page
itu show --dry-run --display web --source webcam --walk       # + a simulated robot walking in a simulated room
itu show --dry-run --interactive …                            # skip the narration: consent, then straight to each round
itu show validate                                             # checks both programmes; lists each round's pictures
```
Rehearsal photos and the would-be Telegram messages go to `data/runtime/rehearsal/<session>/`
(`telegram_preview.txt`); nothing is sent.

## 2. Live with the robot

`.env`, in addition to the robot setup in [HARDWARE_SETUP.md](HARDWARE_SETUP.md) and the voice in the README:
```
SHOW_PROGRAMME=presentation
SHOW_DISPLAY=web
SHOW_CAMERA_SOURCE=webcam            # the LAPTOP camera: pose + photo
SHOW_CAMERA_MIRRORED=false           # true if your webcam already mirrors
SHOW_WALK_ENABLED=true               # only if the robot should walk
SHOW_WALK_CAMERA=zhaw_bridge         # the ROBOT camera: finds the person
SHOW_MOTION_ENABLED=true
SHOW_TELEGRAM_CHAT_ID=-100…          # the one private chat/group the pictures go to
SHOW_ROUND_HOLD_SECONDS=6            # how long the robot holds each pose after settling
SHOW_RESCUE_WAIT_SECONDS=60
SHOW_MONTAGE_FLASH_SECONDS=2.8
```
```bash
itu show --display web --motion --i-have-physical-estop --walk --telegram
```
Open the **projector page** (printed URL) full-screen on the beamer. Open the **operator page**
(the URL with `?key=…`) on a second screen or phone. It has the buttons; the beamer page has none.
Keep the robot's remote in your hand the whole time.

Without walking (the robot stays where you put it and only changes pose): leave out `--walk`.
Without any motion: leave out `--motion`. The presentation still runs, and the operator page says
"not performed".

## 3. Operator buttons (operator page / Telegram / keyboard)

| When | Buttons |
| --- | --- |
| Intro: "may I take pictures of us?" | **✓ They agreed** / **✗ No photos**, then separately **✓ Send to Telegram** / **✗ Don't send** (big screen only) |
| Round: waiting for a pose | starts by itself after ~3 s of a steady pose; **✓ Ready / start now**, **✗ Declines** (skips the round) |
| Holding the pose | **End the pose now** |
| Rescue | **✓ Yes, now** if the camera doesn't notice the person lying down (and again when they are up); **Skip** |
| Always | Pause · Resume · Next · ✦ Go to interactive (skips the rest of this chapter's narration) · End |

**End** (or `/cancel`, or `q`) stops motion through the guard and deletes this session's photos.

## 4. How the picture is chosen

Each era's pictures are in `data/performance/catalog.yaml` (`chapter:`). A round only uses open-access
artworks with a local image (`itu show catalog build` fetches them with verified rights). For each
picture, the curators note the person's posture (`human: sitting | standing | kneeling | lying`), what
the robot does to echo the dog (`dog: sit | lie_down | stand | greet | stretch`), and where the dog is
(`dog_side: left | right | front`). The robot picks the picture whose person poses most like the
participant (same posture, then a close one, plus an arm reaching out or a tilted head). It doesn't
repeat a picture in the same presentation. `itu show validate` prints this per round. The operator page
shows why each pick was made; the audience never sees a score.

## 4b. Calibrating the walk (once per room/camera)

```bash
itu show walk-test --calibrate 2 --posture sitting --i-have-physical-estop    # a person sits 2 m in front
itu show walk-test --calibrate 2 --posture standing --i-have-physical-estop   # a person stands 2 m in front
```
Put the printed `SHOW_WALK_K_SITTING=…` and `SHOW_WALK_K_STANDING=…` into `.env`, add
`SHOW_WALK_CALIBRATED=true`, and try one walk on its own: `itu show walk-test --side left --i-have-physical-estop`.

While walking, the program sends the current velocity about every 0.1 s. A velocity older than 0.6 s is
replaced by zero, and the bridge itself zeroes anything older than 250 ms. Every end of a walk (arrival,
person lost, camera lost for 0.6 s, disarm, End, error) sends an explicit stop. Speed stays at most
0.22 m/s, and the adapter caps it at 0.3 m/s.

## 5. Safety in the rescue

The robot approaches someone lying on the floor more carefully. It judges the distance by the
person's width, not their height, and stops in front of them at `lying_gap_m` = 1.2 m instead of
0.55 m. All the walking limits still apply (speed, distance, time, the stop latch, the bridge's
obstacle avoidance and heartbeat). It never walks over the person. Rehearse the rescue with the
person lying across the robot's view, not along it. Keep the remote in hand.

## 6. What is verified

| Level | Status |
| --- | --- |
| Software sequence (simulation, tests) | `tests/test_presentation.py`: full presentation with/without consent and send confirmation, no camera, cancel, rounds-only, simulated walking there and back, keeping distance from a lying person, meme drawing |
| Telegram live delivery | not verified here (needs your bot token and chat) |
| Voice | English-only ElevenLabs; live API and offline cache checked. Listening approval and full audio rehearsal remain; see [VOICE.md](VOICE.md). |
| Robot poses, walking, rescue approach | **not verified on the robot**; rehearse with the remote in hand |
| Lying-person detection with the laptop camera | MediaPipe posture classifier; not verified in the room. The operator's **✓ Yes, now** is the fallback |
