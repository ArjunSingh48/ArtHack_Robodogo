# The performance — `itu show`

A world with no living dogs. A small robot dog that has only ever seen pictures. Seven chapters of
humanity's dog images, an invitation to a living person, a picture made together, and one question:
**Is this us?**

Performance mode sits on top of the existing installation — same artworks, same pose pipeline, same
voice, same Telegram bot, same safety guard. Free-play (`itu bot`, `itu live`, `itu talk`) is unchanged.

## Contents

1. [Rehearse without hardware](#1-rehearse-without-hardware)
2. [Run the live show](#2-run-the-live-show)
3. [Controls (keyboard and Telegram)](#3-controls)
4. [How the show is built](#4-how-the-show-is-built)
5. [Artworks and how the picture is chosen](#5-artworks)
6. [The pose, the photo, the composition](#6-the-pose-the-photo-the-composition)
7. [Consent and privacy](#7-consent-and-privacy)
8. [Fallbacks](#8-fallbacks)
9. [What is verified and what is not](#9-what-is-verified-and-what-is-not)
10. [Operator run sheet](#10-operator-run-sheet)

---

## 0. Straight to the interactive part

```bash
itu show --interactive --display web --source webcam      # starts at the invitation: pose → photo → ending
itu show --start-at photography …                         # or begin with any chapter
```
During any show: the **✦ Go to interactive** button on the operator page, `pose` on the keyboard, or
`/pose` in Telegram jumps there at once. In Telegram, `/show interactive` starts a performance that
begins with the invitation.

## 1. Rehearse without hardware

Works in a clean checkout: no robot, no camera, no OpenAI key, no Telegram.

```bash
pip install -e ".[dev]"
itu show validate                                   # script + lines + catalog are consistent
itu show --dry-run --auto --fast 0.05               # the whole show, unattended, ~1 minute
```

`--auto` plays automatic mode and adds a **simulated participant** (takes part, agrees, confirms
sending). Variants:

```bash
itu show --dry-run --auto --participant none        # nobody takes part → the "alone" ending
itu show --dry-run --auto --consent no              # no photo
itu show --dry-run --auto --send no                 # photo made, not sent
itu show --dry-run                                  # guided: YOU press Enter between chapters
itu show --dry-run --display web                    # + projector page at http://127.0.0.1:8765/
itu show --dry-run --source webcam                  # your webcam instead of the stand-in figure
```

In a rehearsal: narration appears as subtitles (cached voice clips play if you generated them before);
the camera is a drawn **stand-in figure** unless you pass `--source`; the composition is saved in
`data/runtime/rehearsal/<session>/is-this-us.jpg`; Telegram is **previewed** in
`telegram_preview.txt` next to it, never sent; the robot never moves.

## 2. Run the live show

```bash
cp .env.example .env              # fill in: AZURE_OPENAI_API_KEY, TELEGRAM_*, SHOW_TELEGRAM_CHAT_ID …
itu setup                         # pose model etc.
itu show validate
itu show voice pregenerate        # all narration + short lines → local cache (see docs/VOICE.md)
itu show voice status             # must say N/N cached before the show
itu show --display web --source webcam              # local keyboard control
itu show --display web --source webcam --telegram   # + Telegram controls (the existing bot, same process)
```

* **Guided mode** (default, `SHOW_MODE=guided`): the robot finishes a chapter and waits for the
  operator (`Enter` / `/next`). Best for a jury.
* **Automatic mode** (`--mode automatic`): chapters flow by audio completion. The participant part still
  waits for a real "ready" — nothing is captured by a timer.
* `--display web` serves a projector page (dark, museum label, subtitles, AI-voice notice). Open it
  full-screen on the projector computer. `SHOW_WEB_HOST=0.0.0.0` only on a private network.
* It also prints an **operator page** (`…/?key=…`): the same page with buttons — Next, Pause, Resume,
  Repeat, **✦ Go to interactive**, End — and, when the robot asks something, the matching answers
  (✓ Ready / ✗ Declines, ✓ They agreed / ✗ No photo, Send / Don't send, ✓ They did it). Without the key the
  page has no buttons, so the projector URL can be shown to anyone. Use it on the operator's laptop or phone.
* Camera: `--source webcam[:i]` (a webcam that sees the participant — recommended), `unitree` /
  `zhaw_bridge` (the robot's own camera), `image:<path>`, or `none`.
* With the robot: the show is fully playable with a **stationary** robot. Motion cues are off unless
  you pass `--motion --i-have-physical-estop` **and** set `SHOW_MOTION_ENABLED=true` **and** every
  existing gate is open (see §9).

## 3. Controls

| Action | Keyboard | Telegram (operators only) |
|---|---|---|
| start | (runs at launch) | `/show` |
| next chapter / cue | `Enter` or `n` | `/next` |
| pause / resume | `p` / `r` | `/pause` / `/resume` |
| repeat last segment | `a` | `/repeat` |
| skip current stage | `s` | `/skip` |
| go to the pose session | `pose` · **✦ Go to interactive** button | `/pose` (or start with `/show interactive`) |
| "they did it" in a small moment | `ready` · **✓ They did it** | `/capture` |
| participant is ready | `ready` | `/capture` or the ✓ button |
| participant declines | `d` | `/decline` or the ✗ button |
| photo consent | `y` / `n` (when asked) | ✓ *They agreed* / ✗ *No photo* buttons |
| send the picture | `y` / `n` (when asked) | `/send` or *Send* / *Don't send* |
| conversation: push-to-talk | `l` | `/listen` |
| status | `?` | `/status` (the existing operator status + a performance line) |
| end safely | `q` | `/cancel` |

`/stop` remains the visitor command it always was; the performance uses `/cancel`. Every button carries
the performance's session id — a button from an earlier performance is refused, and any command that
makes no sense in the current state ("send" during narration) is ignored with a message.

## 4. How the show is built

```
data/performance/script.en.md   ← the narration (edit here; Python contains none of it)
data/performance/lines.en.yaml  ← short runtime lines: hints, consent, delivery caption
data/performance/catalog.yaml   ← which picture belongs to which chapter; what may be imitated
                       │
              StoryDirector (src/is_this_us/performance/director.py) — a state machine
   introduction → narration ⇄ artwork_display (holds) ⇄ conversation (optional)
   → invitation → participant_readiness → consent → capture → composition → delivery → conclusion
   (paused / cancelled / error from anywhere)
                       │ orchestrates, owns none of:
   ShowVoice (OpenAI audio + cache) · Stage (terminal / projector) · PoseReader (existing MediaPipe
   pipeline) · composer · PhotoStore · Delivery (Telegram / preview) · MotionCues (existing guard)
```

**The participant sees what the robot sees.** Whenever the robot is looking (the small moments and the
pose session), the projector page shows the camera image next to the painting — mirrored like a mirror,
with the detected skeleton drawn on top and a short checklist: ✓ I can see you · ✓ whole body in the
picture · ✓ in the middle · ✓ sitting · ✓ head tilted. It disappears before the photo/composition. Frames
stay in memory (replaced ~5× per second) and are never saved.

**The participant is part of the story from the first chapter.** Every chapter ends with a small
*moment* (`@try` in the script): the robot turns to the person in front of the camera and asks for
something small that belongs to that chapter — "Is someone there?", "Could you stand like the hunter?",
"Would you sit down… like someone a dog would stay beside?", "Could you tilt your head a little?",
"Could you be very ordinary for a moment?", "Could you do something completely unnecessary?". The camera
looks for a few seconds (two readings in a row) and the robot reacts with the `@yes` or `@no` line. No
photo is taken and nothing is stored. The operator can confirm (`ready` / ✓ They did it) when the camera
misses it, or move on (`Enter`). If at least one moment worked, the invitation begins with "We have
practised a little already." Without a camera the moments are simply left out.

**Script format** (top of `script.en.md`): `## id | Title` chapters; a blank line ends a cue (one audio
clip); `@show: <artifact id>` changes the picture exactly when the next cue starts; `[pause 2]`;
`@hold` (guided wait); `@voice:` delivery direction; `@motion:` an optional robot cue; `@branch:
together|alone|always` for the two endings; `{pose_request}` = the chosen picture's request;
`@try: any|standing|sitting|head_tilt` + request lines + `@yes:` / `@no:` = a moment with the participant;
`{practised}` = the "we have practised" line, only when a moment worked.

Narrative time and robot movement are separate: a motion cue is requested and forgotten; the story never
waits for the body. One thing speaks at a time. Pausing stops the clip; resume plays that cue again from
its start.

## 5. Artworks

`itu show catalog list` prints the show, chapter by chapter.

| Chapter | Pictures |
|---|---|
| 1 The Last Dog | title only |
| 2 Prehistory | gold dog pendant (c. 3300–2900 BCE), tablet with seal of a man and hunting dogs (c. 3100–2900 BCE), bowman and dog relief (Egypt, c. 2051–2000 BCE), bronze man and dog (8th–7th c. BCE) |
| 3 The Middle Ages | Book of Hours leaf (c. 1460, a dog curled at the shepherds' feet), Unicorn Tapestries, enamel medallion (c. 1240–60), Spanish hunting fresco (12th c.) |
| 4 Paintings and Portraits | seven of the existing 24 paintings |
| 5 Photography | daguerreotypes from the 1840s–50s, "Peter" (1860), a snapshot (c. 1890), Raja Deen Dayal's portrait of Robert Hotz and his bulldog (1887) |
| 6 The Internet | the five memes as typographic reference cards (no images — see docs/MEMES.md) |
| 7 The Present | title, then the new picture |

Honesty notes:
* Open museum collections do not offer Palaeolithic cave paintings. "Prehistory" shows the oldest dog
  images we could verify as open access; each label shows its real date.
* The 13 early artifacts were fetched with `itu show catalog build` from The Met / Cleveland APIs; rights
  were checked per record (Met `isPublicDomain`, Cleveland `CC0`) and metadata is stored as returned
  (`data/performance/records.yaml`). Descriptions in `catalog.yaml` repeat museum text or what is plainly
  visible. Pictures are shown alongside narration as illustration, not as proof of each sentence.

**The interactive picture.** Only curated entries with `interactive:` and `safe_to_imitate: true` can be
chosen — a small, reliable set (standing or sitting still; nothing on the floor, nothing near the robot):

| Picture | Pose | Who is imitated |
|---|---|---|
| Zorn, *Mrs. Walter Rathbone Bacon* (1897) | sitting | the woman |
| Traversi, *Saint Margaret of Cortona* (c. 1758) | sitting, head tilted | the saint |
| Raja Deen Dayal, *Robert Hotz Esquire and Bulldog* (1887) | sitting | the man |
| Murillo, *Don Andrés de Andrade y la Cal* (c. 1665–72) | standing | the man |
| Ceruti, *A Woman with a Dog* (1740s) | standing | the woman |
| Rufus Anson, *[Dog posing … in studio chair]* (c. 1855) | sitting | **the dog** (semantic only) |

The robot picks by `SHOW_POSE_PREFERENCE` (is there a chair?), priority, and avoids repeating the
previous performance's picture. Human poses are preferred; the dog photograph is a curated option for
"sit like this dog".

## 6. The pose, the photo, the composition

1. The picture is shown; the robot says the request ("Could you try sitting like this?").
2. The camera (existing MediaPipe pipeline) checks: someone visible, whole body framed, roughly centred,
   sitting/standing, head tilt when the picture asks for it.
3. At most one short hint every ~8 s, never the same twice in a row, at most seven: "A little to your
   left…", "Maybe… sitting down?", "Perhaps tilt your head?", then "Oh! I think we've got it." and
   "Tell me when you are ready." Then silence.
4. **The participant decides when they are ready** (the operator relays: `ready` / `/capture`). There is
   no countdown; if nobody confirms for 4 minutes, the robot gently gives up and the story continues.
5. The robot asks for consent (§7). Only after ✓ does the camera take one frame.
6. Comparison — qualitative only, never a number:
   * human figure: the camera's reading of the participant vs. the curators' description; if the same
     pipeline can also read the painted figure and agrees, the label says so;
   * dog figure: by meaning only ("both seated") — human and dog skeletons are never compared;
   * nothing readable: "a pose taken together", no comparison claimed.
   Nothing about the person's body, looks or ability is ever judged.
7. The composition (2400×1500 JPEG, ivory museum-label design): **THEN** (the artwork, uncropped, with
   title, artist, date, museum, licence) · **NOW** (the photo) · **IS THIS US?** · the shared pose in small
   caps · one honest sentence on how it was compared · a line from the robot · a tiny paw.

## 7. Consent and privacy

* A photo is never taken because the camera sees someone. Sequence: participant says ready → robot asks
  "May I take a photograph of us? Only if you want to." and explains where it may go → operator confirms
  ✓ only if the participant clearly agreed → capture.
* Sending is a **second, separate** confirmation after the participant has seen the picture on screen.
  It goes only to `SHOW_TELEGRAM_CHAT_ID` (a private chat or group), never a channel or public post. The
  participant is not assumed to be that chat's owner — say who will receive it.
* Declining at any point is fine: the story ends with the "alone" ending, which is just as complete.
* Storage: `data/runtime/performance/<session>/` (git-ignored, folder mode 700); deleted after
  `SHOW_PHOTO_RETENTION_MINUTES` (default 60), immediately on `/cancel`, or with `itu show purge`.
  Nothing is uploaded or archived silently.
* Narration audio contains no visitor data. Conversation windows (if enabled) stream microphone audio to
  OpenAI **only while push-to-talk is on**; see docs/VOICE.md.

## 8. Fallbacks

| Problem | What happens |
|---|---|
| No OpenAI key / network down | cached clips play; uncached lines appear as subtitles for their reading time |
| Voice generation slow | 20 s timeout per clip, then subtitle — never blocks |
| Camera fails | with `--telegram`: the operator may send ONE consented photo in Telegram; otherwise the robot says its eyes didn't work and the story continues |
| Pose not readable | the robot says it can't see the pose well and waits for "ready"; the comparison says none was made |
| Telegram fails | "It didn't go through. That's all right. It still happened." — the ending plays |
| Participant declines | "alone" ending |
| GPT-Live unavailable | Realtime push-to-talk fallback; if that fails too, the show just holds on the picture |
| Anything else in the interactive part | logged; the final chapter always plays |

## 9. What is verified and what is not

Verified here (simulated, `pytest` + `itu show --dry-run --auto`): chapter order, every state transition,
pause/resume/repeat/skip/cancel/pose, stale-command rejection, consent and send order, decline paths,
camera/voice/Telegram failure recovery, composition output, retention, Telegram controller and delivery
with fakes, GPT-Live/Realtime session payloads validated against the installed `openai` SDK schemas.

**Not verified** (needs hardware / accounts): real OpenAI audio (no key in this environment), GPT-Live
access for your organisation, a real microphone/speaker, a real Telegram bot, MediaPipe on your camera in
the room's light, the projector machine, and anything on the physical Go2. Motion cues in the script are
two `stand` requests (the invitation and "I can show you something too"), which map to the existing,
guarded `BalanceStand`; they need `SHOW_MOTION_ENABLED=true`, `APP_MODE=installation`,
`ROBOT_MODEL=go2|zhaw_bridge`, `ROBOT_MOTION_ENABLED=true`, `--motion --i-have-physical-estop`, and an
armed guard. Recommended: run the show with the robot **stationary**.

## 10. Operator run sheet

**Day before**
- [ ] `git pull`, `pip install -e ".[vision,voice,talk,bridge,dev]"`, `pytest -q`
- [ ] `itu show validate` → OK
- [ ] `itu voice audition` once with the team; set `OPENAI_VOICE`
- [ ] `itu show voice pregenerate` → `itu show voice status` says all cached
- [ ] `itu show --dry-run --display web` full run with the real projector and speaker
- [ ] Telegram: `SHOW_TELEGRAM_CHAT_ID` set; `/show` in a test run reaches the chat; `/cancel` works

**One hour before**
- [ ] Projector on the show computer; `itu show --display web …` page full-screen
- [ ] Camera framed on the chair/spot for the participant (whole body visible); light checked
- [ ] Chair in place if `SHOW_POSE_PREFERENCE=sitting`
- [ ] Sign visible: "The robot's voice is AI-generated. Photos only with your permission."
- [ ] Robot standing, powered, stationary; remote / e-stop with a second person if motion is used
- [ ] `itu show purge` (no photos from rehearsals left)

**Show**
| Moment | Operator does |
|---|---|
| Lights down | start `itu show --display web --source webcam --telegram` (or `/show`); operator page on your laptop/phone |
| A moment ("Could you stand like the hunter?") | let the person try; ✓ They did it if the camera misses it, Next to move on |
| End of each chapter (guided) | wait a breath, then `Enter` / `/next` |
| Audience laughs / noise | `p` … `r` (the line is repeated) |
| Invitation | choose the participant with a glance; they take the spot |
| Hints | let the robot speak; don't hurry |
| Participant says ready | `ready` / `/capture` |
| Robot asks for the photo | ✓ only if they clearly said yes; otherwise ✗ |
| THEN/NOW on screen | ask the participant if it may be sent; `/send` or *Don't send* |
| Ending | silence after "Is this us?" — then lights |
| Anything wrong | `q` / `/cancel` (photos of this session are deleted); robot: physical stop first |

**After**
- [ ] `itu show purge` unless the participant asked for the picture and it was sent
- [ ] Robot lowered and powered off (remote)
