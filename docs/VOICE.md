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

## Voice character experiments — "Hertz"-style robot dog (`VOICE_PRESET`)

Direction: a small physical robot dog with an unexpectedly **low**, distinctly synthetic, slightly nasal,
buzzy, deadpan voice (inspired by "Hertz", the talking delivery robot dog of the retailer re). Low-mid pitch —
not a natural adult speaker, not booming bass, not a high cartoon voice. Cute through contrast: small body,
serious machine voice, awkward pauses. The narration text is **not** rewritten.

**Status: experiments only. The default is `VOICE_PRESET=off` = the current show voice, unchanged.** Pick a
preset after listening (see "Choosing" below).

**How it works (same Azure OpenAI Realtime path, nothing migrated):**

1. **Base voice** — one of the voices that exist in both Realtime and GPT-Live (alloy, ash, ballad, coral,
   echo, sage, shimmer, verse, marin, cedar — there is no `onyx`/`fable`/`nova` in Realtime). Presets use the
   lower ones (ash, echo, verse); `VOICE_BASE_VOICE` overrides (e.g. `cedar`).
2. **Character instructions** — the Realtime endpoint has no TTS `instructions` field, no SSML and no
   pitch/rate parameters; the only style control is the session `instructions`. The preset's delivery text
   (deadpan, mechanical articulation, awkward pauses, abrupt intonation changes) goes there with the reader
   rules (read exactly, no added sounds) and the scene's emotion. The transcript fidelity check is unchanged.
3. **Local post-processing** (`speech/fx.py`, FFmpeg + numpy): time-preserving pitch shift (down), separate
   formant control, rate, EQ (high/low-pass band-limiting, nasal peak ~1.3–1.7 kHz, presence, air), channel
   vocoder buzz (speech envelope on a fixed ~100 Hz saw+noise carrier, blended with the dry voice), saw or
   sine ring-mod blend, short comb resonance ("hollow body" / "tin can"), loudness normalisation
   (speech-active RMS −19 dBFS, soft ceiling −1.5 dBFS — never clips). No bitcrush, no distortion.
4. **Sound cues** (`speech/cues.py`) — optional procedural beeps (`startup`, `boop`, `chirp`, `puzzled`,
   `chime`), low and square-ish for these presets; called explicitly by the show, never between sentences.

| Preset | Base | Pitch | Rate | Character of the processing | Delivery instructions |
|---|---|---|---|---|---|
| `hertz_deadpan` (recommended first try) | ash | −3 st (formant −0.5) | 0.97 | light vocoder buzz (18 %), whisper of saw ring-mod, nasal +4 dB @1.5 kHz, hollow comb ~5 ms | flat, deadpan, mechanical, awkward pauses, occasional abrupt intonation |
| `hertz_buzzy` | echo | −2 st | 0.98 | strong vocoder (55 %, 98 Hz carrier → monotone-ish), nasal +3 dB @1.3 kHz | low, electronic, almost monotone |
| `hertz_tin_can` | verse | −1.5 st | 1.00 | band-limited 250 Hz–4 kHz, nasal +5 dB @1.7 kHz, resonant tin-can comb (~2.6 ms), light vocoder | dry, nasal, through a little built-in speaker |
| `off` (default) | `OPENAI_VOICE` | — | — | none | the current voice exactly (old instructions, same cache keys) |

These are **starting values**. Previews so far were made only with local stand-in voices (eSpeak NG, Piper);
nobody has heard the Azure voice through this processing yet.

### Previews: `itu voice-demo`

```bash
itu voice-demo                                          # Azure (needs the key): all presets, same sample
itu voice-demo --engine azure --preset hertz_deadpan --play
itu voice-demo --engine espeak                          # no key: eSpeak NG as the base voice
itu voice-demo --engine piper --piper-model ~/voices/en_US-ryan-medium.onnx   # no key: neural local base
itu voice-demo --offline-input my_recording.wav         # no key: any recording through every preset
itu voice-demo --direction scene:internet               # with a scene's emotion (Azure only)
```

Default sample, one clip per line, joined with fixed silences so timing is fair across experiments
(`[pause]` 0.7 s, `[long pause]` 1.8 s, `[pause 1.2]`; `--text` takes the same syntax):

    Hello. I am a dog. [pause] I have four legs. [pause] I do not have a tail. [long pause] This is concerning.

