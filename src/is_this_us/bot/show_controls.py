"""Performance controls in the EXISTING Telegram bot (no second bot, no second process).

Commands (operators only — TELEGRAM_OPERATOR_IDS):
  /show /next /pause /resume /repeat /pose /capture /send /skip /cancel /listen /decline
`/status` stays the existing operator status and gains a performance line. The visitor
command `/stop` is unchanged; the performance uses `/cancel`.

Prompts that need an answer (photo consent, send confirmation, camera fallback) arrive
as buttons whose callback data carries the performance session id: a button from an
earlier performance is refused. A photo is never sent because the camera saw someone:
capture needs the consent button (or the local `y`), and sending needs a separate /send.
"""

from __future__ import annotations

import asyncio
import html
import logging
from io import BytesIO

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..performance.director import Cmd, ShowState, StoryDirector

log = logging.getLogger(__name__)

SHOW_COMMANDS = [
    BotCommand(command="show", description="(operator) start the performance"),
    BotCommand(command="next", description="(operator) next scene / cue"),
    BotCommand(command="pause", description="(operator) pause"),
    BotCommand(command="resume", description="(operator) resume"),
    BotCommand(command="repeat", description="(operator) repeat the last segment"),
    BotCommand(command="pose", description="(operator) go to the pose session"),
    BotCommand(command="capture", description="(operator) participant is ready"),
    BotCommand(command="send", description="(operator) confirm sending the picture"),
    BotCommand(command="skip", description="(operator) skip the current stage"),
    BotCommand(command="cancel", description="(operator) end the performance safely"),
]
SIMPLE = {"next": Cmd.NEXT, "pause": Cmd.PAUSE, "resume": Cmd.RESUME, "repeat": Cmd.REPEAT, "pose": Cmd.POSE,
          "capture": Cmd.READY, "send": Cmd.SEND_YES, "skip": Cmd.SKIP, "cancel": Cmd.CANCEL,
          "listen": Cmd.LISTEN, "decline": Cmd.DECLINE}
BUTTON_CMDS = {Cmd.CONSENT_YES, Cmd.CONSENT_NO, Cmd.SEND_YES, Cmd.SEND_NO, Cmd.READY, Cmd.DECLINE}


def callback_data(cmd: Cmd, session_id: str) -> str:
    return f"show:{cmd.value}:{session_id}"


