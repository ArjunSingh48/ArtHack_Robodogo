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


STANDIN_DIR = Path(__file__).resolve().parents[3] / "data" / "standins"


# the sample stand-ins; each round shows the one whose pose fits that era's pictures best
SAMPLE_ORDER = ["sitting", "standing", "squatting", "leaning_forward", "standing_2", "sitting_2", "standing_3"]


class PhotoParticipant:
    """Still pictures as camera + pose source: an uploaded photo, or sample stand-ins (data/standins, from
    Unsplash) — one per round. Unlike the drawn figure, the real pose estimator reads them (also in 3-D, for
    the avatar)."""

    name = "photo"
    reads_paintings = False

    def __init__(self, pictures: list[tuple[Image.Image, str]], estimator):
        self._pics = []
        for image, label in pictures:
            image = image.convert("RGB")
            image.thumbnail((1280, 1280))
            rgb = np.asarray(image)
            if label:
                ImageDraw.Draw(image).text((12, image.height - 22), label, fill=(240, 236, 228))
            self._pics.append([rgb, np.asarray(image), None])  # what is read, what is shown, the cached pose
        self.estimator, self._i = estimator, 0

    @classmethod
    def from_bytes(cls, data: bytes, estimator, label: str = "uploaded picture") -> "PhotoParticipant":
        import io

        return cls([(Image.open(io.BytesIO(data)), label)], estimator)

    @property
    def pictures(self) -> int:
        return len(self._pics)

    def select(self, i: int) -> None:
        """Which sample is "in front of the camera" now (the director picks one per round)."""
        self._i = i % len(self._pics)

    def read(self) -> Frame:
        return Frame(image=self._pics[self._i][1], source="photo")

    def estimate(self, rgb):
        pic = self._pics[self._i]
        if pic[2] is None:  # a still picture: read it once
            pic[2] = self.estimator.estimate(pic[0])
        return pic[2]

    def close(self) -> None:
        pass


def _standin(name: str) -> tuple[Image.Image, str]:
    return Image.open(STANDIN_DIR / f"{name}.jpg"), f"sample stand-in ({name.split('_')[0]}) · photo: Unsplash"


def sample_photo(posture: str, estimator) -> PhotoParticipant | None:
    if estimator is None or not (STANDIN_DIR / f"{posture}.jpg").exists():
        return None
    return PhotoParticipant([_standin(posture)], estimator)


def sample_set(first: str, estimator) -> PhotoParticipant | None:
    names = [n for n in SAMPLE_ORDER if (STANDIN_DIR / f"{n}.jpg").exists()]
    if estimator is None or not names:
        return None
    names.sort(key=lambda n: n != first)  # SHOW_POSE_PREFERENCE first
    return PhotoParticipant([_standin(n) for n in names], estimator)


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
            if st is ShowState.ARTWORK and d.script.is_presentation:
                cmd = Cmd.NEXT  # rehearsal: "we've looked at the photo"
            elif st is ShowState.RESCUE:
                cmd = Cmd.READY if self.takes_part else Cmd.SKIP
            elif st is ShowState.READINESS:
                cmd = Cmd.READY if self.takes_part else Cmd.DECLINE
            elif st in (ShowState.CONSENT, ShowState.LOBBY):
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
    "l": "listen", "listen": "listen", "sample": "sample", "start": "consent_yes",
}
HELP = ("start: y = camera (they agreed to photos) · sample = sample photo · n = no photos — then "
        "keys: Enter=next · p=pause · r=resume · a=repeat · s=skip · pose · ready · y/n (consent/send) · "
        "d=decline · l=listen (push-to-talk) · ?=status · q=cancel")


def keyboard_command(director: StoryDirector, line: str) -> tuple[bool, str]:
    key = line.strip().lower()
    if key in ("?", "status"):
        return True, str(director.snapshot())
    if key in ("h", "help"):
        return True, HELP
    if key in ("y", "yes", "n", "no"):
        yes = key.startswith("y")
        if director.state in (ShowState.CONSENT, ShowState.LOBBY):
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
    walk: bool = False  # camera-guided walking to the participant
    finale: str | None = None  # encounter | imitate (default SHOW_FINALE)
    programme: str | None = None  # presentation | show (default SHOW_PROGRAMME)
    sim_robot: bool = False  # no Go2: a simulated dog (poses + walking) drawn in the interface
    wait_for_start: bool = False  # begin in the lobby: Start + photo consent before any voice or camera


_WEB_STAGES: dict = {}


