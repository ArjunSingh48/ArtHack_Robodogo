# Demo script: the good boy encounter

This is the intended primary encounter defined in [UX.md](UX.md). It is a script
for implementation and rehearsal, not a claim that the current application
already performs it. Follow [the alignment plan](UX_ALIGNMENT_PLAN.md) for the
remaining changes and verification.

## Before the visitor begins

The operator prepares the robot, local camera-processing application, archive,
computer audio playback, and Telegram delivery. The computer plays the dog's
voice during the presentation; this is the accepted prototype setup.
Pair the intended recipient before the encounter; keep
pairing and privacy information brief and separate from the dog's image message.
The visitor should be able to put the phone aside and attend to the robot.

Use the existing [hardware setup](HARDWARE_SETUP.md) and
[safety checklist](SAFETY_CHECKLIST.md) before physical movement. The chosen pose,
its transition, hold, and safe completion must be qualified on the actual robot.
Existing operator controls include `/arm`, `/disarm`, `/status`, `/qr`, `/open`,
`/close`, `/approve`, `/revoke`, and `/actions`. Keep these out of the visitor's
image message. Software stop controls remain available; they do not replace the
physical emergency stop.

The final pose is a supported pose based on the selected image. The encounter
starts automatically when the prepared, armed installation detects a stable
human pose. Automatic observation is a target behavior still to implement; the
visitor does not issue `/observe` or another chat command to begin.

## One encounter

1. **Meet the dog.** The visitor stands or takes a posture where the robot camera
   can see them, holding it steadily. Their attention is on the dog. No web screen
   is needed. Stable-pose detection starts one encounter automatically.
2. **The dog sees them.** Its camera sends frames to
   the local installation computer. Pose analysis and matching happen there in
   memory. The application selects a painting or meme from the curated archive;
   the visitor frame is not sent to Telegram or an AI service.
3. **A small question, sometimes.** The dog may quietly say “Am I a good boy
   already?” or “A really good boy now?” Leave room for silence. The visitor does
   not need to answer, rate the robot, or start a conversation.
4. **A message from the good boy.** The selected archive image arrives in the
   visitor's Telegram chat. A painting says `me and who?🥺` or `us?👉👈`; a meme
   says `hehe or not hehe?`. Image attribution appears below in italics. This is
   one image message, without explanatory paragraphs, voice notes, or buttons.
5. **The physical finale.** The robot enters a supported, qualified pose based on
   the dog in the selected image. Once it has reached and settled into that pose,
   it holds it for **10 seconds**. The hold remains interruptible. Command
   acceptance alone does not establish that the pose has been reached. Any
   approximation is recorded honestly for the operator; unsupported actions do
   not become improvised movement.
6. **Let the moment end.** The hold completes through the qualified safe ending.
   No reply in Telegram is required. A sustained pose produces one encounter,
   not a repeating loop. Only after completion and safe reset can a changed pose
   or a newly acquired participant start another encounter; a new participant
   must have the intended recipient established. Do not repeat the movement
   because delivery failed.

Example caption under the selected painting:

> us?👉👈
>
> *[Artist] — [Title], [Date]. [Collection/source]. [Required rights credit].*

Use actual archive attribution. Only select an image available for permitted
delivery. The current five meme annotations are reference-only; the intended
meme-image experience needs suitable assets and metadata before it can be shown.

## When something is unavailable

| Situation | Rehearsal behavior |
| --- | --- |
| Missing camera frame, no usable human pose, or no suitable match | End the attempt without a claimed match or physical imitation; show the problem to the operator. |
| Voice unavailable | Continue silently; do not add fallback narration to Telegram. |
| Image delivery fails | Report the delivery problem to the operator. Any delivery retry is separate from robot movement. |
| Pose unsupported during planning | Use an approved, holdable approximation if available and record it as such; otherwise remain still and report the skipped finale to the operator. |
| Motion unavailable, failed, or outcome unknown | Do not claim the finale happened or automatically attempt a different pose. Use existing stop/failure handling and operator recovery. |
| Stop during the encounter | Interrupt the pending sequence/hold using the existing stop controls. Do not continue automatically after a stop. |

## Debugging without the robot

Use substitute camera input and a mock robot to inspect the same intended flow.
The web interface is a debugging aid for the operator/developer, not part of the
visitor encounter. A simulation should show what would be captured, matched,
sent, and performed, with simulated actions and delivery labeled clearly.

Today, `run_simulation.sh` launches the historical `itu show` rehearsal. That is
an existing experiment, not yet a rehearsal of this script. Launcher alignment
belongs to step 6 of the [alignment plan](UX_ALIGNMENT_PLAN.md). Mock timing does
not verify that the physical robot can reach and hold a pose for 10 seconds.

The historical [performance](PERFORMANCE.md), [talk mode](TALK.md), memory
galleries, and consented compositions remain separate experiments. They add no
steps or visitor choices to this primary encounter.
