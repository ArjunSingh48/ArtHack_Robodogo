# Robodogo: the good boy encounter

Agreed product direction, 2026-10-24. This document defines the intended primary
encounter and its presentation. It takes precedence over older descriptions of
product scope in the README, demo scripts, performance documentation, and implementation plans. It
does not claim that all requirements below are already implemented.

The ordered implementation work is recorded in the
[UX alignment plan](UX_ALIGNMENT_PLAN.md). The [demo script](DEMO_SCRIPT.md)
describes this same encounter for rehearsal; it must not introduce extra visitor
steps. Step 1 defines the experience only. Changes to dialogue, delivery, motion,
and launchers belong to later steps of the plan. Step 2 provides independent
caption and optional speech outputs; step 3 connects those captions to the
installation's image delivery. Automatic observation and the complete encounter
sequence still need integration.

## Presentation programme (user decision, 2026-10-24 afternoon)

The presentation (robot dog + laptop + beamer) runs as a fixed programme. This
section was added on the user's explicit direction and takes precedence over the
parts of this document that assume a single encounter, occasional speech only, or
no visitor photo. The single good boy encounter below remains available as the
narrated show's finale (`--programme show`).

1. **Intro.** The shy dog introduces itself briefly (it is a dog, there are no
   dogs left, only pictures; it wants to learn to be a good dog) and shows a first
   picture. It asks once whether it may take pictures of the two of them. The
   operator records the visitor's answer, and then separately confirms whether
   those photos may also be sent to the configured private Telegram chat.
