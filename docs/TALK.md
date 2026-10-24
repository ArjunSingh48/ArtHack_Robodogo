# Talk mode — a live conversation with the dog

In talk mode a visitor simply speaks to the dog. It listens, answers out loud in the visitor's language, tells
its story, and — when the visitor takes a pose — looks at them, remembers a painting or a meme, tries the dog's
pose with its body, and asks: *"Is this us?"*

It uses the **OpenAI Realtime API** (speech-to-speech) with `gpt-realtime-2.1` and the `marin` voice by default.

> In the performance (`itu show`), short conversation windows use GPT-Live with push-to-talk instead —
> see `docs/VOICE.md`. Talk mode below is the free-play version and uses the same primary voice (`OPENAI_VOICE`).

## Who the dog is

The character is defined in two places:

* **`data/persona/story.md`** — the dog's own story, written by the team. Edit it freely; it's plain text.
  The dog is told to tell its story **only** from this text and never to invent new facts about itself.
* **`src/is_this_us/talk/persona.py`** — how it speaks: shy and cute (soft, curious, a little clumsy, never babyish),
  short sentences, honest about being a machine, kind boundaries (no personal questions, gentle redirection).

The core idea: *the robodog is shy and cute. It wants to learn how real dogs behave, and the only way it can learn
is through cultural artifacts — paintings and memes.*

## What the dog can and cannot do

The voice model **cannot move the robot**. It has four tools:

| Tool | What it does |
|---|---|
| `look_and_remember(archive)` | Camera → pose → curated painting/meme match → curated behavior → **MotionGuard** → robot. Returns what was found and what the body did. |
| `recall_memories(archive)` | Lists the paintings and memes the dog knows (so it never invents titles). |
| `describe_memory(id)` | The facts about one painting or meme. |
| `stop_moving()` | Immediate software stop. The dog is told to use it whenever someone says stop or seems afraid. |

No tool has a parameter that chooses a movement. The model decides *when* to look; the curated annotations decide
*how* the body moves; the guard decides *whether* it may (operator arming, cooldown, battery, stop latch, approvals).

## Setup, step by step

1. **OpenAI key.** Get one at https://platform.openai.com/api-keys (billing must be enabled). In `.env`:
   ```
   OPENAI_API_KEY=sk-...
   OPENAI_REALTIME_MODEL=gpt-realtime-2.1
   OPENAI_VOICE=marin
   ```
2. **Install the talk extras** (microphone/speaker support):
   ```bash
   pip install -e ".[vision,talk]"
   ```
   macOS: the first run asks for microphone permission for your terminal — allow it
   (System Settings → Privacy & Security → Microphone). Linux: `sudo apt install libportaudio2`.
3. **Pick the microphone and speaker.**
   ```bash
   itu talk --list-devices
   ```
   Put the name or number in `.env` (`TALK_INPUT_DEVICE=…`, `TALK_OUTPUT_DEVICE=…`), or leave empty for the system default.
   Best: a USB conference speakerphone (with built-in echo cancellation) placed at the robot.
4. **Try it without the robot:**
   ```bash
   itu talk                       # webcam + mock robot
   itu talk --no-camera           # just talk
   itu talk --lang ru             # fallback language (the dog follows the visitor anyway)
   ```
   Press Enter to start a conversation. Say hello. Ask about its story. Then sit down with a cup in both
   hands and say "look at me" — it should find *This Is Fine*.
5. **With the real robot** (after `docs/HARDWARE_SETUP.md` and `docs/SAFETY_CHECKLIST.md`):
   ```bash
   itu talk --robot go2 --source unitree --i-have-physical-estop
   ```

## Echo and interruptions

With an open speaker the dog would hear itself and answer itself. By default talk mode is **half-duplex**:
the microphone is not sent while the dog speaks (plus a short tail), and it is also muted while the body is busy
with a pose. Visitors therefore wait for the dog to finish.

With a headset or a speakerphone that cancels echo, run `itu talk --full-duplex` (or `TALK_HALF_DUPLEX=false`):
visitors can interrupt and the dog stops mid-sentence.

## Conversation limits

* A conversation ends after `TALK_IDLE_SECONDS` (default 45) without speech — the dog says a short goodbye —
  or after `TALK_MAX_SESSION_SECONDS` (default 300).
* Ctrl+C sends a software stop to the robot and ends the conversation.
* Input noise reduction is set to `far_field` (the mic is not at the visitor's mouth).

## Privacy

* While a conversation runs, **microphone audio is streamed to OpenAI** so the dog can understand and answer.
  The installation does not record audio, does not transcribe visitors, and does not log what they say
  (only the dog's own lines and tool calls are logged).
* Talk mode keeps no database: each run uses an in-memory store that is discarded.
* **Put up a sign** at the installation, for example:
  > *This robot dog listens when you talk to it. Your voice is sent to OpenAI to understand and answer you.
  > Nothing is recorded here. The camera sees body posture only.*

## Costs

Realtime audio is billed per audio token, for both listening and speaking. A 5-minute conversation costs
noticeably more than reading short pre-written lines; check current OpenAI pricing and set a usage limit in the
OpenAI dashboard before an exhibition.

## Status

The conversation logic, tools and payloads are tested against a fake Realtime connection and validated against
the OpenAI SDK's own schemas. **Not yet tested with a real microphone, speaker and key** — try it once before a show.
