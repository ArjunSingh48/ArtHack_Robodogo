"""Assembling a performance from settings (shared by `itu show` and the Telegram bot)."""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from ..config import Settings
from ..speech.synthesizer import LANGUAGE_NAMES, make_synthesizer
from ..vision.frame_sources import Frame
from ..vision.synthetic import synthetic_person
from .catalog import PerformanceCatalog
from .conversation import ConversationService
from .delivery import PhotoStore, PreviewDelivery
from .director import Cmd, ShowState, StoryDirector
from .lines import Lines
from .motion import MotionCues
from .pose_session import PoseReader
from .script import load_script
from .stage import MultiStage, Stage, TerminalStage, WebStage
from .voice import ShowVoice, Speaker

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- rehearsal stand-ins
class SyntheticParticipant:
    """Rehearsal camera + estimator: a drawn stand-in figure, clearly not a person's photo."""

    name = "synthetic participant"
    reads_paintings = False  # a stand-in cannot look at a painting

    def __init__(self, posture: str = "sitting", head_tilt_deg: float = 0.0):
        self.posture = posture
        self.head_tilt_deg = head_tilt_deg

    def _person(self):
        return synthetic_person(self.posture, head_tilt_deg=self.head_tilt_deg)

    def read(self) -> Frame:
        p = self._person()
        img = Image.new("RGB", p.image_size, (222, 216, 206))
        d = ImageDraw.Draw(img)
        bones = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24), (23, 25),
                 (25, 27), (24, 26), (26, 28)]
        for a, b in bones:
            if p.visibility[a] > 0.5 and p.visibility[b] > 0.5:
                d.line([tuple(p.xy[a]), tuple(p.xy[b])], fill=(70, 64, 74), width=7)
        hx, hy = p.xy[0]
        d.ellipse([hx - 24, hy - 30, hx + 24, hy + 22], outline=(70, 64, 74), width=6)
        d.text((12, p.image_size[1] - 26), "rehearsal stand-in (synthetic figure)", fill=(120, 112, 118))
        return Frame(image=np.asarray(img), source="synthetic")

    def estimate(self, rgb):
        return [self._person()]

    def close(self) -> None:
        pass


class NoPersonEstimator:
    def estimate(self, rgb):
        return []


# ---------------------------------------------------------------------- participant simulator
@dataclass
class ParticipantScript:
    """Automatic answers for rehearsals (`itu show --dry-run --auto`)."""

    takes_part: bool = True
    consent: bool = True
    send: bool = True
    delay_s: float = 1.0
    answered: list[str] = field(default_factory=list)

    def attach(self, director: StoryDirector) -> None:
        def on_state(d: StoryDirector, st: ShowState) -> None:
            cmd = None
            if st is ShowState.READINESS:
                cmd = Cmd.READY if self.takes_part else Cmd.DECLINE
            elif st is ShowState.CONSENT:
                cmd = Cmd.CONSENT_YES if self.consent else Cmd.CONSENT_NO
            elif st is ShowState.DELIVERY:
                cmd = Cmd.SEND_YES if self.send else Cmd.SEND_NO
            if cmd is None:
                return
            sid = d.session_id

            def fire():
                ok, _ = d.command(cmd, sid)
                if ok:
                    self.answered.append(cmd.value)
            asyncio.get_running_loop().call_later(self.delay_s, fire)

        director.listeners.append(on_state)


# ---------------------------------------------------------------------- keyboard control
KEYS = {
    "": "next", "n": "next", "next": "next", "p": "pause", "pause": "pause", "r": "resume", "resume": "resume",
    "a": "repeat", "again": "repeat", "repeat": "repeat", "s": "skip", "skip": "skip", "c": "cancel", "q": "cancel",
    "cancel": "cancel", "pose": "pose", "ready": "ready", "capture": "ready", "d": "decline", "decline": "decline",
    "l": "listen", "listen": "listen",
}
HELP = ("keys: Enter=next · p=pause · r=resume · a=repeat · s=skip · pose · ready · y/n (consent/send) · "
        "d=decline · l=listen (push-to-talk) · ?=status · q=cancel")


def keyboard_command(director: StoryDirector, line: str) -> tuple[bool, str]:
    key = line.strip().lower()
    if key in ("?", "status"):
        return True, str(director.snapshot())
    if key in ("h", "help"):
        return True, HELP
    if key in ("y", "yes", "n", "no"):
        yes = key.startswith("y")
        if director.state is ShowState.CONSENT:
            return director.command(Cmd.CONSENT_YES if yes else Cmd.CONSENT_NO)
        if director.state is ShowState.DELIVERY:
            return director.command(Cmd.SEND_YES if yes else Cmd.SEND_NO)
        if director.state is ShowState.READINESS and not yes:
            return director.command(Cmd.DECLINE)
        if key == "n":
            return director.command(Cmd.NEXT)
        return False, "nothing to answer yes/no to right now"
    if key in KEYS:
        return director.command(KEYS[key])
    return False, f"unknown key {key!r} — {HELP}"


def keyboard_or_cmd(director: StoryDirector, cmd: str) -> tuple[bool, str]:
    """Web buttons send command names ("pose", "consent_yes" …) or keys ("y")."""
    try:
        return director.command(Cmd(cmd))
    except ValueError:
        return keyboard_command(director, cmd)


