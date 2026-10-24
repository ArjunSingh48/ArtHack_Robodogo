# Compatibility notes (verified 2026-10-24)

## Unitree SDK — `unitreerobotics/unitree_sdk2_python` (HEAD 2026-09-21, package version 1.0.1)

Verified **in source code**, not on hardware:

| Need | SDK API | Used? |
|---|---|---|
| Sit | `SportClient.Sit()` (api 1009) | yes |
| Rise from sit | `SportClient.RiseSit()` (1010) | yes |
| Stand up / lie down | `StandUp()`, `StandDown()` | yes |
| Hold balance | `BalanceStand()` | yes |
| Greeting | `Hello()` (1016) | yes |
| Stretch | `Stretch()` (1017) | yes |
| Software stop | `StopMove()` | yes |
| Walking | `Move(vx, vy, vyaw)` | **no** — no autonomous translation toward visitors |
| Flips, jumps, pounce, dance, free gaits | `FrontFlip`, `FrontJump`, … | **never** |
| Front camera | `go2.video.video_client.VideoClient.GetImageSample()` → JPEG bytes | yes (Mode C) |
| Battery | `rt/lowstate` → `LowState_.bms_state.soc` | yes |
| Error code / mode | sport state topic → `SportModeState_.error_code` | yes (topic configurable) |
| Speaker playback | none (VUI client: volume & brightness only) | external speaker instead |

Edition caveats: AIR / PRO / EDU differ in firmware and SDK access. The DDS Python SDK is in practice an
EDU feature over Ethernet. The sport-state topic name has varied across models/firmware
(`rt/sportmodestate`, `rt/lf/sportmodestate`) → `GO2_STATE_TOPIC` setting. Settle times in
`robot/base.py` are estimates to be measured on the real robot.

**Status of `UnitreeGo2Adapter`: HARDWARE-UNVERIFIED.** Run `itu robot-check` (no motion) first.

## Museum APIs

* **The Met**: `/public/collection/v1/search` was **retired 2026-10-01** and returns a message. We use
  `/public/collection/v1.1/search` with `offset` / `limit` (max 500). Requests **without a User-Agent
  get HTTP 403** (bot protection) — our client always sends one. Objects: `/v1/objects/{id}`,
  rights from `isPublicDomain`.
* **Cleveland Museum of Art**: `openaccess-api.clevelandart.org/api/artworks/` with `cc0=1`,
  `has_image=1`; per-record `share_license_status == "CC0"` is checked again on fetch.
* **Rijksmuseum**: not yet integrated (optional stage).

## Python packages (PyPI, 2026-10-24)

aiogram 3.31.0 · elevenlabs 2.74.1 (`client.text_to_speech.convert(...) -> Iterator[bytes]`,
`VoiceSettings(stability, similarity_boost, style, use_speaker_boost, speed)`) · mediapipe 1.1.1
(`mediapipe.tasks.python.vision.PoseLandmarker`, IMAGE mode, `num_poses`) · pydantic-settings 2.16 ·
qrcode 8.2. Tested on Python 3.13; requires ≥ 3.11.
