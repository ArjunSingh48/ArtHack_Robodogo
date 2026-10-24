"""aiogram 3 adapter: Telegram updates <-> VisitorFlows."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, LinkPreviewOptions, Message, URLInputFile,
)

from ..artworks.repository import YamlArtworkRepository
from ..config import Settings
from ..motion.guard import MotionGuard
from ..image_message import ImageMessageError
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


async def _send_one(bot: Bot, chat_id: int, r: Reply) -> str:
    markup = _markup(r.buttons)
    if r.kind == "text":
        preview = (LinkPreviewOptions(url=r.preview_url, prefer_large_media=True, show_above_text=True)
                   if r.preview_url else LinkPreviewOptions(is_disabled=True))
        await bot.send_message(chat_id, r.text, reply_markup=markup, link_preview_options=preview)
    elif r.kind == "photo":
        try:
            await bot.send_photo(chat_id, _photo_input(r.photo), caption=r.text or None, reply_markup=markup)
        except TelegramBadRequest as exc:
            if r.image_required:
                raise
            # Explicit rejection permits the legacy experiment's text fallback.
            # A network error may follow a successful send: never duplicate it.
            log.warning("photo delivery failed (%s); falling back to text", exc)
            await bot.send_message(chat_id, r.text or "…", reply_markup=markup)
            return "text fallback"
    elif r.kind == "voice" and r.voice:
        await bot.send_voice(chat_id, FSInputFile(r.voice))
    else:
        raise ValueError(f"invalid reply kind/content: {r.kind}")
    return "sent"


async def send_replies(bot: Bot, chat_id: int, replies: list[Reply], *, timeout_s: float = 30.0) -> str:
    """Bound delivery; retry explicit rate limits, never ambiguous network sends."""
    sent, fallbacks = 0, 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    for r in replies:
        for attempt in range(3):
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return f"failed: delivery deadline reached; sent {sent}/{len(replies)} message(s)"
                status = await asyncio.wait_for(_send_one(bot, chat_id, r), timeout=remaining)
                sent += 1
                fallbacks += status == "text fallback"
                break
            except TelegramRetryAfter as exc:
                delay = max(0, exc.retry_after) + 0.5
                if attempt == 2 or delay >= deadline - loop.time():
                    return f"failed: rate limited; sent {sent}/{len(replies)} message(s)"
                await asyncio.sleep(delay)
            except (TelegramNetworkError, TelegramServerError, TimeoutError) as exc:
                log.warning("delivery outcome unknown; not retrying: %s", exc)
                return f"unknown: delivery may have succeeded; not retried; confirmed {sent}/{len(replies)} message(s)"
            except (TelegramAPIError, OSError, ValueError) as exc:
                log.warning("delivery failed: %s", exc)
                return f"failed: {exc}; sent {sent}/{len(replies)} message(s)"
    result = f"sent {sent}/{len(replies)} message(s)"
    if fallbacks:
        result += f"; {fallbacks} image(s) not delivered (text fallback)"
    return result


class TelegramNotifier:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.flows: VisitorFlows | None = None

    async def deliver(self, chat_id: int, outcome: InteractionOutcome) -> str:
        assert self.flows is not None
        try:
            replies = self.flows.replies_for_outcome(chat_id, outcome)
        except ImageMessageError as exc:
            log.warning("image delivery rejected: %s", exc)
            return f"failed: {exc}"
        if outcome.encounter is None:
            return await send_replies(self.bot, chat_id, replies)
        if not replies:
            return "not sent: stopped or no matched image"
        # The completed outcome is also the retry handle: it holds the selected
        # image and recipient. Delivery retries never invoke perception or motion.
        if outcome.delivery_status.startswith(("sent ", "unknown:", "sending")):
            return outcome.delivery_status
        outcome.delivery_status = "sending"
        try:
            outcome.delivery_status = await send_replies(self.bot, chat_id, replies)
        except asyncio.CancelledError:
            outcome.delivery_status = "unknown: delivery cancelled; not retried"
            raise
        return outcome.delivery_status


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

    if settings.robot_model is RobotModel.ZHAW_BRIDGE:
        from ..robot.zhaw_bridge import ZhawBridgeAdapter

        robot = ZhawBridgeAdapter(settings.zhaw_bridge_host, settings.zhaw_bridge_cmd_port)
    elif settings.robot_model is RobotModel.GO2:
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


def build_planner(settings: Settings, robot=None):
    from ..domain import RobotBehavior
    from ..motion.planner import DEFAULT_ALLOWED, SafeMotionPlanner

    unavailable = frozenset(getattr(robot, "unsupported_behaviors", frozenset()))
    if not settings.motion_allowed_behaviors.strip():
        return SafeMotionPlanner(DEFAULT_ALLOWED, unavailable)
    allowed = frozenset(RobotBehavior(x.strip()) for x in settings.motion_allowed_behaviors.split(",") if x.strip())
    return SafeMotionPlanner(allowed, unavailable)


def build_estimator(settings: Settings):
    try:
        from ..vision.pose import MediaPipePoseEstimator

        return MediaPipePoseEstimator(settings.pose_model_path)
    except (ImportError, FileNotFoundError) as exc:
        log.warning("Pose estimation unavailable: %s", exc)
        return None


async def run_bot(settings: Settings, show_options=None) -> None:
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
        planner=build_planner(settings, robot), default_lang=settings.default_language,
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

    # Performance mode lives in the same bot process (`/show`, operators only).
    from ..performance.delivery import TelegramDelivery
    from ..performance.runtime import ShowOptions, build_director
    from .show_controls import SHOW_COMMANDS, ShowController, build_show_router

    show_opts = show_options or ShowOptions(lang=settings.default_language)
    show_delivery = TelegramDelivery(bot, settings.show_telegram_chat_id)
    ctrl = ShowController(
        lambda: build_director(settings, show_opts, delivery=show_delivery, guard=guard, planner=orch.planner,
                               art_repo=repo.art, meme_repo=repo.memes, estimator=orch.estimator),
        settings.telegram_operator_ids,
        recipient_label="the exhibition chat" if settings.show_telegram_chat_id else "nobody (SHOW_TELEGRAM_CHAT_ID unset)")
    flows.extra_status = ctrl.status_line

    dp = Dispatcher()
    dp.include_router(build_show_router(ctrl))  # first: its photo/callback filters are narrow
    dp.include_router(build_router(flows, settings))
    await bot.set_my_commands(VISITOR_COMMANDS)
    from aiogram.types import BotCommandScopeChat

    for op in settings.telegram_operator_ids:  # show commands are visible to operators only
        try:
            await bot.set_my_commands(VISITOR_COMMANDS + SHOW_COMMANDS, scope=BotCommandScopeChat(chat_id=op))
        except Exception as exc:
            log.warning("could not set operator commands for %s: %s", op, exc)

    async def cleanup_loop():
        while True:
            removed = storage.cleanup()
            if removed:
                log.info("expired %d interaction(s)", removed)
            await asyncio.sleep(300)

    task = asyncio.create_task(cleanup_loop())
    log.info("Bot @%s running (mode=%s, robot=%s, voice=%s, archives=%s, default=%s)", me.username,
             settings.app_mode, settings.robot_model, settings.voice_provider if settings.voice_configured else "text-only",
             ",".join(repo.archives()), settings.default_archive)
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        if ctrl.running and ctrl.director is not None:
            ctrl.director.command("cancel")
            await asyncio.sleep(0.5)
        guard.software_stop("bot shutdown")
        guard.shutdown()
        robot.close()
        storage.close()
        await bot.session.close()