def start_keyboard(director: StoryDirector, loop: asyncio.AbstractEventLoop, out=None) -> threading.Thread:
    out = out or sys.stdout

    def worker():
        for line in sys.stdin:
            def handle(line=line):
                ok, msg = keyboard_command(director, line)
                if not ok or line.strip() in ("?", "status", "h", "help"):
                    print(f"   [{msg}]", file=out, flush=True)
            loop.call_soon_threadsafe(handle)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------- assembly
@dataclass
class ShowOptions:
    lang: str = "en"
    dry_run: bool = False
    mode: str | None = None  # guided | automatic (default from settings)
    display: str | None = None
    source: str | None = None  # camera spec; "synthetic:<posture>" for a rehearsal stand-in
    voice: bool = True  # play/generate audio (dry-run: cached clips only)
    fast: float = 1.0  # time scale (<1 = faster rehearsal)
    motion: bool = False
    out_dir: Path | None = None
    seed: int | None = None


def build_stage(settings: Settings, display: str) -> tuple[Stage, WebStage | None]:
    term = TerminalStage(color=sys.stdout.isatty())
    if display == "web":
        web = WebStage(settings.show_web_host, settings.show_web_port)
        log.info("projector page: %s", web.url)
        print(f"Projector page: {web.url}")
        stage = MultiStage(term, web)
        stage.web = web
        return stage, web
    return term, None


def build_director(settings: Settings, opts: ShowOptions, *, stage: Stage | None = None, delivery=None,
                   guard=None, planner=None, art_repo=None, meme_repo=None, estimator=None) -> StoryDirector:
    from ..bot.app import build_estimator, build_library

    data_dir = Path(settings.show_data_dir)
    script = load_script(data_dir, opts.lang)
    if art_repo is None or meme_repo is None:
        lib = build_library(settings)
        art_repo, meme_repo = lib.art, lib.memes
    catalog = PerformanceCatalog(data_dir, art_repo, meme_repo)
    rng = random.Random(opts.seed)
    lines = Lines.load(data_dir, script.lang, rng=rng)

    narration = make_synthesizer(settings, purpose="narration")
    short = make_synthesizer(settings, purpose="lines")
    speaker = Speaker(enabled=opts.voice and opts.fast >= 1.0)
    voice = ShowVoice(narration, short, speaker, lang=script.lang,
                      allow_generation=opts.voice and not opts.dry_run, time_scale=opts.fast)
    if not opts.voice:
        voice.narration = voice.lines = _Silent()

    # camera + pose reading (existing MediaPipe pipeline), or a rehearsal stand-in
    source = opts.source if opts.source is not None else (settings.show_camera_source or settings.camera_source)
    frame_factory, reader = None, None
    if source and source.startswith("synthetic"):
        posture = source.partition(":")[2] or "sitting"
        stand_in = SyntheticParticipant(posture, head_tilt_deg=14 if posture == "sitting" else 0)
        frame_factory = lambda: stand_in  # noqa: E731
        reader = PoseReader(stand_in, mirrored=settings.show_camera_mirrored)
    elif source and source != "none":
        from ..vision.frame_sources import make_frame_source

        est = estimator if estimator is not None else build_estimator(settings)
        frame_factory = lambda: make_frame_source(source, network_interface=settings.robot_network_interface)  # noqa: E731
        reader = PoseReader(est, mirrored=settings.show_camera_mirrored) if est is not None else None

    out_root = Path(opts.out_dir) if opts.out_dir else Path(settings.show_output_dir)
    store = PhotoStore(out_root, retention_minutes=settings.show_photo_retention_minutes)
    if stage is None:
        stage, _ = build_stage(settings, opts.display or settings.show_display)
    if delivery is None and opts.dry_run:
        delivery = PreviewDelivery()

    motion = MotionCues(guard, planner, enabled=bool(opts.motion and settings.show_motion_enabled and not opts.dry_run))

    conversation = None
    if settings.openai_enable_live_conversation and settings.voice_configured and not opts.dry_run:
        def client_factory():
            from openai import AsyncOpenAI

            return AsyncOpenAI(api_key=settings.openai_api_key)

        def audio_factory():
            from ..talk.audio import SoundDeviceAudio

            dev = lambda v: int(v) if v.strip().isdigit() else (v or None)  # noqa: E731
            return SoundDeviceAudio(dev(settings.talk_input_device), dev(settings.talk_output_device))

        conversation = ConversationService(
            enabled=True, client_factory=client_factory, audio_factory=audio_factory, stage=stage,
            live_model=settings.openai_live_model, fallback_model=settings.openai_realtime_model,
            voice=settings.primary_voice, language=LANGUAGE_NAMES.get(script.lang, "English"),
            story_path=settings.story_path)

    return StoryDirector(
        script=script, catalog=catalog, lines=lines, voice=voice, stage=stage, store=store, pose_reader=reader,
        frame_source_factory=frame_factory, delivery=delivery, motion=motion, conversation=conversation,
        mode=opts.mode or settings.show_mode, pose_preference=settings.show_pose_preference,
        tick_s=0.8 * min(1.0, opts.fast) if opts.fast < 1 else 0.8,
        hint_gap_s=8.0 * opts.fast, try_seconds=12.0 * opts.fast, font_path=settings.card_font_path, rng=rng,
        photo_upload_allowed=delivery is not None and not isinstance(delivery, PreviewDelivery))


class _Silent:
    available = False

    def synthesize(self, text, lang="en", direction=""):
        return None

    def cached(self, text, lang="en", direction=""):
        return None
