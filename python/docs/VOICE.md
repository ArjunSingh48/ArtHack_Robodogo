# Voice — ElevenLabs English speech and legacy Azure experiments

**Decision, 2026-10-24:** replace Azure OpenAI as the dog's speech provider with
ElevenLabs. **Status: implemented for scripted speech; live API and offline cache
checks passed.** English-only presentation speech uses the selected ElevenLabs
voice. The Azure reference below applies only when explicitly selected and to
the separate live conversation experiments.

## Recommended solution

Use ElevenLabs text-to-speech for the approved presentation script and short
runtime lines, **in English only** (user clarification). Generate clips before
the show, cache them locally as WAV, and reuse the existing laptop playback,
pauses, subtitles, and stop handling.
This changes the voice heard during the intro, era rounds, and rescue finale;
it introduces no visitor choice. The script, consent steps, Telegram captions,
and guarded motion remain governed by [UX.md](UX.md).

Start with **`eleven_multilingual_v2`**, using one selected voice ID for narration
and reactions. The reason for this baseline is stable delivery, not multilingual
coverage; the model's name does not require multilingual use. Compare an
expressive `eleven_v4` take during audition if needed; Flash v2.5 is a later
option for latency-sensitive lines. These are model
choices, separate from the voice's identity. The recommendation prioritizes a
repeatable, rehearsed performance; it is not a claim that this voice has been
heard or validated. [Official model comparison](https://elevenlabs.io/docs/overview/models).

```text
Approved spoken text → ElevenLabs TTS → local WAV cache → existing laptop speaker
                                            ↓
                                  optional existing local FX
```

Only spoken text and synthesis settings go to ElevenLabs. No visitor photo,
camera frame, pose landmarks, or microphone recording is needed for this path.
An unavailable voice continues silently with the existing presentation subtitles;
it must not add explanatory Telegram messages or delay safety controls.

## Setup for the operator

1. Sign in to ElevenLabs. Open [API keys](https://elevenlabs.io/app/api/api-keys)
   and create a project key with Text to Speech access. Allow voice/model read
   access if using API discovery; set a small credit quota for the first trials.
   Save the key only in the local, git-ignored `.env`, never in chat or the
   projector frontend. [Authentication and key restrictions](https://elevenlabs.io/docs/api-reference/authentication).
2. Audition voices in the [Voice Library](https://elevenlabs.io/app/voice-library)
   using English lines: “Hello. I am a dog.”, “Please wake up”, and “Am I a good boy
   already?”. Add the chosen voice to your account and copy its voice ID. The
   [official quickstart](https://elevenlabs.io/docs/eleven-api/quickstart) shows how
   the key and voice ID are used; no Azure deployment is needed for this TTS path.
3. If stock voices miss the character, use Voice Design, audition its previews,
   and save the chosen preview to the library to obtain a reusable voice ID.
   [Voice Design workflow](https://elevenlabs.io/docs/eleven-api/guides/how-to/voices/voice-design).
   Suggested design prompt, retaining the existing robot-voice experiment:

   > A small robot dog with a low-mid, slightly nasal, gently buzzy voice.
   > Dry, deadpan articulation, awkward pauses, shy curiosity and quiet warmth.
   > Clear and intelligible, with subtle mechanical character. No booming bass
   > or high cartoon squeak.

4. Try a few short clips before generating the whole programme. Start with the
   account's available allowance; compare the chosen model's character cost in
   [API pricing](https://elevenlabs.io/pricing/api). Budget for alternate takes.
   Playback of a locally cached clip makes no synthesis request.

**Working `.env` configuration:**

```dotenv
VOICE_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=<local secret>
ELEVENLABS_VOICE_ID=<chosen voice ID>
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
ELEVENLABS_OUTPUT_FORMAT=pcm_24000
ELEVENLABS_STABILITY=0.5
ELEVENLABS_SIMILARITY_BOOST=0.75
ELEVENLABS_STYLE=0.0
VOICE_CACHE_ENABLED=true
LOCAL_AUDIO_PLAYBACK=true
VOICE_PRESET=off
OPENAI_ENABLE_LIVE_CONVERSATION=false
```

The numeric voice settings are audition starting points, not an approved sound.
Begin with raw audio; compare existing local Hertz FX separately. Voice selection
must use `ELEVENLABS_VOICE_ID`, even if a local FX preset is enabled later.
[ElevenLabs voice-settings reference](https://github.com/elevenlabs/skills/blob/main/text-to-speech/references/voice-settings.md).

## Implemented behavior and commands

The REST adapter uses the existing `httpx` dependency, receives `pcm_24000`, and
writes validated, complete WAV files atomically. No additional Python SDK is
needed for ElevenLabs. `VOICE_PROVIDER=elevenlabs` takes precedence over the old
`OPENAI_VOICE_PROVIDER`; without the new setting, an explicitly configured old
provider still works. Otherwise the default is ElevenLabs. `VOICE_CACHE_ENABLED`
similarly overrides `OPENAI_AUDIO_CACHE_ENABLED`.

```bash
itu show voice status                     # provider, missing clips and character count
itu voice audition --no-play --text "Hello. I am a dog."
itu voice audition                        # listen to the configured voice
itu show voice pregenerate                # generate missing clips (uses account credits)
itu show voice regenerate rescue.01       # use an actual cue ID listed by status
itu show voice prune                      # selected provider's unused clips only
itu show --dry-run --display web           # mock rehearsal; cached speech can play
```

`status`, `pregenerate`, and `prune` cover both the presentation and historical
show plus fixed runtime lines. Audition accepts `--voices ID1,ID2` for ElevenLabs.
Use `itu voice-demo --engine elevenlabs` to compare existing local Hertz FX on the
same selected voice; `--engine azure`, `espeak`, `piper`, and `--offline-input`
remain explicit alternatives. Automated previews do not approve a final sound.

For ElevenLabs, show loading and CLI tools select English source files even when
another `--lang` was requested. The adapter rejects non-English language codes;
it does not translate text. Legacy locale templates remain available to other
modes. Captions remain separate from the speech cache.

Cache files live in `AUDIO_CACHE_DIR/elevenlabs/narration/`, `lines/`, and
`dialogue/`. Keys include provider, text, language, voice ID, model, audio format,
and effective voice settings. FX use a separate fingerprint and reuse raw takes.
A missing API key still permits cache playback. `VOICE_PROVIDER=none` and
`--no-voice` disable speech playback and character cues. Disabling persistent
caching uses a temporary cache for that synthesizer's lifetime; it can reuse clips
within the session and cleans up when released. Keep persistent caching enabled
for show preparation.

The adapter maps approved script directions and `scene:` IDs to small stability
adjustments. These are **audition starting points, not verified emotional delivery**.
Unknown free-text directions warn the operator and use base settings; directions
never become spoken words. Script pauses remain in the director. Existing Hertz
presets affect local audio processing without replacing the ElevenLabs voice ID.
Model-specific controls differ: v4 omits the v2 `style` field. The supported and
live-tested baseline is Multilingual v2.

Network generation has an overall deadline (`ELEVENLABS_TIMEOUT_SECONDS`, default
18 seconds), one bounded retry for transient errors, and no retries for rejected
credentials, permissions, or exhausted quota. The show's preparation wait is also
bounded. Stop/skip cancels playback and prevents late worker results from playing;
a cancelled worker can still finish caching its result. No failure can trigger
robot movement or explanatory Telegram messages. Errors avoid logging secrets or
response bodies.

ElevenLabs returns audio, not the old Azure self-transcript. This provider does
**not** claim a transcript fidelity check. Listen to generated clips to check the
words, tone, pronunciation, and intelligibility before the presentation.

## Verification and remaining rehearsal

A live request with the configured voice produced a valid 24 kHz mono WAV for
“Hello. I am a dog. Am I a good boy already?” (2.6 seconds). The same clip passed
cache-only show integration with credentials removed and generation disabled.
This establishes API access and cache integration, not a listening judgment.
Mock HTTP and runtime tests cover provider selection, English source loading,
cache invalidation, concurrent requests, invalid/partial audio, FX reuse, request
failures/deadlines, and no late playback after stop/skip. Run the relevant suites
with `.venv/bin/python -m pytest`.

The full regression run passed 380 tests with two skips; four legacy Azure
schema/tool tests could not run because the optional `openai` package is absent.
Localhost projector tests passed with sandbox permission. The complete synthetic
presentation finished with voice, robot motion, and real Telegram sending off.
`itu show voice status` reported 262 missing programme clips / 9,737 text
characters; the live smoke-test clip is separate from those programme cues.

Still required for the presentation: listen to and approve the selected voice,
pregenerate/review the complete programme, and rehearse sound on the actual
laptop/beamer setup. No physical robot verification is implied by speech tests.

## Static browser presentation

`scripts/export_web.py --audio-only` refreshes the browser's existing scripted
clips with the configured ElevenLabs voice, without regenerating images or poses.
The export always selects ElevenLabs, even if a legacy Azure setting remains in
the local environment. It reads `python/.env`, falling back to the repository-root
`.env` after the directory migration. Only scripted text and synthesis settings
are sent to ElevenLabs.

Browser MP3s live under `web/audio/elevenlabs/`. Their filenames include the
provider/voice/model/settings cache identity and processing configuration. A
manifest records provider, voice ID, text checksums and MP3 checksums. This fixes
the old text-only export cache, which could silently reuse an older provider's
MP3 after a voice change. The browser plays only ElevenLabs exports identified
by the show data; missing audio continues with subtitles, never another voice.

## Experimental live conversation (legacy)

`itu talk` and show conversation windows currently use Azure Realtime/GPT-Live
for microphone input, turn handling, and audio output. A TTS adapter does not
replace those sessions. Keep them explicitly labeled Azure experiments and keep
show conversation windows off for the ElevenLabs presentation; do not delete
them or claim that all Azure dependencies are removed.

If those modes also need the new voice, schedule a separate migration: microphone
→ speech recognition → existing character/context and guarded tool routing → text
answer → ElevenLabs TTS, with interruption and echo handling. Evaluate an
ElevenLabs conversation service against that alternative before implementation.
Preserve talk's existing tool guards and the show conversation's no-tool boundary.
This follow-up needs its own privacy/latency checks; it adds no conversation step
to the approved presentation.

## Legacy Azure implementation (reference)

The following applies to `VOICE_PROVIDER=openai` and the separate Azure live
conversation experiments. The projector describes the voice as AI-generated
without naming a provider. These Azure settings do not configure ElevenLabs.

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
