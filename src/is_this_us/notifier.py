"""Delivery of a finished moment to the visitor (Telegram or console)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

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
