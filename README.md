# Is This Us? — A Dog's Memory

**MCP integration plan:** [Robot, backend, and Telegram implementation plan](docs/IMPLEMENTATION_PLAN.md)
and [Go2 MCP contract](docs/MCP_INTEGRATION.md). These describe the next implementation steps for
`zhaw-physical-ai/go2_main`, including pose similarity, robot capability gaps, and delivery behavior.
The setup guide below describes the existing direct SDK prototype; the proposed MCP adapter is not implemented yet.

[![tests](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

An interactive installation in which a Unitree robot dog tries to learn how to be a dog from
humanity's pictures of dogs. A visitor takes a posture; the robot remembers a scene with a similar
**human–dog relationship**, tries to become that dog with its own body, sends the picture to the
visitor's phone, and quietly asks: **"Is this us?"**

The robot has two cultural memories, treated with equal sincerity:

1. **Fine art** — 24 museum paintings of people and dogs (The Met, Cleveland Museum of Art; public domain / CC0).
2. **Internet culture** — famous dog memes: Doge, Cheems, This Is Fine, Side Eye Dog, Long Nose Borzoi.

> The machine does not learn to imitate commands. It learns to imitate relationships.

Python · MediaPipe · aiogram 3 · ElevenLabs · Unitree SDK2 · SQLite · Pillow

## Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | pipeline, state machine, matching, risks |
| [`docs/MEMES.md`](docs/MEMES.md) | meme memory: schema, rights policy, adding a meme, Imgflip discovery |
| [`docs/HARDWARE_SETUP.md`](docs/HARDWARE_SETUP.md) | first session with a Go2, step by step |
| [`docs/SAFETY_CHECKLIST.md`](docs/SAFETY_CHECKLIST.md) | **read before any motion near people** |
| [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) | verified SDK / API facts |
| [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) | how a visit unfolds in the gallery |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | common problems |

## What works today (no robot required)

- Pose recognition with MediaPipe + transparent geometric rules: standing, sitting, crouching, kneeling,
  leaning forward, reclining, lying; gestures (holding, reaching, looking down, turned toward/away); distance.
- Relationship matching across paintings, memes, both, or a surprise, on one comparable 0–1 scale.
- A closed, operator-controlled movement vocabulary with a safety guard; a mock robot that simulates the
  exact SDK call sequences; a Go2 adapter (hardware-unverified).
- The robot's character in English, Russian and German; ElevenLabs voice with caching and text fallback.
- A Telegram bot: QR pairing, `/observe`, photo mode without the robot, `/art` `/meme` `/surprise`,
  ♡ / ↻ / ✧ buttons, memory gallery, memory cards, consented imitation photos, `/forget`, operator controls.
- 129 tests, GitHub Actions CI.

## 1. Install

Python 3.11–3.14 and FFmpeg (for voice notes: `brew install ffmpeg` / `sudo apt install ffmpeg`).

```bash
git clone <this repo> is-this-us && cd is-this-us
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[vision,voice,dev]"
cp .env.example .env
itu setup          # downloads the pose model, checks artworks, creates the database
pytest             # everything should pass
```

## 2. First simulation (no Telegram, no robot)

```bash
itu simulate --posture sitting --another --card card.jpg          # SYNTHETIC skeleton, paintings + memes
itu simulate --posture sitting --gesture holding --archive meme   # → This Is Fine
itu simulate --posture kneeling --gesture extending --lang ru     # Russian
itu simulate --image me.jpg                                       # your own photo (Mode A)
itu simulate --webcam                                             # one webcam frame (Mode B)
itu webcam                                                        # live debug window (SPACE / Q)
```

The debug output shows detected posture and confidence, landmark confidence, the top three scenes with
score breakdowns, the chosen robot behavior, execution status, dialogue, delivery status and state trace.

## 3. Create the Telegram bot (BotFather)

1. In Telegram, open **@BotFather** → `/newbot`. Name: `Is This Us? — A Dog's Memory`; username e.g. `IsThisUsDogBot`.
2. Put the token in `.env`: `TELEGRAM_BOT_TOKEN=…` and `TELEGRAM_BOT_USERNAME=IsThisUsDogBot`.
3. Get your numeric Telegram ID (e.g. from **@userinfobot**) → `TELEGRAM_OPERATOR_IDS=123456789` (comma-separated).
4. Run `itu bot`. On your phone: open the bot → *Start* → send a photo of yourself sitting, standing, kneeling…
5. At the installation, an operator sends `/qr`; visitors scan it to pair, then use `/observe`.

**Visitor commands:** `/start` `/help` `/pair` `/observe` `/art` `/meme` `/surprise` `/memory` `/privacy` `/forget` `/stop`
**Operator commands:** `/arm` `/disarm` `/status` `/qr` `/open` `/close` `/approve <behavior>` `/revoke <behavior>` `/actions`

Never commit `.env` (already in `.gitignore`).

## 4. Voice (optional)

```
ELEVENLABS_API_KEY=…
ELEVENLABS_VOICE_ID=…              # choose a soft, quiet voice
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
LOCAL_AUDIO_PLAYBACK=true          # play on the laptop / a Bluetooth speaker
```

Only the sentence to be spoken is sent to ElevenLabs — never images or body data. Without a key, or when
the API fails, everything continues as text.

## 5. The real robot

```bash
pip install -e ../unitree_sdk2_python                          # official SDK, once (see HARDWARE_SETUP)
itu robot-check --save-frame test_frame.jpg                    # read-only, no motion
itu robot-test --sequence --i-have-physical-estop              # each movement on Enter
itu live --source unitree --no-motion                          # camera + matching, robot still
itu live --source unitree --i-have-physical-estop --archive both
```

The Go2 adapter uses only official high-level calls (StandUp, StandDown, BalanceStand, Sit, RiseSit, Hello,
Stretch, StopMove, and small bounded `Euler` body poses) — no walking, no tricks. It is
**HARDWARE-UNVERIFIED** until it has run on your unit; SDK access is expected on Go2 EDU.

The robot moves only when `APP_MODE=installation`, `ROBOT_MODEL=go2`, `ROBOT_MOTION_ENABLED=true` **and** an
operator has sent `/arm`. Photos sent to the bot never move the real robot. `/stop`, `s` and Ctrl+C are
software stops — **not** an emergency stop.

## 6. Curating

Paintings: `itu artworks search|draft|build|validate|cache|list` — museum metadata is fetched from the APIs,
never typed by hand. Memes: one JSON file per meme in `data/memes/annotations/`; `itu memes discover|validate|list`.
See [`docs/MEMES.md`](docs/MEMES.md).

## Repository layout

```
data/artworks/      curation_notes.yaml · annotations.yaml (generated) · images/ (public domain / CC0)
data/memes/         annotations/*.json (one per meme) · drafts/ (never loaded) · recreations/ (authorised images only)
docs/               architecture, memes, hardware, safety, compatibility, demo, troubleshooting
src/is_this_us/
  vision/           frame sources · MediaPipe pose · geometric interpreter · synthetic skeletons
  artworks/         museum clients (Met v1.1, Cleveland) · repository · curation
  memes/            schema · repository · curation (Imgflip discovery)
  scenes.py         SceneLibrary: one interface over both memories
  matching/         relationship matcher · MemeMatcher · SceneMatcher (art | meme | both | surprise)
  motion/           planner (scene → approved behavior) · guard (safety)
  robot/            step sequences · mock · Go2 adapter · shared DDS init
  dialogue/         engine + locales (en, ru, de)
  speech/           ElevenLabs, cache, FFmpeg, local speaker
  bot/              flows (network-free logic) · app (aiogram 3)
  orchestrator.py   state machine
  storage.py        SQLite: pairing, sessions, memories, archive choice, /forget
  cards.py          memory cards and diptychs
  cli.py, hardware_cli.py
tests/              129 tests
```

## Privacy

No face recognition or identification. Camera frames and visitor photos are processed in memory and not
stored. The database keeps only coarse labels (posture, scene id, behavior), which expire after
`SESSION_TTL_MINUTES`. Imitation photos are taken only after explicit consent, sent only to that visitor,
and never written to disk. `/forget` deletes everything immediately. No visitor ever sees another's moments.

## Licence

Code: MIT. Paintings: public domain / CC0 (see `data/artworks/README.md`). Memes: **not included** — they are
referenced and credited only, unless a licence or a team-made recreation is recorded (see `docs/MEMES.md`).
