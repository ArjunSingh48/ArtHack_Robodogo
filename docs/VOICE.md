# Voice — one character, OpenAI only

ElevenLabs is gone from the project (no code, dependency or setting refers to it). Every spoken word
uses OpenAI. The robot's voice is **AI-generated** — the projector page says so, and so should a sign.

## Models (checked 2026-10-24 against the installed `openai` 3.37 SDK)

| Use | Setting | Default | API |
|---|---|---|---|
| Performance narration + short show lines | `OPENAI_NARRATION_MODEL` | `gpt-realtime-2.1-mini` | Realtime (`client.realtime.connect`) — listed in the SDK's model literals |
| Conversation windows in the show | `OPENAI_LIVE_MODEL` | `gpt-live-1` | GPT-Live (`client.live.connect` → `session.start`) |
| Free-play lines, Telegram voice notes, `itu talk`, conversation fallback | `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | Realtime |

No deprecated TTS endpoint is used. Whether *your* organisation has access to `gpt-live-1` can only be
learned with your key: the show tries it once and, if it is refused, switches to the Realtime fallback for
the rest of the run (logged as "GPT-Live … unavailable").

## One voice

`OPENAI_VOICE` is used for narration, lines, conversation and talk mode. It must exist in **both**
Realtime and GPT-Live: `alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar` (GPT-Live has
more voices, Realtime has exactly these). Default `marin`.

```bash
itu voice audition                         # the same sample, from the script, in all ten voices
itu voice audition --voices marin,cedar,coral,sage
```

Listen for: young in spirit but not childish, soft, a little hesitant; playful in the meme lines; intimate
at "Is this us?". Then set `OPENAI_VOICE=` and regenerate (`itu show voice pregenerate`).

## Scripted narration (A)

* Each cue in `data/performance/script.en.md` becomes one WAV clip, read by a Realtime session that is told
  to read the text exactly, with the chapter's `@voice:` direction (shy at the start, observational in the
  history chapters, confused in the internet chapter, warm at the end). Line breaks become short pauses,
  `…` longer ones. The model's own transcript is compared with the text; an improvised reading is
  discarded (one retry, then subtitle).
* Cache: `AUDIO_CACHE_DIR/narration/` (cues) and `AUDIO_CACHE_DIR/lines/` (short runtime lines). The key is
  a hash of text + voice + model + language + direction — change any of them and that clip is simply
  regenerated; old clips are removed with `prune`. `OPENAI_AUDIO_CACHE_ENABLED=false` uses a throw-away cache.

```bash
itu show voice status                      # which clips exist
itu show voice pregenerate                 # generate everything missing (≈ 170 clips the first time)
itu show voice regenerate paintings.07     # one cue again (ids from `status`; lines look like pose.got_it.1)
itu show voice prune                       # delete clips the script no longer uses
```

* During the show, cached clips play locally (ffplay / afplay / aplay / paplay) — **no internet needed**.
  A missing clip is generated on the fly with a 20 s limit, otherwise shown as a subtitle. The next clip is
  prepared in the background while the current one plays.

## Conversation windows (B) — GPT-Live, push-to-talk

Off by default: `OPENAI_ENABLE_LIVE_CONVERSATION=true` to enable (needs `pip install -e ".[talk]"`).

* Opens only in **guided** mode, at a hold after a chapter's narration ("You can ask me something, if you
  like."). The operator's `next` closes it and the story continues. The model never advances scenes.
* **Push-to-talk:** the microphone is muted and nothing is streamed until the operator presses `l`
  (`/listen`) while a visitor asks; `l` again to stop. Pressing `l` also silences the dog, so a visitor can
  interrupt it.
* The model gets the character (curious, shy, gentle, quietly funny; honest that it is a robot with an AI
  voice when asked), the dog's story, and the current chapter with **verified** artwork facts (title,
  artist, date, museum, description) and — when present — the structured pose observation from the camera
  pipeline. No images, no numbers.
* It has **no tools**: it cannot move the robot, take photos or send anything. Those stay with the
  StoryDirector and the operator, behind consent and the motion guard.
* Fallback: if GPT-Live cannot start, a Realtime session with turn detection off (push-to-talk:
  `input_audio_buffer.commit` + `response.create` on release). If that fails too, the show holds on the
  picture. Losing the internet never stops the performance.

Echo: use a headset mic for the visitor, or an echo-cancelling speakerphone; with push-to-talk the mic is
closed while the dog answers.

## Status

Verified offline: request payloads against the SDK's own schemas (`SessionStartEvent`,
`SessionUpdateEvent`), push-to-talk gating, fallback, cache keys, offline playback of cached clips.
**Not verified:** real audio quality, latency, GPT-Live access for your account — no API key was
available while building this.
