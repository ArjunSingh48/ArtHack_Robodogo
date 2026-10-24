"""Visitor and operator flows, independent of the Telegram library.

Each flow returns a list of `Reply` objects; `bot/app.py` turns them into aiogram API
calls. This keeps the experience fully testable without a network connection.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from ..artworks.museum_clients import USER_AGENT
from ..artworks.repository import YamlArtworkRepository
from ..cards import render_diptych, render_memory_card
from ..dialogue.engine import DialogueEngine, Texts
from ..domain import Artwork, ExecutionStatus
from ..orchestrator import BusyError, InteractionOrchestrator, InteractionOutcome
from ..storage import Storage
from ..vision.frame_sources import FrameSource

log = logging.getLogger(__name__)

CB_YES, CB_ANOTHER, CB_KEEP, CB_BEGIN = "yes", "another", "keep", "begin"
CB_PHOTO, CB_CONSENT, CB_DECLINE = "photo", "consent", "decline"


@dataclass
class Reply:
    kind: str  # text | photo | voice
    text: str = ""
    photo: Path | str | bytes | None = None  # local path, URL, or JPEG bytes
    voice: Path | None = None
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # rows of (label, callback_data)
    preview_url: str | None = None  # show a link preview (meme references) instead of an image


def pick_lang(language_code: str | None, default: str = "en") -> str:
    code = (language_code or "").split("-")[0].lower()
    return code if code in ("en", "ru", "de") else default


def caption_for(outcome: InteractionOutcome, texts: Texts) -> str:
    a = outcome.artwork
    assert a is not None
    lines = outcome.utterance.lines
    body = [html.escape(lines[1])] + [html.escape(x) for x in lines[2:-1]]
    if getattr(a, "kind", "art") == "meme":
        credit = (f"<b>{html.escape(a.name)}</b> — {html.escape(a.subject)}, {html.escape(a.year)}\n"
                  f"{html.escape(a.attribution)}\n<i>{html.escape(a.cultural_meaning)}</i>\n"
                  f"{html.escape(texts.bot('meme_reference'))}: <a href=\"{a.page_url}\">{html.escape(a.sources[0].label)}</a>")
        if not a.displayable:
            credit += f" · {html.escape(texts.bot('reference_only'))}"
    else:
        credit = f"<b>{html.escape(a.artist)}</b> — <i>{html.escape(a.title)}</i>"
        if a.date:
            credit += f", {html.escape(a.date)}"
        credit += f"\n{html.escape(a.museum)} · <a href=\"{a.page_url}\">{html.escape(a.rights.status)}</a>"
    sim = ""
    if outcome.result.status is ExecutionStatus.SIMULATED:
        sim = "\n\n<code>" + html.escape(texts.bot("simulated_note", behavior=outcome.plan.behavior.value)) + "</code>"
    text = "\n".join(body) + "\n\n" + credit + f"\n\n<b>{html.escape(lines[-1])}</b> 🥺" + sim
    return text[:1024]


class VisitorFlows:
    def __init__(self, *, orchestrator: InteractionOrchestrator, storage: Storage, repo: YamlArtworkRepository,
                 operator_ids: list[int], bot_username: str = "", frame_source_factory=None,
                 imitation_camera_factory=None, default_lang: str = "en", font_path: str = ""):
        self.orch = orchestrator
        self.storage = storage
        self.repo = repo
        self.operator_ids = set(operator_ids)
        self.bot_username = bot_username
        self.frame_source_factory = frame_source_factory
        self.imitation_camera_factory = imitation_camera_factory
        self._frame_source: FrameSource | None = None
        self.default_lang = default_lang
        self.font_path = font_path

    # ------------------------------------------------------------------ helpers
    def texts(self, chat_id: int, lang: str | None = None) -> Texts:
        return Texts(lang or self.storage.visitor_lang(chat_id, self.default_lang))

    def is_operator(self, user_id: int) -> bool:
        return user_id in self.operator_ids

    def painting_source(self, scene) -> Path | str | None:
        """Local file or museum URL for paintings; for memes only a licensed/recreated file, else None."""
        return self.repo.local_image(scene) or self.repo.remote_image(scene)

    async def load_painting(self, scene) -> Image.Image | None:
        local = self.repo.local_image(scene)
        if local:
            return Image.open(local)
        remote = self.repo.remote_image(scene)
        if remote is None:
            return None  # reference-only meme: never fetched, never reproduced
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as http:
            r = await http.get(remote, follow_redirects=True)
            r.raise_for_status()
            return Image.open(BytesIO(r.content))

    def replies_for_outcome(self, chat_id: int, outcome: InteractionOutcome) -> list[Reply]:
        texts = self.texts(chat_id)
        if outcome.artwork is None:
            return [Reply("text", text=html.escape(" ".join(outcome.utterance.lines)))]
        iid = outcome.interaction_id
        buttons = [[(texts.bot("button_yes"), f"{CB_YES}:{iid}")],
                   [(texts.bot("button_another"), f"{CB_ANOTHER}:{iid}"),
                    (texts.bot("button_keep"), f"{CB_KEEP}:{iid}")]]
        if self.imitation_camera_factory is not None and outcome.origin == "installation":
            buttons.append([(texts.bot("button_photo"), f"{CB_PHOTO}:{iid}")])
        image = self.painting_source(outcome.artwork)
        caption = caption_for(outcome, texts)
        main = (Reply("photo", text=caption, photo=image, buttons=buttons) if image is not None
                else Reply("text", text=caption, buttons=buttons, preview_url=str(outcome.artwork.page_url)))
        replies = [Reply("text", text=f"<i>{html.escape(outcome.utterance.lines[0])}</i>"), main]
        if outcome.speech and outcome.speech.ogg_path:
            replies.append(Reply("voice", voice=outcome.speech.ogg_path))
        return replies

    def set_archive(self, chat_id: int, lang: str, archive: str) -> list[Reply]:
        self.storage.touch_visitor(chat_id, lang)
        self.storage.set_visitor_archive(chat_id, archive)
        return [Reply("text", text=f"<i>{html.escape(self.texts(chat_id).bot('archive_' + archive))}</i>")]

    async def imitation_photo(self, chat_id: int, iid: str) -> list[Reply]:
        """Called only after the visitor pressed the consent button. Nothing touches the disk."""
        t = self.texts(chat_id)
        row = self.storage.get_interaction(iid, chat_id)
        if row is None or row.expired or not row.artwork_id:
            return [Reply("text", text=t.bot("expired"))]
        if self.imitation_camera_factory is None or row.origin != "installation":
            return [Reply("text", text=t.bot("photo_unavailable"))]
        scene = self.repo.get(row.artwork_id)
        cam = await asyncio.to_thread(self.imitation_camera_factory)
        try:
            frame = await asyncio.to_thread(cam.read)
        finally:
            cam.close()
        if frame is None or scene is None:
            return [Reply("text", text=t.bot("photo_unavailable"))]
        attempt = Image.fromarray(frame.image)
        del frame
        reference = await self.load_painting(scene)
        caption = scene.reflection_for(t.lang) if getattr(scene, "kind", "art") == "meme" else (scene.poem or "")
        data = await asyncio.to_thread(render_diptych, reference, attempt, scene, caption=caption or "Is this us?",
                                       session_id=iid, font_path=self.font_path)
        attempt.close()
        return [Reply("photo", text=f"<i>{html.escape(t.get('robot', 'question'))}</i>", photo=data)]

    # ------------------------------------------------------------------ visitor commands
    def start(self, chat_id: int, lang: str, payload: str = "") -> list[Reply]:
        self.storage.touch_visitor(chat_id, lang)
        t = Texts(lang)
        if payload.startswith("pair_"):
            return self.pair(chat_id, lang, payload.removeprefix("pair_"))
        return [Reply("text", text=html.escape(t.bot("intro")), buttons=[[(t.bot("start_button"), CB_BEGIN)]]),
                Reply("text", text=f"<i>{html.escape(t.bot('privacy_short'))}</i>")]

    def help(self, chat_id: int) -> list[Reply]:
        return [Reply("text", text=self.texts(chat_id).bot("help"))]

    def privacy(self, chat_id: int) -> list[Reply]:
        return [Reply("text", text=self.texts(chat_id).bot("privacy"))]

    def pair(self, chat_id: int, lang: str, token: str) -> list[Reply]:
        t = Texts(lang)
        ok = bool(token) and self.storage.pair(chat_id, token.strip(), lang)
        return [Reply("text", text=t.bot("pair_ok" if ok else "pair_invalid"))]

    async def observe(self, chat_id: int) -> list[Reply]:
        t = self.texts(chat_id)
        if not self.storage.is_paired(chat_id):
            return [Reply("text", text=t.bot("pair_needed"))]
        if not self.orch.observation_open or self.frame_source_factory is None:
            return [Reply("text", text=t.bot("observe_closed"))]
        if self._frame_source is None:
            self._frame_source = await asyncio.to_thread(self.frame_source_factory)
        try:
            await self.orch.observe_installation(chat_id, self._frame_source)  # delivered by notifier
        except BusyError:
            return [Reply("text", text=t.bot("observe_busy"))]
        return []

    async def photo(self, chat_id: int, image_bytes: bytes) -> list[Reply]:
        await self.orch.observe_photo(chat_id, image_bytes)  # delivered by notifier
        return []

    async def callback(self, chat_id: int, data: str) -> list[Reply]:
        t = self.texts(chat_id)
        kind, _, iid = data.partition(":")
        if kind == CB_BEGIN:
            return [Reply("text", text=t.bot("begin"))]
        row = self.storage.get_interaction(iid, chat_id)  # scoped to this chat only
        if row is None or row.expired or not row.artwork_id:
            return [Reply("text", text=t.bot("expired"))]
        dialogue = DialogueEngine(t.lang)
        if kind == CB_PHOTO:
            if self.imitation_camera_factory is None:
                return [Reply("text", text=t.bot("photo_unavailable"))]
            return [Reply("text", text=html.escape(t.bot("consent_ask")),
                          buttons=[[(t.bot("consent_yes"), f"{CB_CONSENT}:{iid}"),
                                    (t.bot("consent_no"), f"{CB_DECLINE}:{iid}")]])]
        if kind == CB_CONSENT:
            return await self.imitation_photo(chat_id, iid)
        if kind == CB_DECLINE:
            return [Reply("text", text=f"<i>{html.escape(t.bot('consent_declined'))}</i>")]
        if kind == CB_YES:
            return [Reply("text", text=f"<i>{html.escape(dialogue.response('yes', iid))}</i>")]
        if kind == CB_KEEP:
            artwork = self.repo.get(row.artwork_id)
            if artwork is None:
                return [Reply("text", text=t.bot("expired"))]
            self.storage.keep_memory(chat_id, artwork.id, iid)
            line = dialogue.response("keep", iid)
            caption = (artwork.reflection_for(t.lang) if getattr(artwork, "kind", "art") == "meme" else artwork.poem)
            card = await self.memory_card(artwork, caption=caption or t.get("robot", "question"), session=iid)
            return [Reply("photo", text=f"<i>{html.escape(line)}</i>", photo=card)]
        if kind == CB_ANOTHER:
            try:
                outcome = await self.orch.another(chat_id, iid)  # delivered by notifier
            except BusyError:
                return [Reply("text", text=t.bot("observe_busy"))]
            if outcome is None:
                return [Reply("text", text=t.bot("expired"))]
            return []
        return []

    async def memory_card(self, artwork: Artwork, caption: str, session: str) -> bytes:
        painting = await self.load_painting(artwork)
        return await asyncio.to_thread(render_memory_card, painting, artwork, caption=caption,
                                       session_id=session, font_path=self.font_path)

    async def memory(self, chat_id: int) -> list[Reply]:
        t = self.texts(chat_id)
        items = self.storage.memories(chat_id)
        if not items:
            return [Reply("text", text=t.bot("memory_empty"))]
        replies = [Reply("text", text=t.bot("memory_header"))]
        for artwork_id, created in items[-10:]:
            a = self.repo.get(artwork_id)
            if a:
                cap = f"<i>{html.escape(a.title)}</i>\n{html.escape(a.artist)} · {html.escape(a.museum)}\n<code>{created[:10]}</code>"
                src = self.painting_source(a)
                replies.append(Reply("photo", text=cap, photo=src) if src is not None
                               else Reply("text", text=cap, preview_url=str(a.page_url)))
        return replies

    def forget(self, chat_id: int) -> list[Reply]:
        t = self.texts(chat_id)
        self.storage.forget(chat_id)
        return [Reply("text", text=t.bot("forget_done"))]

    def stop(self, chat_id: int, user_id: int) -> list[Reply]:
        t = self.texts(chat_id)
        if self.storage.is_paired(chat_id) or self.is_operator(user_id):
            self.orch.stop(f"/stop from {'operator' if self.is_operator(user_id) else 'visitor'}")
            return [Reply("text", text=t.bot("stop_motion"))]
        return [Reply("text", text=t.bot("stop_done"))]

    # ------------------------------------------------------------------ operator
    def operator(self, user_id: int, command: str, arg: str = "") -> list[Reply]:
        if not self.is_operator(user_id):
            return [Reply("text", text=Texts(self.default_lang).bot("operator_only"))]
        guard = self.orch.guard
        if command == "arm":
            ok, why = guard.arm(user_id)
            return [Reply("text", text=("🟢 ARMED. Physical e-stop in hand?" if ok else f"⛔ not armed: {why}"))]
        if command == "disarm":
            guard.disarm(f"operator {user_id}")
            return [Reply("text", text="⚪ disarmed — robot will not move")]
        if command == "open":
            self.orch.observation_open = True
            return [Reply("text", text="observation open")]
        if command == "close":
            self.orch.observation_open = False
            return [Reply("text", text="observation closed")]
        if command == "qr":
            token = self.storage.create_pairing_token()
            link = f"https://t.me/{self.bot_username}?start=pair_{token}" if self.bot_username else f"/pair {token}"
            return [Reply("photo", text=f"Pairing link (valid {int(self.storage.pairing_ttl.total_seconds() // 60)} min):\n{link}",
                          photo=make_qr_png(link))]
        if command in ("approve", "revoke", "actions"):
            from ..domain import RobotBehavior

            planner = self.orch.planner
            if command != "actions":
                try:
                    b = RobotBehavior(arg.strip())
                except ValueError:
                    return [Reply("text", text=f"usage: /{command} <{'|'.join(x.value for x in RobotBehavior)}>")]
                if command == "approve" and not planner.approve(b):
                    return [Reply("text", text=f"⛔ {b.value} is not available on this robot connection")]
                if command == "revoke":
                    planner.revoke(b)
            allowed = sorted(x.value for x in planner.allowed)
            blocked = sorted(x.value for x in RobotBehavior if x not in planner.allowed)
            return [Reply("text", text=f"approved: {', '.join(allowed)}\nnot approved (fallback used): {', '.join(blocked) or '—'}")]
        if command == "status":
            st = guard.robot.status()
            lines = [
                f"robot: {st.model} real={st.is_real} connected={st.connected} body={st.body}",
                f"battery: {st.battery_percent}%  error: {st.error_code}",
                f"armed: {guard.armed}  hardware gates: {guard.hardware_gates_open}",
                f"observation open: {self.orch.observation_open}",
                f"hardware verified: {st.hardware_verified}",
            ]
            return [Reply("text", text="<code>" + html.escape("\n".join(lines)) + "</code>")]
        return []


def make_qr_png(data: str) -> bytes:
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=12)
    qr.add_data(data)
    img = qr.make_image(fill_color=(52, 47, 58), back_color=(247, 242, 233))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
