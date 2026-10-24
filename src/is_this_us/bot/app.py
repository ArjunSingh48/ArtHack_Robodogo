"""aiogram 3 adapter: Telegram updates <-> VisitorFlows."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, LinkPreviewOptions, Message, URLInputFile,
)

from ..artworks.repository import YamlArtworkRepository
from ..config import Settings
from ..motion.guard import MotionGuard
from ..orchestrator import InteractionOrchestrator, InteractionOutcome
from ..robot.base import RobotAdapter
from ..speech.synthesizer import LocalSpeakerPlayer, make_synthesizer
from ..storage import Storage
from ..vision.frame_sources import make_frame_source
from .flows import Reply, VisitorFlows, pick_lang

log = logging.getLogger(__name__)

VISITOR_COMMANDS = [
    BotCommand(command="start", description="Introduction"),
    BotCommand(command="help", description="How the installation works"),
    BotCommand(command="pair", description="Join the installation"),
    BotCommand(command="observe", description="Ask the robot to look at you"),
    BotCommand(command="art", description="Remember through paintings"),
    BotCommand(command="meme", description="Remember through memes"),
    BotCommand(command="surprise", description="Let the robot choose a memory"),
    BotCommand(command="memory", description="Paintings you kept"),
    BotCommand(command="privacy", description="Your data"),
    BotCommand(command="forget", description="Delete your data"),
    BotCommand(command="stop", description="Stop the current moment"),
]


def _markup(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=data)
                                                  for label, data in row] for row in buttons])


def _photo_input(photo):
    if isinstance(photo, bytes):
        return BufferedInputFile(photo, filename="memory.jpg")
    if isinstance(photo, Path):
        return FSInputFile(photo)
    return URLInputFile(str(photo), headers={"User-Agent": "is-this-us/0.2"})


async def _send_one(bot: Bot, chat_id: int, r: Reply) -> None:
    markup = _markup(r.buttons)
    if r.kind == "text":
        preview = (LinkPreviewOptions(url=r.preview_url, prefer_large_media=True, show_above_text=True)
                   if r.preview_url else LinkPreviewOptions(is_disabled=True))
        await bot.send_message(chat_id, r.text, reply_markup=markup, link_preview_options=preview)
    elif r.kind == "photo":
        try:
            await bot.send_photo(chat_id, _photo_input(r.photo), caption=r.text or None, reply_markup=markup)
        except (TelegramBadRequest, TelegramNetworkError) as exc:
            # e.g. museum URL unreachable from Telegram: never lose the moment — send the text
            log.warning("photo delivery failed (%s); falling back to text", exc)
            await bot.send_message(chat_id, r.text or "…", reply_markup=markup)
    elif r.kind == "voice" and r.voice:
        await bot.send_voice(chat_id, FSInputFile(r.voice))


async def send_replies(bot: Bot, chat_id: int, replies: list[Reply]) -> str:
    sent = 0
    for r in replies:
        for attempt in range(3):
            try:
                await _send_one(bot, chat_id, r)
                sent += 1
                break
            except TelegramRetryAfter as exc:  # rate limited: wait as Telegram asks
                await asyncio.sleep(exc.retry_after + 0.5)
            except TelegramNetworkError:
                await asyncio.sleep(1.5 * (attempt + 1))
        await asyncio.sleep(0.6)  # quiet pacing; silence is part of the piece
    return f"sent {sent}/{len(replies)} message(s)"


class TelegramNotifier:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.flows: VisitorFlows | None = None

    async def deliver(self, chat_id: int, outcome: InteractionOutcome) -> str:
        assert self.flows is not None
        return await send_replies(self.bot, chat_id, self.flows.replies_for_outcome(chat_id, outcome))


def build_router(flows: VisitorFlows, settings: Settings) -> Router:
    r = Router()

    def lang_of(msg_or_cb) -> str:
        return pick_lang(getattr(msg_or_cb.from_user, "language_code", None), settings.default_language)

    @r.message(CommandStart())
    async def start(m: Message, command: CommandObject):
        await send_replies(m.bot, m.chat.id, flows.start(m.chat.id, lang_of(m), command.args or ""))

    @r.message(Command("help"))
    async def help_(m: Message):
        await send_replies(m.bot, m.chat.id, flows.help(m.chat.id))

    @r.message(Command("privacy"))
    async def privacy(m: Message):
        await send_replies(m.bot, m.chat.id, flows.privacy(m.chat.id))

    @r.message(Command("pair"))
    async def pair(m: Message, command: CommandObject):
        await send_replies(m.bot, m.chat.id, flows.pair(m.chat.id, lang_of(m), command.args or ""))

    @r.message(Command("observe"))
    async def observe(m: Message):
        await send_replies(m.bot, m.chat.id, [Reply("text", text=f"<i>{flows.texts(m.chat.id).bot('looking')}</i>")])
        await send_replies(m.bot, m.chat.id, await flows.observe(m.chat.id))

    @r.message(Command("art", "meme", "surprise", "both"))
    async def archive(m: Message, command: CommandObject):
        await send_replies(m.bot, m.chat.id, flows.set_archive(m.chat.id, lang_of(m), command.command))

    @r.message(Command("memory"))
    async def memory(m: Message):
        await send_replies(m.bot, m.chat.id, await flows.memory(m.chat.id))

    @r.message(Command("forget"))
    async def forget(m: Message):
        await send_replies(m.bot, m.chat.id, flows.forget(m.chat.id))

    @r.message(Command("stop"))
    async def stop(m: Message):
        await send_replies(m.bot, m.chat.id, flows.stop(m.chat.id, m.from_user.id))

    @r.message(Command("arm", "disarm", "status", "qr", "open", "close", "approve", "revoke", "actions"))
    async def operator(m: Message, command: CommandObject):
        await send_replies(m.bot, m.chat.id, flows.operator(m.from_user.id, command.command, command.args or ""))

    @r.message(F.photo)
    async def photo(m: Message):
        flows.storage.touch_visitor(m.chat.id, lang_of(m))
        await send_replies(m.bot, m.chat.id, [Reply("text", text=f"<i>{flows.texts(m.chat.id).bot('photo_processing')}</i>")])
        buf = BytesIO()
        await m.bot.download(m.photo[-1], destination=buf)  # in memory only, never written to disk
        data = buf.getvalue()
        buf.close()
        await send_replies(m.bot, m.chat.id, await flows.photo(m.chat.id, data))

    @r.callback_query(F.data)
    async def callback(cb: CallbackQuery):
        await cb.answer()
        await send_replies(cb.bot, cb.message.chat.id, await flows.callback(cb.message.chat.id, cb.data))

    return r


def build_robot(settings: Settings) -> RobotAdapter:
    from ..config import RobotModel

    if settings.robot_model is RobotModel.GO2:
        from ..robot.unitree_go2 import UnitreeGo2Adapter

        robot = UnitreeGo2Adapter(settings.robot_network_interface, state_topic=settings.go2_state_topic,
                                  lowstate_topic=settings.go2_lowstate_topic)
    else:
        from ..robot.mock import MockRobotAdapter

        robot = MockRobotAdapter()
    robot.connect()
    return robot


def build_library(settings: Settings):
    from ..memes.repository import MemeRepository
    from ..scenes import SceneLibrary

    memes = MemeRepository(settings.meme_data_dir)
    for name, err in memes.errors.items():
        log.error("meme %s not loaded: %s", name, err)
    return SceneLibrary(YamlArtworkRepository(settings.artwork_data_dir), memes)


def build_planner(settings: Settings):
    from ..domain import RobotBehavior
    from ..motion.planner import DEFAULT_ALLOWED, SafeMotionPlanner

    if not settings.motion_allowed_behaviors.strip():
        return SafeMotionPlanner(DEFAULT_ALLOWED)
    allowed = frozenset(RobotBehavior(x.strip()) for x in settings.motion_allowed_behaviors.split(",") if x.strip())
    return SafeMotionPlanner(allowed)


def build_estimator(settings: Settings):
    try:
        from ..vision.pose import MediaPipePoseEstimator

        return MediaPipePoseEstimator(settings.pose_model_path)
    except (ImportError, FileNotFoundError) as exc:
        log.warning("Pose estimation unavailable: %s", exc)
        return None


async def run_bot(settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty. See README → BotFather.")
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    repo = build_library(settings)
    storage = Storage(settings.database_path, pairing_ttl_min=settings.pairing_token_ttl_minutes,
                      session_ttl_min=settings.session_ttl_minutes)
    robot = build_robot(settings)
    guard = MotionGuard(robot, hardware_gates_open=settings.real_robot_possible,
                        cooldown_s=settings.motion_cooldown_seconds, timeout_s=settings.motion_timeout_seconds,
                        min_battery=settings.min_battery_percent)
    notifier = TelegramNotifier(bot)
    orch = InteractionOrchestrator(
        repo=repo, estimator=build_estimator(settings), storage=storage, guard=guard, notifier=notifier,
        synthesizer=make_synthesizer(settings), player=LocalSpeakerPlayer(settings.local_audio_playback),
        planner=build_planner(settings), default_lang=settings.default_language,
        default_archive=settings.default_archive,
    )
    flows = VisitorFlows(
        orchestrator=orch, storage=storage, repo=repo, operator_ids=settings.telegram_operator_ids,
        bot_username=settings.telegram_bot_username or me.username or "",
        frame_source_factory=lambda: make_frame_source(settings.camera_source,
                                                       network_interface=settings.robot_network_interface),
        imitation_camera_factory=(lambda: make_frame_source(settings.imitation_camera_source))
        if settings.imitation_camera_source else None,
        default_lang=settings.default_language, font_path=settings.card_font_path,
    )
    notifier.flows = flows
    dp = Dispatcher()
    dp.include_router(build_router(flows, settings))
    await bot.set_my_commands(VISITOR_COMMANDS)

    async def cleanup_loop():
        while True:
            removed = storage.cleanup()
            if removed:
                log.info("expired %d interaction(s)", removed)
            await asyncio.sleep(300)

    task = asyncio.create_task(cleanup_loop())
    log.info("Bot @%s running (mode=%s, robot=%s, voice=%s, archives=%s, default=%s)", me.username,
             settings.app_mode, settings.robot_model, "openai-realtime" if settings.voice_configured else "text-only",
             ",".join(repo.archives()), settings.default_archive)
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        guard.software_stop("bot shutdown")
        guard.shutdown()
        robot.close()
        storage.close()
        await bot.session.close()