def parse_callback(data: str) -> tuple[Cmd, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "show":
        return None
    try:
        cmd = Cmd(parts[1])
    except ValueError:
        return None
    return (cmd, parts[2]) if cmd in BUTTON_CMDS else None


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


def prompt_for(state: ShowState, director: StoryDirector, recipient: str) -> tuple[str, InlineKeyboardMarkup | None] | None:
    sid = director.session_id
    art = director.artifact
    if state is ShowState.READINESS:
        title = html.escape(art.title) if art else "the picture"
        return (f"🎭 Pose session — <i>{title}</i>.\nWhen the participant says they are ready:",
                _kb([[("✓ Ready (/capture)", callback_data(Cmd.READY, sid)),
                      ("✗ Declines", callback_data(Cmd.DECLINE, sid))]]))
    if state is ShowState.CONSENT:
        return ("📷 The robot asked for a photograph. Only press ✓ if the participant clearly agreed "
                f"(the picture may later go to {recipient}, never public).",
                _kb([[("✓ They agreed", callback_data(Cmd.CONSENT_YES, sid)),
                      ("✗ No photo", callback_data(Cmd.CONSENT_NO, sid))]]))
    if state is ShowState.AWAITING_PHOTO:
        return ("The camera failed. If the participant agreed, send ONE photo here now — or:",
                _kb([[("✗ No photo", callback_data(Cmd.CONSENT_NO, sid))]]))
    if state is ShowState.DELIVERY:
        return (f"🖼 The THEN / NOW picture is on the screen. Send it to {recipient}? (separate confirmation)",
                _kb([[("Send", callback_data(Cmd.SEND_YES, sid)), ("Don't send", callback_data(Cmd.SEND_NO, sid))]]))
    if state in (ShowState.FINISHED, ShowState.CANCELLED, ShowState.ERROR):
        extra = f"\ndelivery: {html.escape(director.delivery_detail)}" if director.delivery_detail else ""
        return (f"Performance {sid}: {state.value} (ending: {director.outcome}){extra}", None)
    return None


class ShowController:
    """Owns the one StoryDirector of this bot process and its run task."""

    def __init__(self, director_factory, operator_ids: list[int], *, recipient_label: str = "the exhibition chat"):
        self.director_factory = director_factory
        self.operator_ids = set(operator_ids)
        self.recipient_label = recipient_label
        self.director: StoryDirector | None = None
        self.task: asyncio.Task | None = None
        self.control_chat: int | None = None
        self.bot = None

    def is_operator(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.operator_ids

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def status_line(self) -> str:
        if self.director is None:
            return "performance: not started"
        s = self.director.snapshot()
        return (f"performance {s['session']}: {s['state']} · chapter {s['chapter'] or '—'} · cue {s['cue'] or '—'} · "
                f"picture {s['artwork'] or '—'} · ending {s['outcome']}")

    def _on_state(self, director: StoryDirector, state: ShowState) -> None:
        p = prompt_for(state, director, self.recipient_label)
        if p is None or self.bot is None or self.control_chat is None:
            return
        text, kb = p
        asyncio.ensure_future(self._send(text, kb))

    async def _send(self, text: str, kb) -> None:
        try:
            await self.bot.send_message(self.control_chat, text, reply_markup=kb)
        except Exception as exc:  # Telegram down must not stop the show
            log.warning("could not send show prompt: %s", exc)

    def start(self, chat_id: int, bot) -> str:
        if self.running:
            return "A performance is already running. /status, /cancel"
        self.bot, self.control_chat = bot, chat_id
        self.director = self.director_factory()
        self.director.listeners.append(self._on_state)
        self.task = asyncio.ensure_future(self.director.run())
        return "▶️ Performance starting. /next /pause /resume /repeat /skip /pose /capture /send /cancel"

    def command(self, cmd: Cmd, session_id: str | None = None) -> str:
        if self.director is None or not self.running:
            return "No performance is running. /show to start."
        ok, msg = self.director.command(cmd, session_id)
        return msg if ok else f"⚠️ {msg}"

    def photo(self, data: bytes) -> str:
        if self.director is None:
            return "no performance"
        ok, msg = self.director.supply_photo(data)
        return msg if ok else f"⚠️ {msg}"


def build_show_router(ctrl: ShowController) -> Router:
    r = Router()

    async def reply(m: Message, text: str) -> None:
        await m.answer(text)

    @r.message(Command("show"))
    async def show(m: Message):
        if not ctrl.is_operator(m.from_user.id):
            return await reply(m, "This command is for the operators.")
        await reply(m, ctrl.start(m.chat.id, m.bot))

    @r.message(Command(*SIMPLE.keys()))
    async def simple(m: Message, command: CommandObject):
        if not ctrl.is_operator(m.from_user.id):
            return await reply(m, "This command is for the operators.")
        await reply(m, ctrl.command(SIMPLE[command.command]))

    def awaiting_operator_photo(m: Message) -> bool:
        return (bool(m.photo) and ctrl.is_operator(getattr(m.from_user, "id", None)) and ctrl.director is not None
                and ctrl.director.state is ShowState.AWAITING_PHOTO)

    @r.message(awaiting_operator_photo)
    async def photo(m: Message):
        buf = BytesIO()
        await m.bot.download(m.photo[-1], destination=buf)  # kept in memory, then in the temp session folder
        await reply(m, ctrl.photo(buf.getvalue()))

    @r.callback_query(F.data.startswith("show:"))
    async def button(cb: CallbackQuery):
        parsed = parse_callback(cb.data)
        if parsed is None or not ctrl.is_operator(cb.from_user.id):
            await cb.answer("Not allowed.", show_alert=False)
            return
        cmd, sid = parsed
        msg = ctrl.command(cmd, sid)
        await cb.answer(msg[:180])
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    return r
