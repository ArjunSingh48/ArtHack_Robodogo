"""Delivery of a finished moment to the visitor (Telegram or console)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .domain import ExecutionStatus
from .image_message import ImageMessageError, image_caption

if TYPE_CHECKING:
    from .orchestrator import InteractionOutcome


class Notifier(Protocol):
    async def deliver(self, chat_id: int, outcome: "InteractionOutcome") -> str:
        """Send the moment; return a short delivery status string."""
        ...


class ConsoleNotifier:
    """Prints what the Telegram visitor would receive. Used by `itu simulate`."""

    def __init__(self):
        self.delivered: list[tuple[int, "InteractionOutcome"]] = []

    async def deliver(self, chat_id: int, outcome: "InteractionOutcome") -> str:
        if outcome.recipient_chat_id != chat_id:
            raise ImageMessageError("recipient does not own this interaction")
        if outcome.encounter is not None:
            if outcome.artwork is None:
                return "not sent: no matched image"
            if outcome.result.status is ExecutionStatus.STOPPED:
                return "not sent: stopped"
            if outcome.image_source is None or (isinstance(outcome.image_source, Path) and not outcome.image_source.is_file()):
                raise ImageMessageError("selected archive image is unavailable for presentation delivery")
            caption = image_caption(outcome.artwork, outcome.encounter.caption)
            self.delivered.append((chat_id, outcome))
            print(f"\n  PREVIEW ONLY → Telegram chat {chat_id}")
            print(f"  image: {outcome.image_source}")
            print(caption)
            return "previewed image (not sent)"
        self.delivered.append((chat_id, outcome))
        print("\n" + "─" * 64)
        print(f"  → Telegram chat {chat_id}")
        for line in outcome.utterance.lines[:-1]:
            print(f"    {line}")
        if outcome.artwork:
            print(f"\n    [{'meme' if outcome.artwork.kind == 'meme' else 'painting'}: {outcome.artwork.title}]")
            for cl in outcome.artwork.credit_line().splitlines():
                print(f"    {cl}")
        print(f"\n    {outcome.utterance.lines[-1]} 🥺")
        print("    [♡ Yes, that's us]  [↻ Try another painting]  [✧ Keep this memory]")
        if outcome.speech:
            print(f"    🔊 voice: {outcome.speech.ogg_path or outcome.speech.audio_path}")
        print("─" * 64)
        return "printed"