class _RobotView:
    """Wraps a camera so its frames are labelled as the robot's ("robot:…") — for rehearsal stand-ins."""

    is_robot_camera = True

    def __init__(self, inner, label: str):
        self.inner, self.name = inner, label

    def read(self):
        f = self.inner.read()
        return None if f is None else Frame(f.image, self.name)

    def close(self):
        pass


def robot_camera_factory(settings: Settings, opts: "ShowOptions", source: str | None, reader):
    """The dog's own camera as a second feed (and the pose camera with SHOW_POSE_CAMERA=robot).
    Real: the bridge stream (SHOW_WALK_CAMERA, default zhaw_bridge). Rehearsal: the drawn stand-in, labelled robot."""
    if reader is None:
        return None
    if source and source.startswith("synthetic"):
        stand_in = getattr(reader, "estimator", None)
        return (lambda: _RobotView(stand_in, "robot:rehearsal")) if stand_in is not None else None
    if opts.dry_run or opts.sim_robot or settings.robot_model.value != "zhaw_bridge" or not settings.show_walk_camera:
        return None
    from ..vision.frame_sources import make_frame_source

    def make():
        src = make_frame_source(settings.show_walk_camera, network_interface=settings.robot_network_interface)
        src.is_robot_camera = True
        return src
    return make

ROBOT_CAMERAS = ("zhaw_bridge", "unitree")


def explain_camera_error(spec: str, exc: Exception) -> str:
    text = str(exc)
    if spec.startswith("webcam") and ("not authorized" in text or "Cannot open webcam" in text):
        return (f"{spec}: macOS did not allow the camera. System Settings → Privacy & Security → Camera → allow "
                f"your terminal app (Terminal / iTerm / VS Code), then QUIT and reopen it and start again.")
    return f"{spec}: {text}"


class CameraOpener:
    """Opens the show camera once. If it fails and SHOW_CAMERA_FALLBACK is set (e.g. zhaw_bridge), uses that
    instead — marked as a ROBOT camera, so the director reads poses with it but takes no "you two" photos
    (the robot cannot photograph itself). A failure is remembered: a denied camera is not retried."""

    def __init__(self, primary: str, fallback: str, make):
        self.primary, self.fallback, self.make = primary, (fallback or "").strip(), make
        self.failures: list[str] = []

    def __call__(self):
        for spec in [self.primary] + ([self.fallback] if self.fallback and self.fallback != self.primary else []):
            try:
                src = self.make(spec)
            except Exception as exc:
                self.failures.append(explain_camera_error(spec, exc))
                log.warning("camera %s failed: %s", spec, exc)
                continue
            try:
                src.is_robot_camera = spec.split(":")[0] in ROBOT_CAMERAS
                src.fallback_from = "; ".join(self.failures) if spec != self.primary else ""
            except AttributeError:
                pass
            return src
        raise OSError(" | ".join(self.failures) or f"camera {self.primary} unavailable")


def operator_key(settings: Settings) -> str:
    """The operator page's key: SHOW_OPERATOR_KEY, else one kept in data/runtime/ (git-ignored) so a
    bookmarked operator link keeps working after a restart."""
    if settings.show_operator_key:
        return settings.show_operator_key
    import secrets

    p = Path(settings.show_output_dir).parent / "operator_key"
    try:
        if p.exists() and p.read_text().strip():
            return p.read_text().strip()
        p.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(9)
        p.write_text(key)
        p.chmod(0o600)
        return key
    except OSError:
        return secrets.token_urlsafe(9)


