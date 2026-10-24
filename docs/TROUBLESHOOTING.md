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
| No voice | ElevenLabs key/voice missing or invalid (log says so); installation continues text-only. |
| Voice file but no voice note | `ffmpeg` missing — install it (`brew install ffmpeg` / `apt install ffmpeg`). |
| Met API 403 | Missing User-Agent (our client sends one); or Met bot protection — retry later. |
| Met `/v1/search` message | Retired 2026-10-01; this project uses `/v1.1/search`. |
| `unitree_sdk2py` import error | Install from GitHub (HARDWARE_SETUP.md). Not needed for simulation. |
| Painting images slow | `itu artworks cache` stores them locally; bot then sends local files. |
