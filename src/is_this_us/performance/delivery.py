"""Temporary photo storage and confirmed delivery.

* Participant photos and compositions live in `SHOW_OUTPUT_DIR/<session>/` (inside
  `data/runtime/`, which is git-ignored) and are deleted after
  `SHOW_PHOTO_RETENTION_MINUTES`, on cancel, or with `itu show purge`.
* Nothing is sent unless the operator confirms *after* the participant agreed; the
  director enforces the order (capture consent → composition → separate send confirm).
* `PreviewDelivery` (rehearsal) writes what *would* be sent next to the image.
* `TelegramDelivery` sends one photo to ONE configured private chat/group, never a channel
  or public post. The participant is not assumed to be the chat's owner.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class PhotoStore:
    def __init__(self, root: Path, retention_minutes: float = 60):
        self.root = Path(root)
        self.retention_s = max(0.0, retention_minutes * 60)
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        d = self.root / session_id
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    def save(self, session_id: str, name: str, data: bytes) -> Path:
        p = self.session_dir(session_id) / name
        p.write_bytes(data)
        return p

    def purge_session(self, session_id: str) -> None:
        shutil.rmtree(self.root / session_id, ignore_errors=True)

    def cleanup(self, now: float | None = None) -> int:
        """Delete session folders older than the retention period. Returns how many."""
        now = now or time.time()
        removed = 0
        for d in self.root.iterdir() if self.root.exists() else []:
            if d.is_dir() and now - d.stat().st_mtime > self.retention_s:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed

    def purge_all(self) -> int:
        n = 0
        for d in list(self.root.iterdir()) if self.root.exists() else []:
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        return n


@dataclass
class DeliveryResult:
    ok: bool
    detail: str


class Delivery(Protocol):
    name: str

    async def send(self, image: Path, caption: str, session_id: str) -> DeliveryResult: ...


class PreviewDelivery:
    """Rehearsal: nothing leaves the computer."""

    name = "preview"

    def __init__(self):
        self.previews: list[Path] = []

    async def send(self, image: Path, caption: str, session_id: str) -> DeliveryResult:
        p = image.with_name("telegram_preview.txt")
        p.write_text(f"WOULD SEND (rehearsal — not sent)\nphoto: {image.name}\ncaption:\n{caption}\n", encoding="utf-8")
        self.previews.append(p)
        return DeliveryResult(True, f"previewed, not sent → {p}")


class TelegramDelivery:
    name = "telegram"

    def __init__(self, bot, chat_id: int | None):
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, image: Path, caption: str, session_id: str) -> DeliveryResult:
        if not self.chat_id:
            return DeliveryResult(False, "SHOW_TELEGRAM_CHAT_ID is not set")
        from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
        from aiogram.types import FSInputFile

        try:
            await self.bot.send_photo(self.chat_id, FSInputFile(image), caption=caption)
            return DeliveryResult(True, "sent")
        except (TelegramAPIError, TelegramNetworkError, OSError) as exc:
            log.warning("show delivery failed: %s", exc)
            return DeliveryResult(False, f"telegram error: {type(exc).__name__}")