def build_stage(settings: Settings, display: str) -> tuple[Stage, WebStage | None]:
    term = TerminalStage(color=sys.stdout.isatty())
    if display == "web":
        addr = (settings.show_web_host, settings.show_web_port)
        web = _WEB_STAGES.get(addr) if settings.show_web_port else None
        if web is None or getattr(web, "closed", False):  # one page server per address: a second /show reuses it instead of failing to bind
            web = WebStage(settings.show_web_host, settings.show_web_port, key=operator_key(settings))
            if settings.show_web_port:
                _WEB_STAGES[addr] = web
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
    lang = "en" if settings.voice_provider == "elevenlabs" else opts.lang
    if lang != opts.lang:
        log.info("ElevenLabs presentation speech uses English; ignoring the requested language")
    script = load_script(data_dir, lang, opts.programme or settings.show_programme)
    if art_repo is None or meme_repo is None:
        lib = build_library(settings)
        art_repo, meme_repo = lib.art, lib.memes
    catalog = PerformanceCatalog(data_dir, art_repo, meme_repo)
    rng = random.Random(opts.seed)
    lines = Lines.load(data_dir, script.lang, rng=rng)

    voice_enabled = opts.voice and settings.voice_provider != "none"
    narration = make_synthesizer(settings, purpose="narration") if voice_enabled else _Silent()
    short = make_synthesizer(settings, purpose="lines") if voice_enabled else _Silent()
    speaker = Speaker(enabled=voice_enabled and opts.fast >= 1.0)
    voice = ShowVoice(narration, short, speaker, lang=script.lang,
                      allow_generation=voice_enabled and not opts.dry_run, time_scale=opts.fast)

    # camera + pose reading (existing MediaPipe pipeline), or a rehearsal stand-in
    source = opts.source if opts.source is not None else (settings.show_camera_source or settings.camera_source)
    frame_factory, reader = None, None
    if source and source.startswith("synthetic"):
        posture = source.partition(":")[2] or "sitting"
        stand_in = SyntheticParticipant(posture, head_tilt_deg=14 if posture == "sitting" else 0)
        frame_factory = lambda: stand_in  # noqa: E731
        reader = PoseReader(stand_in, mirrored=settings.show_camera_mirrored)
    elif source and source.startswith("photo:"):  # stills: photo:<posture> · photo:samples (all, per round) · photo:<path>
        spec = source.partition(":")[2] or "sitting"
        est = estimator if estimator is not None else build_estimator(settings)
        pic = (PhotoParticipant([(Image.open(spec), Path(spec).name)], est) if Path(spec).is_file() and est is not None
               else sample_set(settings.show_pose_preference, est) if spec == "samples" else sample_photo(spec, est))
        if pic is None:
            sys.exit(f"--source {source}: no such picture (or the pose model is missing: `itu setup`)")
        frame_factory, reader = (lambda: pic), PoseReader(pic)
    elif source and source != "none":
        from ..vision.frame_sources import make_frame_source

        est = estimator if estimator is not None else build_estimator(settings)
        frame_factory = CameraOpener(source, settings.show_camera_fallback,
                                     lambda spec: make_frame_source(spec, network_interface=settings.robot_network_interface))
        reader = PoseReader(est, mirrored=settings.show_camera_mirrored) if est is not None else None

    robot_factory = robot_camera_factory(settings, opts, source, reader)
    out_root = Path(opts.out_dir) if opts.out_dir else Path(settings.show_output_dir)
    store = PhotoStore(out_root, retention_minutes=settings.show_photo_retention_minutes)
    if stage is None:
        stage, _ = build_stage(settings, opts.display or settings.show_display)
    if delivery is None and opts.dry_run and settings.show_telegram_chat_id:
        delivery = PreviewDelivery(out_root)  # rehearsal of a configured Telegram: writes what WOULD be sent

    sim = None
    if opts.sim_robot:
        from .sim_robot import SimRobot

        sim = SimRobot(approach_settings(settings), pose_time_scale=min(1.0, opts.fast))
        motion = sim.motion  # the same MotionGuard path as the real robot — with the mock robot
    else:
        motion = MotionCues(guard, planner,
                            enabled=bool(opts.motion and settings.show_motion_enabled and not opts.dry_run))

    conversation = None
    if (voice_enabled and settings.voice_provider == "openai" and settings.openai_enable_live_conversation
            and settings.azure_voice_configured and not opts.dry_run):
        def client_factory():
            return settings.openai_client()

        def audio_factory():
            from ..talk.audio import SoundDeviceAudio

            dev = lambda v: int(v) if v.strip().isdigit() else (v or None)  # noqa: E731
            return SoundDeviceAudio(dev(settings.talk_input_device), dev(settings.talk_output_device))

        conversation = ConversationService(
            enabled=True, client_factory=client_factory, audio_factory=audio_factory, stage=stage,
            live_model=settings.openai_live_model, fallback_model=settings.openai_realtime_model,
            voice=settings.character_voice, character=_preset(settings),
            language=LANGUAGE_NAMES.get(script.lang, "English"),
            story_path=settings.story_path)

    approacher = sim.approacher if sim is not None else build_approacher(settings, opts, guard, estimator)

    def photo_factory(data: bytes | None):
        """Lobby: an uploaded picture, or (None) the sample stand-ins, one per round."""
        est = estimator if estimator is not None else build_estimator(settings)
        if est is None:
            return None
        if data is not None:
            return PhotoParticipant.from_bytes(data, est)
        return sample_set(settings.show_pose_preference, est)

    director = StoryDirector(
        approacher=approacher,
        script=script, catalog=catalog, lines=lines, voice=voice, stage=stage, store=store, pose_reader=reader,
        frame_source_factory=frame_factory, delivery=delivery, motion=motion, conversation=conversation,
        mode=opts.mode or settings.show_mode, pose_preference=settings.show_pose_preference,
        tick_s=0.8 * min(1.0, opts.fast) if opts.fast < 1 else 0.8,
        hint_gap_s=8.0 * opts.fast, try_seconds=12.0 * opts.fast,
        moment_hold_s=1.5 * opts.fast, live_linger_s=2.0 * opts.fast,
        finale=opts.finale or settings.show_finale, stable_s=settings.show_stable_seconds * opts.fast,
        final_hold_s=settings.show_final_hold_seconds * opts.fast, photo_in_encounter=settings.show_encounter_photo,
        round_hold_s=settings.show_round_hold_seconds * opts.fast, rescue_wait_s=settings.show_rescue_wait_seconds * opts.fast,
        rescue_wake_s=20.0 * opts.fast, flash_s=settings.show_montage_flash_seconds * opts.fast,
        live_always=settings.show_live_always,
        robot_frame_source_factory=robot_factory, pose_from=settings.show_pose_camera, sim_robot=sim, font_path=settings.card_font_path, rng=rng,
        photo_upload_allowed=delivery is not None and not isinstance(delivery, PreviewDelivery),
        wait_for_start=opts.wait_for_start, photo_factory=photo_factory)
    if delivery is None:
        log.info("Telegram not configured: photos stay on the local display")
    web = getattr(stage, "web", None)
    if web is not None:  # the operator page's buttons work however the show was started (CLI or Telegram)
        try:
            loop = asyncio.get_running_loop()
            web.enable_controls(lambda cmd: keyboard_or_cmd(director, cmd), loop, upload=director.use_picture)
            print(f"Operator page (buttons): {web.control_url}")
        except RuntimeError:
            pass
    return director


