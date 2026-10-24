# Gallery demo script (~3 minutes per visitor)

**Setup.** Robot standing in the marked area. QR code on a small museum-style label beside it
(`/qr` in Telegram, or `itu qr --out label_qr.png`; tokens last `PAIRING_TOKEN_TTL_MINUTES`, so a
screen that refreshes the QR is better than print). Operator nearby, remote in hand, has sent `/arm`.

1. **Invitation.** Visitor scans the QR. The bot says hello and explains it learned dogs from paintings.
   Privacy line is visible. They press *Start*; pairing happens through the QR deep link.
2. **The gesture.** Operator (or label) suggests: *"Sit, kneel, reach out, or simply stand near it."*
   Visitor takes a posture at the edge of the marked area and taps `/observe`.
3. **Remembering.** Pause. The robot says "Wait…" — then the painting arrives on the phone.
4. **The attempt.** The robot sits / lies down / stands / greets. Sometimes it can't — it stays still
   and says so. That failure is part of the work; don't apologise for it.
5. **The question.** "Is this us?" — visitor answers: ♡ yes / ↻ another painting / ✧ keep.
6. **The memory.** *Keep* sends a cream-and-lavender memory card with full credits.
7. **A cultural dog for the camera.** Visitor raises their phone to photograph the robot;
   the intended branch selects a random reviewed meme pose, then names and credits it.
   The visitor's posture does not select a particular meme. Automatic photography detection
   is pending: rehearse the explicit event with `itu simulate --photographing --seed 7`.
   In the present live app, merely raising a phone does not yet trigger this behavior.
8. **The photo (optional).** If an imitation camera is installed, the visitor may press 📷 and agree to one
   photo; they receive a diptych: the memory beside the robot's attempt.
9. **Goodbye.** Mention `/memory` and `/forget`.

**Without the robot** (talks, press, rehearsals): anyone can send the bot a photo of themselves.
The bot answers with a painting and a *simulated* robot pose. The real robot never moves from photos.

**Operator commands**: `/arm`, `/disarm`, `/status`, `/qr`, `/open`, `/close`, `/approve <behavior>`,
`/revoke <behavior>`, `/actions`.