2. **Four rounds:** prehistory/antiquity, the Middle Ages, the Renaissance and
   portraits, internet memes. In each round, a short narration is followed by an
   invitation to take any pose. When the pose is steady, the dog picks the closest
   picture of that era from the curated, open-access set (by the person's coarse
   posture and the curators' annotations; no similarity score is shown). While
   the robot walks over and takes the painted dog's pose next to the person, the
   era's other pictures flash by ("remembering"), so the robot's slowness reads
   as thinking. Then it reveals the picture and holds its pose. The laptop camera
   takes the photo during the hold (only with consent). The beamer shows
   THEN/NOW. Telegram receives the archive picture (short caption + italic
   attribution), and, only with consent plus the operator's send confirmation,
   the photo. The memes round uses no third-party meme image: the robot draws its
   own doge-style meme on the consented photo.
3. **Finale: rescue.** The dog wants to be a rescue dog. A participant lies down
   as if they fainted. The robot comes (stopping farther away from someone on the
   floor), offers a paw, says "Please wake up", and is happy when they get up. It
   sits beside them; a last consented photo goes to the beamer (and Telegram if
   confirmed). It ends with "Am I a good boy already?" / "A really good boy now?"
   / "Is this us?".

Rules that still hold: no photo without consent; no sending without the separate
operator confirmation; only the one configured private chat, never public; photos
in the git-ignored temporary store with retention, purged on cancel; robot motion
only through MotionGuard with operator arming and the physical stop, walking only
with `--walk`; spoken lines and written captions stay separate; operator controls
stay off the audience view. The camera that reads poses and takes the photo is the
laptop camera (`SHOW_CAMERA_SOURCE`); the robot's own camera guides its walking
(`SHOW_WALK_CAMERA`).

## Character and communication

The robot is the main interface. The visitor relates to a shy, timid robot dog
trying to become a real, good boy. Like Pinocchio, it wonders whether it is real
enough. Its body and occasional short spoken questions express that uncertainty.

Approved examples of spoken lines:

- “Am I a good boy already?”
- “A really good boy now?”

Speech is occasional in the encounter itself, not continuous narration or a
mandatory conversation. (The presentation programme above adds a short narration
before each round, on the user's direction.)
Leave space for silence and the physical encounter. The questions do not require
a rating, a chat reply, or a feedback button. Technical status and explanations
belong in operator/debug tools.

## Presentation setup and interface roles

Presentation is a significant part of this prototype. The setup is a robot dog
and a computer connected to a beamer (projector); the computer also plays the
dog's voice. The presentation flow is built around the main dog interaction
scenario below. Projection helps the audience follow that encounter, while the
dog's body and occasional voice remain its expressive center.

The projected view can show the selected archive image and its attribution as
the dog responds. Its visual design and surrounding presentation material remain
to be developed. It follows the same encounter state and selected image as the
robot and Telegram, with operator controls and diagnostics kept separately.

| Interface | Responsibility |
| --- | --- |
| Robot | See the human, occasionally speak, and embody its attempt to be a good boy. |
| Telegram | Display the painting or meme sent by the good boy, with a short message and italic attribution. |
| Computer + beamer | Present the main dog encounter to the audience, including its selected archive image and credit. |
| Operator/debug view | Support setup, inspection, and testing when robot-camera input is unavailable. |

Web technology may support both the presentation and debugging views. The
encounter must not require the visitor to navigate a web page or operate a chat
menu. Any Telegram pairing needed to reach the correct recipient is setup,
not the expressive interaction. Operator controls remain available separately.

## Camera image: where it goes

The robot camera supplies an image to the local installation computer running the
Python application. The application processes it in memory with the pose
estimator, derives posture/gesture information, and matches that observation
against the curated painting and meme collection.

```text
Robot camera → local application → human pose analysis → archive match
                                                        ├→ Telegram image + caption + credit
                                                        ├→ projected archive image + credit
                                                        └→ guarded robot behavior
```

The camera image is input for local analysis. In the single encounter, the image
sent to Telegram is the selected painting or meme, and the visitor frame is not
uploaded or saved. In the presentation programme, a photo is taken only with the
visitor's consent; it is shown on the beamer, kept in the temporary store, and
sent to the private chat only after the operator's separate confirmation. It is
never sent to an AI service.

For this presentation prototype, the dog's voice plays from the computer.
Onboard robot audio is unavailable in this setup. Computer playback is the
accepted presentation arrangement; the voice still expresses the dog's character.
No onboard speaker integration or concealed sound source is required.

## Primary encounter

1. The visitor meets the robot. The operator has prepared the installation and
   established the intended Telegram recipient and armed the robot when physical
   movement is enabled.
2. The robot automatically notices a stable human pose through its camera.
   The local application processes the observation, finds a relevant painting
   or meme, and prepares a supported, guarded dog behavior. The visitor does not
   need to send `/observe` or trigger the encounter from a screen.
3. The dog may say a brief, timid line. It sends the selected archive image to
   the visitor's Telegram chat as a message from the good boy.
4. The physical finale is the robot taking a supported good-boy pose based on
   the selected image and holding it for **10 seconds**. The hold begins after
   reaching/settling into the target pose, not when the command is sent. It
   remains interruptible by stop/safety controls.
5. After the hold and a qualified safe completion, the encounter ends. The
   visitor need not reply, rate the dog, or press a button. Another encounter
   requires a new eligible stable-pose event; the same pending interaction must
   not repeat itself.

Delivery and movement must not depend on the visitor replying to the caption.
Keep one encounter active through the final hold and completion. Do not begin a
second movement while that encounter is still active. The physical completion
sequence must be qualified for the chosen pose; this contract does not prescribe
an unverified return-to-standing movement.

One sustained human pose produces one encounter. After the encounter completes
and the motion cooldown ends, reacquire a stable pose only following a meaningful
pose change or participant departure and reacquisition. Continuing to hold the
same pose must not generate repeated messages or movements. Stability, pose-change,
and reacquisition thresholds are implementation details to tune and verify; the
automatic observation behavior is a requirement, not currently verified readiness.
When a different participant takes over, establish the intended recipient before
starting their encounter. Camera detection does not identify a Telegram account;
never silently reuse the previous participant's recipient for a new person.

## When an encounter cannot finish

| Condition | Required behavior |
| --- | --- |
| Camera unavailable, no usable human observation, or no suitable match | Do not invent a match or execute a pose from stale input. End this attempt and show the reason to the operator; a new attempt requires a new trigger. |
| Voice unavailable | Continue silently. Do not replace speech with explanatory Telegram messages. |
| Image delivery fails | Report the delivery outcome to the operator. Network waiting must be bounded; any later delivery retry must not recapture the visitor or replay the physical action. Do not report a successful image delivery when only text was sent. |
| Requested pose unsupported during planning | Choose an existing approved, holdable approximation, or remain still. Record the actual outcome; do not claim the original pose was reproduced. |
| Movement fails or its outcome is unknown | End the motion attempt through existing stop/failure handling and require operator recovery. Do not automatically attempt a different pose or claim a completed hold. |
| Stop requested during the encounter | Cancel remaining interaction actions and interrupt the hold through the existing stop controls. Do not start another motion or automatically re-arm; operator recovery remains required for real hardware. |

An interrupted hold may be shorter than 10 seconds and must be reported as
interrupted. Command acceptance alone is not evidence that the robot reached or
held a pose. Failures and recovery controls belong to the operator/debug view,
not to additional visitor menus.

## Telegram message contract

Each successful encounter sends one selected image with one short, informal
caption, followed by image attribution at the bottom in italics. The message
should read as a small message from the dog, not a gallery explanation.

Painting captions:

- `me and who?🥺`
- `us?👉👈`

Meme caption:

- `hehe or not hehe?`

Example layout (the bracketed values come from the archive metadata):

> us?👉👈
>
> *[Artist] — [Title], [Date]. [Collection/source]. [Required rights credit].*

The italic block contains real attribution, including required license/source
information. Use Telegram-supported formatting and escape metadata correctly.
For this noncommercial presentation, use the familiar memes with their original
creator/source credits. Licensing clearance is not a prerequisite for this
prototype. Record actual image provenance and presentation use explicitly;
do not describe these images as licensed or as original project recreations.
Keep museum rights credits intact as well.

No story paragraphs, pose analysis, match scores, voice notes, or feedback/action
buttons are part of this image-message format. Do not automatically copy the
spoken line into the caption. Keep necessary pairing, privacy, and operator
messages separate from image delivery.

## Scope and remaining decisions

The narrated historical show, open-ended conversation, Telegram photo uploads,
memory galleries, and consented compositions exist as experiments or other modes.
Their existence does not make them requirements of this encounter. Preserve them
unless their removal is explicitly requested; do not add them to the default UX.
The planned presentation uses the main encounter as its live scenario. Existing
projector components may be reused without adopting the historical show's story
or participant flow.

**Confirmed:** the final good-boy pose is based on the selected image. Use its
curated dog annotation to choose a supported, qualified pose that can be held for
10 seconds. An approved approximation or stillness is acceptable when necessary,
with the actual outcome recorded honestly. Existing gestures are not automatically
holdable poses.

**Confirmed:** encounters start automatically when the robot detects a stable
human pose, after operator preparation, recipient setup, and arming for real
movement. The primary journey does not require a visitor `/observe` command.
The one-pose/one-encounter and reset rules above apply to automatic observation.

The following choices remain open; the documentation does not select a default
on the team's behalf. Record the decisions here before implementing the affected
behavior.

| Decision | Options / boundary |
| --- | --- |
| Occasional speech | Decide how often a short line is used. A silent encounter is valid; continuous narration is outside this flow. |

The step 2 dialogue API accepts an explicit speech request and otherwise returns
silence. This leaves the frequency decision to the encounter integration. English,
Russian, and German speech templates are available with English fallback; image
captions retain the exact approved wording above in all three locales.

## Implementation status and gaps

| Area | Evidence / work remaining |
| --- | --- |
| Local perception and matching | Existing pipeline supports local pose analysis and coarse relationship matching; live camera behavior still needs verification. |
| Automatic observation | Stable-pose triggering, one-event-per-held-pose behavior, and reacquisition/reset rules still need implementation and verification. |
| Telegram | Step 3 prepares one archive photo with an approved caption and italic credit for installation encounters. Recipient checks and bounded delivery distinguish failure from unknown outcomes; console previews use the same caption. Live delivery remains to be verified. |
| Meme images | The five familiar memes have local presentation assets and original creator/source provenance. The explicit presentation-use status follows the user's noncommercial presentation direction; it does not claim a license or a project recreation. |
| Character voice | Step 2's independent captions and optional speech are used by the installation path. It currently stays silent; speech frequency and live playback remain to be decided/verified. Photo-upload and talk experiments retain legacy dialogue. |
| Physical finale | Existing adapter settle times are not a dedicated 10-second final hold. Implement and verify an interruptible hold and its safe exit on the actual robot. |
| Presentation and web | The dog + computer + beamer setup is confirmed. Existing show/projector components are available, but a presentation view following the main encounter still needs integration and rehearsal. Keep operator/debug tools separate from the audience view. |

## Acceptance criteria

- The visitor can experience the encounter by attending to the dog. The web
  page and Telegram commands are not required steps after recipient setup.
- The presentation follows this same encounter on the computer and beamer,
  showing the same selected archive image with credit. The audience view stays
  separate from operator/debug tools and adds no visitor choices.
- A stable human pose triggers the encounter automatically. A sustained pose
  triggers once; another encounter requires the previous encounter and cooldown
  to finish, plus a pose change or departure and stable reacquisition.
- Camera input is processed locally and transiently. The delivered image is the
  selected archive image, not the visitor frame.
- The dog is either silent or speaks a short, timid line. Spoken text and image
  captions are independent outputs.
- Telegram shows one eligible archive image, one short caption appropriate to
  paintings or memes, and real italic attribution at the bottom. The image
  message has no feedback buttons, explanatory paragraphs, or voice note.
- The final pose is based on the selected image and is qualified for a
  10-second hold after settling, unless interrupted. The encounter remains
  exclusive through its safe completion; approximations and stillness are
  recorded as such.
- Stop, perception failure, voice failure, and delivery failure follow the
  behavior above. Retrying delivery never repeats movement.
- The encounter ends without waiting for the visitor to answer. Its completion
  does not silently trigger another encounter.

Verify the software sequence and message format in simulation, actual delivery
and playback through their live integrations, the presentation on the computer
and beamer, and physical pose timing and interruption separately on the robot.
Passing one level does not establish the
others. These criteria describe the target, not completed verification.
