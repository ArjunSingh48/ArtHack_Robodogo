# Is This Us?

A tiny robot dog looks for us in 5,000 years of pictures of humans and dogs. Take any pose — it finds the
closest picture of that era, walks over and takes the painted dog's pose next to you, and makes a THEN / NOW
picture of the two of you.

**Live: https://isthisus.vercel.app**

Everything runs in the browser (a static site in `web/`):

- the pose is read on the visitor's device (MediaPipe Pose Landmarker); the camera picture never leaves it
- the dog is a simulated Unitree Go2 (official URDF, three.js) next to an avatar posed from the camera — or the
  real one, with `?robot=` ([below](#the-real-robot))
- the voice is pre-generated (`web/audio/`); no API keys online
- start with your camera or with sample photos (Unsplash); every step has a hash (`#paintings.04`) to come back to it

```bash
cd web && python3 -m http.server 8790      # local: http://127.0.0.1:8790/
cd web && vercel deploy --prod             # deploy
```

## The real robot

The browser show can also drive a real Unitree Go2. The browser cannot talk to the robot directly, so two small
programs run on the laptop that is wired to it:

```
isthisus.vercel.app in Chrome ──WebSocket ws://127.0.0.1:8770──► itu web-link (python/)
                                                                     │ ZeroMQ :5555
                                        go2_bridge (ZHAW go2_main) ◄─┘ ──DDS / Ethernet──► Go2
```

`itu web-link` passes everything through the same safety code as the Python show: calm poses only (sit, lie
down, paw, stretch, stand), one at a time and confirmed, walking slow and streamed at 10 Hz, and every walk ended
with Move(0,0,0) → stop → stop_move so the dog leaves its walking gait. Without `?robot=` the site stays
simulation-only.

### Running it on show day

**Before the first motion, read [`python/docs/SAFETY_CHECKLIST.md`](python/docs/SAFETY_CHECKLIST.md).**

1. **Connect the laptop to the Go2 by Ethernet** (an address in `192.168.123.x`), then start the bridge
   **with this repository's launcher**. It runs go2_main unchanged, but with a 10 s SDK timeout. go2_main's own
   3 s makes `hello` come back as code 3104, and the robot then refuses to arm.
   ```bash
   python/scripts/start_bridge.sh en6        # en6 = the Ethernet interface wired to the Go2 (it checks)
   ```
   This needs a [go2_main](https://github.com/zhaw-physical-ai/go2_main) checkout set up by its INSTALL.md
   (default `~/Projects/go2-dog/go2_main`; otherwise set `GO2_MAIN=/path/to/go2_main`).
2. **Start the relay** in a second terminal:
   ```bash
   cd python && .venv/bin/itu web-link --i-have-physical-estop
   ```
   It prints `ARMED` when the bridge answers with the 10 s timeout. Otherwise it prints why the robot is not armed.
3. **Open https://isthisus.vercel.app/?robot=ws://127.0.0.1:8770 in Chrome** and allow the "local network
   access" prompt. The badge at the bottom left must say **REAL GO2 · armed**.
4. **Keep the Unitree remote in your hand.** The bridge does not report the battery (watch it in the Unitree app),
   and the remote is the real stop.

During the show:

- **Esc** stops the dog (software stop) and pauses the show. The badge then says why it stopped. Check the dog and
  press **Arm (dog checked)** on the badge, then Resume.
- If a pose fails or its outcome is unknown, the robot disarms and the badge says which pose and why. Nothing more
  is sent until you press Arm.
- Closing or reloading the page ends any walk and disarms ("the page disconnected"). Press Arm again after reloading.
- Ctrl+C in the `itu web-link` terminal stops the dog (Move 0 → stop → stop_move). Ctrl+C in the bridge terminal
  does the same on the robot itself.

### Rehearsing without the robot

```bash
cd python && .venv/bin/itu web-link --mock                # nothing is sent anywhere; the badge says (mock)
cd web && python3 -m http.server 8790                     # or the live site
# open http://127.0.0.1:8790/?robot=ws://127.0.0.1:8770
```

Walking is open loop: the real dog walks by time at the drawn dog's speed (0.25 m/s, 0.5 rad/s). Over the 1–2 m
of a round it ends up roughly where the drawn dog does. The Python show (`itu present --motion --walk`, below)
instead finds the person with the dog's own camera.

## The Python application

[`python/`](python/README.md) is the real-robot application: the presentation with the robot's camera
(`itu present`), the Telegram bot, the relay above, and the tests (GitHub Actions, mock robot only — never the
real Go2). Setup:

```bash
cd python && python3 -m venv .venv && .venv/bin/pip install -e ".[vision,voice,bridge,dev]" && .venv/bin/pytest
```

`web/data/show.json` (script, pictures with each painted dog's pose/side/facing, lines, sample poses) and
`web/audio/` are exported from it. To change the content, edit it there and re-export:

```bash
cd python && .venv/bin/python scripts/export_web.py
```

Picture credits: museum pictures are public domain / open access (labels on screen); sample photos and the
meme stand-ins are from Unsplash (Unsplash License) — see `python/data/standins/README.md`.