Output in `voice_previews/` (git-ignored), per engine E: `base__E.wav` (unprocessed, loudness-matched; for
Azure it is the **current** show voice), `<preset>__azure.raw.wav` (preset voice + instructions, unprocessed),
`<preset>__E.wav` (processed), `hertz_deadpan__pitch-2/-3/-4__E.wav`, `cues/<preset>/*.wav`, and `.mp3`
copies (`--formats mp3,ogg`) for phones. Duration / peak / RMS are printed. Azure raw takes are cached in
`AUDIO_CACHE_DIR/voice_demo/`, so re-running after FX changes costs no API call.

Local engines: `espeak-ng` (`brew install espeak-ng` / `apt install espeak-ng`; `--espeak-voice en-us+m3`,
`--espeak-pitch 50`, `--espeak-speed 140`), Piper (`pip install piper-tts`, then download a voice `.onnx` +
`.onnx.json`, e.g. `en_US-ryan-medium` from huggingface.co/rhasspy/piper-voices — keep it outside the repo).
They only audition the *processing*; the instructions only affect the Azure model.

### Choosing

```bash
VOICE_PRESET=hertz_deadpan          # in .env
itu show voice pregenerate          # new takes for the new voice/instructions (old clips: `itu show voice prune`)
```

| Setting | Default | |
|---|---|---|
| `VOICE_PRESET` | `off` | `hertz_deadpan`, `hertz_buzzy`, `hertz_tin_can` |
| `VOICE_BASE_VOICE` | — | override the preset's base voice (narration AND live conversation) |
| `VOICE_FX_ENABLED` | `true` | `false` = raw model audio (instructions still apply) |
| `VOICE_PITCH_SEMITONES` / `VOICE_FORMANT_SEMITONES` / `VOICE_RATE` | — | override pitch / formant / rate |
| `VOICE_COLORATION` | — | multiplies vocoder / ring-mod / comb / chorus amounts (0 = none, 1 = preset) |
| `VOICE_CHIRPS` | `true` | occasional chirp before a clip (the Hertz presets set it to 0) |
| `VOICE_CUES` / `VOICE_CUE_VOLUME` | `true` / — | procedural sound cues and their volume (0..1) |

While a preset is active it chooses the base voice; `OPENAI_VOICE` applies only with `VOICE_PRESET=off`.

**Scenes.** Each chapter's `@voice:` direction is combined with the preset ("only the emotion changes with the
scene — the voice, its size and its character stay exactly the same"). `@voice: scene:<id>` expands to a
built-in emotion (`speech/character.py: SCENES`): `prehistory` fascinated/curious/slightly confused,
`middle_ages` very serious, trying hard to sound intelligent, `paintings`/`portraits` gentle curiosity,
`photography` thoughtful, slightly melancholic, `internet` suddenly very excited, abrupt intonation, comic
timing, `invitation`/`human_interaction` warm, encouraging, awkwardly shy, `present`/`ending` quiet, gentle,
intimate — still the same low little robot. Free-text directions keep working.

**Caching.** Raw model audio: `<cache>/<key>.wav`, key = text + voice + model + language + full instructions
(character + scene) + preset name/fingerprint. Processed audio: `<cache>/fx/<key>.<fx-hash>.wav`. Changing the
preset, its text or the voice → new takes; changing only FX → local re-processing of cached raw takes (no API
call, ~0.1–0.5 s per clip). `itu show voice prune` also removes processed clips of old FX settings. A
processing failure plays the raw clip (warning logged once); a synthesis failure shows the subtitle.

**Live conversation (GPT-Live / Realtime fallback, `itu talk`).** Same character text in the session
instructions, same base voice — but the live audio is **not** processed: it is streamed and played as it
arrives, and a streaming pitch-shifter + vocoder would add latency and a new failure mode in front of
visitors. Live answers therefore sound like `<preset>__azure.raw.wav` (no pitch-down, no buzz, no nasal/tin
colour). With `hertz_buzzy` and `hertz_tin_can` the contrast will be obvious; with `hertz_deadpan` it is
smallest. If it matters, keep conversation windows off or set `VOICE_FX_ENABLED=false`.

**Requirements.** FFmpeg on PATH; pitch uses FFmpeg's `rubberband` filter when available (check
`ffmpeg -hide_banner -filters | grep rubberband`), otherwise `asetrate+atempo` (timing kept, formants follow
pitch; logged once). macOS: `brew install ffmpeg` (Homebrew's build normally includes rubberband — verify).
MP3 copies need libmp3lame (included in Homebrew's ffmpeg). No new Python dependencies for the show (numpy);
`piper-tts` is optional and only for local previews.

## One voice

`OPENAI_VOICE` is used for narration, lines, conversation and talk mode when `VOICE_PRESET=off`
(otherwise the preset's base voice, see above). It must exist in **both**
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
