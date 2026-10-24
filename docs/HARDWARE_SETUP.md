# Hardware setup — first session with a Unitree Go2

Status of the Go2 integration: **HARDWARE-UNVERIFIED** until these steps pass on your unit.
Follow `SAFETY_CHECKLIST.md` throughout.

## 0. What you need
* Go2 (SDK access is expected on **EDU**; AIR/PRO may refuse SDK commands), charged battery.
* Laptop with Ethernet (or USB-Ethernet adapter) + cable to the robot.
* The Unitree remote in the operator's hand. A clear, marked area ≥ 1.5 m around the robot.

## 1. Install the project and the SDK (on the laptop, once)
```bash
git clone <your repo url> is-this-us && cd is-this-us
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[vision,voice,dev]"
itu setup

git clone https://github.com/unitreerobotics/unitree_sdk2_python.git ../unitree_sdk2_python
pip install -e ../unitree_sdk2_python          # pins cyclonedds==0.10.2
```
If CycloneDDS fails to build, install CycloneDDS 0.10.x from source and set `CYCLONEDDS_HOME`
(see the SDK README). On Apple Silicon this is the most common hurdle.

## 2. Network
Give the laptop's Ethernet interface a static IP in the robot's subnet (Unitree's default is usually
`192.168.123.x`, e.g. `192.168.123.222/24` — check your unit's docs). Find the interface name with
`ip a` (Linux) or `ifconfig` (macOS), e.g. `enp3s0`, `en7`, and set in `.env`:
```
ROBOT_NETWORK_INTERFACE=enp3s0
```

## 3. Read-only check — no motion, safe with people around
```bash
itu robot-check --save-frame test_frame.jpg
```
Every line should be ✓: SDK, interface, DDS, live state topic, battery, error code 0, motion
switcher mode, front camera. The check prints the topic that is actually live — copy it into
`GO2_STATE_TOPIC` if it differs. Look at `test_frame.jpg` for camera framing, then delete it.
The check never changes the robot's mode; if no high-level service is active, switch the robot to
its normal/sport mode in the Unitree app.

## 4. Supervised motion test (area clear, remote in hand)
```bash
itu robot-test --sequence --i-have-physical-estop
```
Runs stand → sit → stand → lie_down → stand → greet → stretch → stand → look_aside → peer, waiting for
Enter before each step (`q` to quit, Ctrl+C = software stop). It prints a results table with real
durations — use it to tune the settle times in `src/is_this_us/robot/base.py`.

For `look_aside` and `peer`, note which way the body moved: the `Euler` sign conventions are unverified.
`peer` should lower the nose; if it raises it, flip `PEER_PITCH` in `robot/base.py`. Only then approve
them for the show in Telegram (`/approve look_aside`, `/approve peer`). A single behavior:
`itu robot-test sit --i-have-physical-estop`.

## 5. Camera + matching only (robot stays still)
```bash
itu live --source unitree --no-motion       # or --source webcam
```
Press Enter while someone poses at the edge of the area: you see posture, the chosen painting, the
behavior the robot *would* do. Tune posture thresholds here before letting it move.

## 6. Full live experience in the terminal
```bash
itu live --source unitree --i-have-physical-estop --lang ru
```
Enter → observe → painting → real motion → voice (laptop/Bluetooth speaker; the Go2 Python SDK has no
playback API). `s` = software stop (ends motion for the session), `q` = quit.

## 7. With Telegram
`.env`: `APP_MODE=installation`, `ROBOT_MODEL=go2`, `ROBOT_MOTION_ENABLED=true`,
`CAMERA_SOURCE=unitree` (or `webcam`), then `itu bot` and `/arm` from an operator account.
