# Robodogo: the good boy encounter

Agreed product direction, 2026-10-24. This document defines the intended primary
experience. It takes precedence over older descriptions of product scope in the
README, demo scripts, performance documentation, and implementation plans. It
does not claim that all requirements below are already implemented.

## Character and communication

The robot is the main interface. The visitor relates to a shy, timid robot dog
trying to become a real, good boy. Like Pinocchio, it wonders whether it is real
enough. Its body and occasional short spoken questions express that uncertainty.

Approved examples of spoken lines:

- “Am I a good boy already?”
- “A really good boy now?”

Speech is occasional, not continuous narration or a mandatory conversation.
Leave space for silence and the physical encounter. The questions do not require
a rating, a chat reply, or a feedback button. Technical status and explanations
belong in operator/debug tools.

## Roles of the three interfaces

| Interface | Responsibility |
| --- | --- |
| Robot | See the human, occasionally speak, and embody its attempt to be a good boy. |
| Telegram | Display the painting or meme sent by the good boy, with a short message and italic attribution. |
| Web | Debugging only for now; support inspection and testing when robot-camera input is unavailable. |

The encounter must not require the visitor to navigate a web page or operate a
chat menu. Any Telegram pairing needed to reach the correct recipient is setup,
not the expressive interaction. Operator controls remain available separately.

## Camera image: where it goes

The robot camera supplies an image to the local installation computer running the
Python application. The application processes it in memory with the pose
estimator, derives posture/gesture information, and matches that observation
against the curated painting and meme collection.

```text
Robot camera → local application → human pose analysis → archive match
                                                        ├→ Telegram image + caption + credit
                                                        └→ guarded robot behavior
```

The camera image is input for local analysis. The image sent to Telegram is the
selected painting or meme. The primary flow does not upload the visitor frame to
an AI service or Telegram, or save it to disk. Existing separately consented
photo/performance experiments are outside this primary flow.

The local computer may also provide audio playback; the visitor should experience
the voice as belonging to the dog. Exact speaker placement is an installation
decision, not a reason to move the dialogue into Telegram.

## Primary encounter

1. The visitor meets the robot. The operator has prepared the installation and
   established the intended Telegram recipient.
2. The robot camera captures the human. The local application finds a relevant
   painting or meme and prepares a supported, guarded dog behavior.
3. The dog may say a brief, timid line. It sends the selected archive image to
   the visitor's Telegram chat as a message from the good boy.
4. The physical finale is the robot taking its good-boy pose and holding it for
   **10 seconds**. The hold begins after reaching/settling into the target pose,
   not when the command is sent. It remains interruptible by stop/safety controls.

Delivery and movement must not depend on the visitor replying to the caption.
A delivery failure must not replay the physical action. Missing camera input or
an unsupported pose must not produce invented successful motion; surface the
problem to the operator. Missing voice should leave room for a silent encounter,
not add long fallback messages to Telegram.

## Telegram message contract

Each successful encounter sends the selected image with one short, informal
caption, followed by image attribution at the bottom in italics.

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
Only send images allowed by the archive's existing rights policy; a reference
to a meme is not permission to distribute its image.

No story paragraphs, pose analysis, match scores, voice notes, or feedback/action
buttons are part of this image-message format. Do not automatically copy the
spoken line into the caption. Keep necessary pairing, privacy, and operator
messages separate from image delivery.

## Scope and remaining decisions

The narrated historical show, open-ended conversation, Telegram photo uploads,
memory galleries, and consented compositions exist as experiments or other modes.
Their existence does not make them requirements of this encounter. Preserve them
unless their removal is explicitly requested; do not add them to the default UX.

Resolve these details when implementing the primary flow:

- Which qualified physical pose expresses “good boy”: a fixed pose, or a safe
  pose chosen from the matched image. Do not assume every existing action can
  be held for 10 seconds.
- How the encounter starts and resets: operator trigger or automatic observation.
- How frequently the dog speaks and where the physical voice plays.

## Implementation gaps at the time of this decision

| Area | Evidence / work remaining |
| --- | --- |
| Local perception and matching | Existing pipeline supports local pose analysis and coarse relationship matching; live camera behavior still needs verification. |
| Telegram | Existing messages include longer dialogue and interaction buttons. A dedicated short caption plus italic-credit format is required. Live delivery remains to be verified. |
| Character voice | Existing dialogue/talk modes need alignment with these short, occasional lines. Voice dependencies and live playback require setup/verification. |
| Physical finale | Existing adapter settle times are not a dedicated 10-second final hold. Implement and verify an interruptible hold and its safe exit on the actual robot. |
| Web | Existing show/projector capabilities are development tools for this scope, not a visitor requirement. |

Acceptance is one complete encounter: the visitor attends to the dog, receives
one appropriate archive image with the agreed message format, and sees the final
qualified pose held for 10 seconds unless interrupted. Verify the software path
in simulation and the physical timing separately on hardware.