def approach_settings(settings: Settings):
    import math

    from .approach import ApproachSettings

    return ApproachSettings(speed=settings.show_walk_speed, max_distance_m=settings.show_walk_max_distance_m,
                            hfov=math.radians(settings.show_walk_hfov_deg), k_sitting=settings.show_walk_k_sitting,
                            k_standing=settings.show_walk_k_standing, beside_m=settings.show_walk_beside_m,
                            face=settings.show_walk_face)


def simulated_approacher(settings: Settings, fast: float = 1.0, person=(2.3, 0.5)):
    """Rehearsal: a simulated room, a simulated robot that walks in it, nothing real moves."""
    import time as _time

    from ..motion.guard import MotionGuard
    from ..robot.mock import MockRobotAdapter
    from .approach import Approacher
    from .sim_world import SimWorld

    t = [0.0]

    def clock():
        return t[0]

    def sleep(d):
        t[0] += d
        _time.sleep(d * fast)

    world = SimWorld(person, "sitting", clock=clock)
    robot = MockRobotAdapter(time_scale=0.0)
    robot.connect()
    robot.world = world
    guard = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0)
    a = Approacher(robot, guard, world.camera, world, approach_settings(settings), clock=clock, sleep=sleep)
    a.world = world
    return a


def build_approacher(settings: Settings, opts: "ShowOptions", guard, estimator=None):
    if not opts.walk:
        return None
    if opts.dry_run:
        return simulated_approacher(settings, opts.fast)
    if not settings.show_walk_enabled:
        log.warning("--walk ignored: SHOW_WALK_ENABLED is false")
        return None
    if not settings.show_walk_calibrated:
        log.warning("--walk ignored: distances are not calibrated — run `itu show walk-test --calibrate 2 "
                    "--posture sitting` and `--posture standing`, put the k values in .env, then SHOW_WALK_CALIBRATED=true")
        return None
    robot = getattr(guard, "robot", None)
    if robot is None or not getattr(robot, "can_walk", False):
        log.warning("--walk ignored: this robot connection cannot walk (use ROBOT_MODEL=zhaw_bridge)")
        return None
    from ..bot.app import build_estimator
    from ..vision.frame_sources import make_frame_source
    from .approach import Approacher

    est = estimator if estimator is not None else build_estimator(settings)
    if est is None:
        log.warning("--walk ignored: pose estimation unavailable")
        return None
    return Approacher(robot, guard,
                      lambda: make_frame_source(settings.show_walk_camera,
                                                network_interface=settings.robot_network_interface),
                      est, approach_settings(settings))


def _preset(settings):
    from ..speech.character import resolve_preset

    return resolve_preset(settings)


class _Silent:
    available = False

    def synthesize(self, text, lang="en", direction=""):
        return None

    def cached(self, text, lang="en", direction=""):
        return None
