# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Pose model not found` | `itu setup` downloads `models/pose_landmarker_lite.task`. |
| Posture always `unknown` | Legs or hips out of frame. Step back; raise/tilt the camera. Check with `itu webcam`. |
| Wrong posture | Tune thresholds in `vision/interpreter.py::posture_scores`; watch values in `itu webcam`. |
| Webcam won't open (macOS) | System Settings → Privacy → Camera → allow your terminal app. |
| Bot silent | Wrong `TELEGRAM_BOT_TOKEN`, or another process polling the same bot. Only one `itu bot`. |
| `/observe` says "resting" | Operator sent `/close`, or `CAMERA_SOURCE` failed to open. |
| `/arm` refused "gates closed" | Need `APP_MODE=installation`, `ROBOT_MODEL=go2`, `ROBOT_MOTION_ENABLED=true`. |
| Robot never moves after `/stop` | By design: re-arm with `/arm`. |
| "cooldown" in logs | `MOTION_COOLDOWN_SECONDS` between motions (default 8 s). |
| No voice | `OPENAI_API_KEY` missing or invalid (log: "OpenAI rejected credentials"); installation continues text-only. |
| "voice discarded … model said …" in logs | The Realtime model improvised instead of reading; the moment continues as text. Try another `OPENAI_REALTIME_VOICE`/model if frequent. |
| Voice file but no voice note | `ffmpeg` missing — install it (`brew install ffmpeg` / `apt install ffmpeg`). |
| Met API 403 | Missing User-Agent (our client sends one); or Met bot protection — retry later. |
| Met `/v1/search` message | Retired 2026-10-01; this project uses `/v1.1/search`. |
| `unitree_sdk2py` import error | Install from GitHub (HARDWARE_SETUP.md). Not needed for simulation. |
| Painting images slow | `itu artworks cache` stores them locally; bot then sends local files. |
| Meme never chosen | Visitor archive is `art` (`/meme` or `/both`), or `itu memes validate` reports a problem. |
| Meme shown without picture | By design: reference-only memes are linked and credited, not reproduced (docs/MEMES.md). |
| Robot stands instead of side-eye / peer | `look_aside` / `peer` not approved yet: test, then `/approve`. |
| No 📷 button | `IMITATION_CAMERA_SOURCE` empty, or the moment came from a photo (robot not present). |
| `itu memes discover` fails | Imgflip API unreachable or changed; add memes by hand (JSON). |
