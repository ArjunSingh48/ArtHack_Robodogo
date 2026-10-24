"""StoryDirector — runs the performance as an explicit state machine.

The director orchestrates existing services and owns none of their internals:
script/lines (content files), catalog (artworks), ShowVoice (OpenAI audio + cache),
Stage (screen), PoseReader (existing MediaPipe pipeline), composer, PhotoStore,
Delivery (Telegram or preview), MotionCues (existing MotionGuard), Conversation (GPT-Live).

Control: every command goes through `command(cmd, session_id)`. A command is accepted
only if it makes sense in the current state and carries the current session id (when
one is given) — so a late Telegram button or a slow voice clip can never advance an
old or finished performance. One thing speaks at a time. Robot motion is requested
alongside cues and never awaited by the narration (narrative timing ≠ movement).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .catalog import PerformanceCatalog, ShowArtifact
from .lines import Lines
from .live_view import checks_for
from .pose_session import Comparison, PoseCoach, PoseReader, PoseReading, compare, read_painted_figure
from .script import Chapter, Cue, Script
from .stage import Stage, View
from .voice import ShowVoice

log = logging.getLogger(__name__)


class ShowState(StrEnum):
    IDLE = "idle"
    LOBBY = "lobby"  # waiting for the operator's Start (and how photos are taken); nothing plays, no camera
    INTRODUCTION = "introduction"
    NARRATION = "narration"
    ARTWORK = "artwork_display"  # holding on a picture (guided mode)
    PARTICIPATION = "participation"  # a small moment with the participant inside a chapter (@try)
    APPROACH = "approach"  # the robot walks to the participant / back (camera-guided, --walk)
    CONVERSATION = "conversation"  # hold + live conversation window open
    INVITATION = "invitation"
    READINESS = "participant_readiness"
    CONSENT = "consent"
    CAPTURE = "capture"
    AWAITING_PHOTO = "awaiting_photo"  # camera failed; a consented photo may come via Telegram
    COMPOSITION = "composition"
    DELIVERY = "delivery"
    SENDING = "sending"  # encounter: the archive image goes to Telegram (automatic, no visitor reply)
    FINAL_POSE = "final_pose"  # encounter: the robot holds its good-boy pose (10 s after settling)
    REMEMBERING = "remembering"  # presentation round: pictures flash by while the robot walks / takes its pose
    RESCUE = "rescue"  # presentation finale: waiting for someone to lie down / to get up again
    CONCLUSION = "conclusion"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    ERROR = "error"


class Cmd(StrEnum):
    NEXT = "next"
    PAUSE = "pause"
    RESUME = "resume"
    REPEAT = "repeat"
    SKIP = "skip"
    CANCEL = "cancel"
    POSE = "pose"
    READY = "ready"  # participant is ready (/capture)
    DECLINE = "decline"  # participant does not want to take part
    CONSENT_YES = "consent_yes"
    CONSENT_NO = "consent_no"
    SEND_YES = "send_yes"  # /send
    SEND_NO = "send_no"
    LISTEN = "listen"  # push-to-talk toggle in a conversation window
    SAMPLE = "sample"  # start with the sample photos instead of the camera
    CAMERA = "camera"  # back from a photo to the real camera (= they agreed to photos); any time
    SAMPLES = "samples"  # from the camera to the sample stand-in photos; any time


RUNNING = {ShowState.LOBBY, ShowState.INTRODUCTION, ShowState.NARRATION, ShowState.ARTWORK, ShowState.CONVERSATION,
           ShowState.PARTICIPATION, ShowState.APPROACH, ShowState.SENDING, ShowState.FINAL_POSE,
           ShowState.REMEMBERING, ShowState.RESCUE,
           ShowState.INVITATION, ShowState.READINESS, ShowState.CONSENT, ShowState.CAPTURE,
           ShowState.AWAITING_PHOTO, ShowState.COMPOSITION, ShowState.DELIVERY, ShowState.CONCLUSION}
SPEAKING = {ShowState.INTRODUCTION, ShowState.NARRATION, ShowState.INVITATION, ShowState.CONCLUSION}
VALID: dict[Cmd, set[ShowState]] = {
    Cmd.NEXT: SPEAKING | {ShowState.ARTWORK, ShowState.CONVERSATION, ShowState.PARTICIPATION},
    Cmd.PAUSE: RUNNING - {ShowState.COMPOSITION},
    Cmd.RESUME: {ShowState.PAUSED},
    Cmd.REPEAT: SPEAKING | {ShowState.ARTWORK, ShowState.CONVERSATION, ShowState.READINESS, ShowState.PARTICIPATION},
    Cmd.SKIP: RUNNING - {ShowState.COMPOSITION},
    Cmd.CANCEL: RUNNING | {ShowState.PAUSED},
    Cmd.POSE: {ShowState.INTRODUCTION, ShowState.NARRATION, ShowState.ARTWORK, ShowState.CONVERSATION,
               ShowState.INVITATION, ShowState.PARTICIPATION},
    Cmd.READY: {ShowState.READINESS, ShowState.PARTICIPATION, ShowState.RESCUE},
    # (APPROACH accepts only PAUSE / SKIP / CANCEL — all of which stop the walk at once)  # in a moment: "yes, they did it"
    Cmd.DECLINE: {ShowState.INVITATION, ShowState.READINESS, ShowState.CONSENT, ShowState.RESCUE},
    Cmd.CONSENT_YES: {ShowState.CONSENT, ShowState.LOBBY},
    Cmd.CONSENT_NO: {ShowState.CONSENT, ShowState.AWAITING_PHOTO},
    Cmd.SAMPLE: {ShowState.LOBBY},
    Cmd.CAMERA: (RUNNING - {ShowState.LOBBY}) | {ShowState.PAUSED},
    Cmd.SAMPLES: (RUNNING - {ShowState.LOBBY}) | {ShowState.PAUSED},
    Cmd.SEND_YES: {ShowState.DELIVERY},
    Cmd.SEND_NO: {ShowState.DELIVERY},
    Cmd.LISTEN: {ShowState.CONVERSATION},
}


class ShowCancelled(Exception):
    pass


@dataclass
class ShowResult:
    session_id: str
    final_state: ShowState
    outcome: str  # together | alone
    artifact: str | None = None
    composition: Path | None = None
    delivered: bool = False
    delivery_detail: str = ""
    transitions: list[tuple[str, str]] = field(default_factory=list)
    chapters: list[str] = field(default_factory=list)


class StoryDirector:
    def __init__(self, *, script: Script, catalog: PerformanceCatalog, lines: Lines, voice: ShowVoice, stage: Stage,
                 store, pose_reader: PoseReader | None = None, frame_source_factory: Callable | None = None,
                 delivery=None, motion=None, conversation=None, mode: str = "guided",
                 pose_preference: str = "sitting", readiness_timeout_s: float = 240, consent_timeout_s: float = 120,
                 send_timeout_s: float = 180, photo_wait_s: float = 120, hint_gap_s: float = 8.0,
                 tick_s: float = 0.8, theme: str = "ivory", font_path: str = "", rng: random.Random | None = None,
                 photo_upload_allowed: bool = False, try_seconds: float = 12.0, approacher=None,
                 moment_hold_s: float = 1.5, live_linger_s: float = 2.0,
                 finale: str = "encounter", matcher=None, stable_s: float = 3.0, final_hold_s: float = 10.0,
                 photo_in_encounter: bool = True, round_hold_s: float = 6.0, rescue_wait_s: float = 60.0,
                 rescue_wake_s: float = 20.0, flash_s: float = 1.4, live_always: bool = True,
                 robot_frame_source_factory: Callable | None = None, pose_from: str = "robot",
                 photo_view_timeout_s: float | None = None, sim_robot=None,
                 wait_for_start: bool = False, photo_factory: Callable | None = None,
                 clock=time.monotonic):
        self.script, self.catalog, self.lines, self.voice, self.stage = script, catalog, lines, voice, stage
        self.store, self.pose_reader, self.frame_source_factory = store, pose_reader, frame_source_factory
        self.delivery, self.motion, self.conversation = delivery, motion, conversation
        self.mode = mode
        self.pose_preference = pose_preference
        self.readiness_timeout_s, self.consent_timeout_s = readiness_timeout_s, consent_timeout_s
        self.send_timeout_s, self.photo_wait_s = send_timeout_s, photo_wait_s
        self.hint_gap_s, self.tick_s = hint_gap_s, tick_s
        self.theme, self.font_path = theme, font_path
        self.rng = rng or random.Random()
        self.photo_upload_allowed = photo_upload_allowed
        self.try_seconds = try_seconds
        self.approacher = approacher  # camera-guided walking to the participant (None = never walks)
        # finale: "encounter" = the good boy encounter (docs/UX.md): the person takes any pose, the robot
        # finds a matching picture, sends it to Telegram, takes the dog's pose and holds it 10 s.
        # "imitate" = the earlier finale: the robot shows a chosen picture and the person copies it.
        self.finale = finale
        self.matcher = matcher
        self.stable_s = stable_s
        self.final_hold_s = final_hold_s
        self.photo_in_encounter = photo_in_encounter
        self.encounter_log: list[str] = []  # what happened, for the operator (no visitor data)
        # presentation
        self.round_hold_s, self.rescue_wait_s, self.rescue_wake_s = round_hold_s, rescue_wait_s, rescue_wake_s
        self.flash_s = flash_s
        self.live_always = live_always  # keep the camera view on screen the whole show (beside the picture)
        self._live_at = {"laptop": -1e9, "robot": -1e9}  # when a moment last published its own view, per feed
        # the dog's OWN camera (via the bridge): shown as a second feed, and — with pose_from="robot" — the camera
        # that reads the person's pose. The laptop camera ("laptop" feed) takes the photo of the two together.
        self.robot_frame_source_factory = robot_frame_source_factory
        # after a photo appears, it stays until the operator presses Next (None = wait; a number = at most that long)
        self.photo_view_timeout_s = photo_view_timeout_s
        # wait_for_start: begin in LOBBY; the camera opens only after the operator confirms photo consent there.
        # photo_factory(bytes | None) → a camera + estimator for one picture (None = the sample stand-in)
        self.wait_for_start, self.photo_factory = wait_for_start, photo_factory
        self._consent_decided, self._shown_pictures = False, set()
        self._goto: str | None = None  # a step to replay / jump to (operator page hash): chapter or cue id
        self._start_cue: str | None = None  # …and, for a cue id, where in its chapter to start
        self._last_said: tuple[str, str, str] | None = None  # (text, kind, direction): what Repeat says again
        self._real_camera = (frame_source_factory, pose_reader)  # kept while a picture stands in for it
        self.sim_robot = sim_robot  # SimRobot: a drawn robot in the interface instead of / besides the real one
        self.pose_from = pose_from
        self._robot_cam = None
        self._robot_cam_failed = False
        self.photo_consent = False  # asked once in the intro (@consent)
        self.send_ok = False  # separate operator confirmation: may photos go to the Telegram chat?
        self.rounds: list[dict] = []  # what each round did (operator log; no visitor data)
        self.walk_log: list[str] = []
        self.clock = clock
        self.tries: list[tuple[str, bool]] = []  # (cue id, seen) — what the participant joined
        self._camera = None
        self._camera_failed = False
        self._robot_camera = False
        self._walk_cmd: Cmd | None = None  # how the last walk ended (None = by itself)
        self._camera_lock = threading.Lock()  # one camera read at a time (OpenCV is not thread-safe)
        self._jump_to_pose = False
        self.moment_hold_s = moment_hold_s  # a moment counts once the pose is held this long
        self.live_linger_s = live_linger_s  # keep the camera view up this long after the reaction
        self.live_tick_s = min(tick_s, 0.2)  # faster looking while the live view is on screen

        self.session_id = ""
        self.state = ShowState.IDLE
        self._before_pause: ShowState | None = None
        self.chapter_id = ""
        self.cue_id = ""
        self.outcome = "alone"
        self.artifact: ShowArtifact | None = None
        self.composition: Path | None = None
        self.delivered = False
        self.delivery_detail = ""
        self.last_reading: PoseReading | None = None
        self.comparison: Comparison | None = None
        self.transitions: list[tuple[str, str]] = []
        self.chapters_played: list[str] = []
        self.listeners: list[Callable[["StoryDirector", ShowState], None]] = []
        self._inbox: collections.deque[Cmd] = collections.deque()
        self._has_cmd = asyncio.Event()
        self._photo: bytes | None = None
        self._photo_event = asyncio.Event()
        self._recent: set[str] = set()
        self._capture_source = ""

    # ------------------------------------------------------------------ public control
    def command(self, cmd: Cmd | str, session_id: str | None = None) -> tuple[bool, str]:
        try:
            cmd = Cmd(cmd)
        except ValueError:
            return False, f"unknown command {cmd!r}"
        if session_id is not None and session_id != self.session_id:
            return False, "that belongs to an earlier performance — ignored"
        if self.state not in VALID[cmd]:
            return False, f"'{cmd.value}' does nothing while {self.state.value}"
        if cmd is Cmd.CAMERA:
            return self._switch_to_camera()
        if cmd is Cmd.SAMPLES:
            return self._switch_to_samples()
        if cmd is Cmd.POSE:
            self._jump_to_pose = True  # sticky: honoured at the next safe point, whatever is playing
        self._inbox.append(cmd)
        self._has_cmd.set()
        return True, f"ok: {cmd.value}"

    def goto(self, chapter_id: str) -> tuple[bool, str]:
        """Operator: replay (or jump to) a step. The current step is skipped; the show continues from there."""
        ids = [c.id for c in self.script.chapters]
        steps = ids + [cue.id for c in self.script.chapters for cue in c.cues]
        if chapter_id not in steps:
            return False, f"no step '{chapter_id}' — e.g. {', '.join(ids)}"
        if self.state in (ShowState.IDLE, ShowState.LOBBY) or not self.running:
            return False, "start the show first"
        self._goto = chapter_id
        self._inbox.append(Cmd.CANCEL)  # unwinds the current step like End (a walk stops first) …
        self._has_cmd.set()  # … but run() continues at the chosen step instead of ending
        return True, f"ok: going to {chapter_id}"

    def _switch_to_camera(self) -> tuple[bool, str]:
        """Operator: the visitor is here now and agreed — the real camera instead of the sample photos."""
        factory, reader = self._real_camera
        if factory is None or reader is None:
            return False, "no camera configured for this show (--source)"
        if self.frame_source_factory is factory:
            return False, "already using the camera"
        self.frame_source_factory, self.pose_reader = factory, reader
        self._camera, self._camera_failed = None, False
        self.photo_consent = self._consent_decided = True
        self._note("switched to the camera (they agreed to photos)")
        self._show_source("camera")
        return True, "ok: camera"

    def _show_source(self, kind: str) -> None:
        getattr(self.stage, "source", lambda _: None)(kind)

    def _switch_to_samples(self) -> tuple[bool, str]:
        """Operator: the sample stand-in photos instead of the camera (next readings, next rounds)."""
        if getattr(self._open_camera(), "is_still", False):
            return False, "already using the sample photos"
        pic = self.photo_factory(None) if self.photo_factory is not None else None
        if pic is None:
            return False, "no sample photos available (pose model missing? `itu setup`)"
        self.frame_source_factory, self.pose_reader = (lambda: pic), PoseReader(pic)
        self._camera, self._camera_failed = None, False
        self.photo_consent = self._consent_decided = True
        self._note("switched to the sample photos")
        self._show_source("photos")
        return True, "ok: sample photos"

    def supply_photo(self, data: bytes, session_id: str | None = None) -> tuple[bool, str]:
        """A consented photo from another input (e.g. Telegram) when the camera failed."""
        if session_id is not None and session_id != self.session_id:
            return False, "that belongs to an earlier performance — ignored"
        if self.state is not ShowState.AWAITING_PHOTO:
            return False, "no photo is needed right now"
        self._photo = data
        self._photo_event.set()
        return True, "photo received"

    @property
    def running(self) -> bool:
        return self.state in RUNNING or self.state is ShowState.PAUSED

    def snapshot(self) -> dict:
        return {"session": self.session_id, "state": self.state.value, "mode": self.mode, "chapter": self.chapter_id,
                "cue": self.cue_id, "artwork": self.artifact.id if self.artifact else None, "outcome": self.outcome,
                "composition": bool(self.composition), "delivered": self.delivered,
                "moments_joined": sum(seen for _, seen in self.tries)}

    # ------------------------------------------------------------------ state + commands
    def _set_state(self, st: ShowState, detail: str = "") -> None:
        if st is self.state:
            return
        # commands that make no sense in the new state are stale; the rest (e.g. "next" pressed while
        # the robot was finishing a line) still count
        kept = [c for c in self._inbox if st in VALID[c] or st is ShowState.PAUSED and c in (Cmd.RESUME, Cmd.CANCEL)]
        self._inbox.clear()
        self._inbox.extend(kept)
        self.transitions.append((self.state.value, st.value))
        self.state = st
        self.stage.status(st.value, detail)
        getattr(self.stage, "allowed", lambda _: None)([c.value for c in Cmd if st in VALID[c]])
        for fn in list(self.listeners):
            try:
                fn(self, st)
            except Exception as exc:
                log.warning("state listener failed: %s", exc)

    async def _get_cmd(self) -> Cmd:
        while not self._inbox:
            self._has_cmd.clear()
            await self._has_cmd.wait()
        return self._inbox.popleft()

    async def _paused(self) -> None:
        self._before_pause = self.state
        self._set_state(ShowState.PAUSED)
        self.stage.subtitle(None)
        while True:
            cmd = await self._get_cmd()
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.RESUME:
                self._set_state(self._before_pause or ShowState.NARRATION)
                return

    async def _race(self, work: asyncio.Task, interrupts: set[Cmd]) -> Cmd | None:
        """Run `work` until it finishes (→ None) or an interrupting command arrives (→ cmd).

        PAUSE and CANCEL are always interrupts. Other commands wait until `work` is done
        (e.g. "ready" said while the robot is still finishing a hint)."""
        interrupts = interrupts | {Cmd.PAUSE, Cmd.CANCEL}
        deferred: list[Cmd] = []
        try:
            while True:
                getter = asyncio.ensure_future(self._get_cmd())
                done, _ = await asyncio.wait({work, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    cmd = getter.result()
                    if cmd in interrupts:
                        return cmd
                    deferred.append(cmd)
                    continue
                getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)
                work.result()
                return None
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            for cmd in reversed(deferred):
                self._inbox.appendleft(cmd)
            if deferred:
                self._has_cmd.set()

    async def _await_cmd(self, accepted: set[Cmd], timeout: float | None = None) -> Cmd | None:
        """Wait for one of `accepted`; PAUSE pauses (and restarts the timer); CANCEL cancels."""
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - self.clock())
            try:
                cmd = await asyncio.wait_for(self._get_cmd(), remaining)
            except asyncio.TimeoutError:
                return None
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.PAUSE:
                await self._paused()
                deadline = None if timeout is None else self.clock() + timeout
                continue
            if cmd in accepted:
                return cmd

    # ------------------------------------------------------------------ speaking
    async def say(self, text: str, *, kind: str = "lines", direction: str = "") -> None:
        """Speak one line with its subtitle. Interruptible only by PAUSE/CANCEL (pause = say it again)."""
        self._last_said = (text, kind, direction)
        while True:
            self.stage.subtitle(text)
            task = asyncio.ensure_future(self.voice.speak(text, kind=kind, direction=direction))
            cmd = await self._race(task, set())
            self.stage.subtitle(None)
            if cmd is None:
                return
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            await self._paused()

    async def _say_again(self) -> None:
        """Repeat: the last line again (while a picture stays up, or while waiting for a pose)."""
        if self._last_said is not None:
            text, kind, direction = self._last_said
            await self.say(text, kind=kind, direction=direction)

    async def say_line(self, section: str, key: str) -> None:
        await self.say(self.lines.pick(section, key), kind="lines")

    async def _play_cue(self, ch: Chapter, cue: Cue, text: str) -> str:
        """→ "done" | "next" | "skip" | "pose" (repeat and pause replay the cue)."""
        while True:
            if cue.kind == "pause":
                task = asyncio.ensure_future(self.voice.silence(cue.seconds))
            else:
                self._last_said = (text, "narration", ch.voice)
                self.stage.subtitle(text)
                task = asyncio.ensure_future(self.voice.speak(text, kind="narration", direction=ch.voice))
            cmd = await self._race(task, {Cmd.NEXT, Cmd.SKIP, Cmd.REPEAT, Cmd.POSE, Cmd.DECLINE})
            self.stage.subtitle(None)
            if cmd is None:
                return "done"
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.PAUSE:
                await self._paused()
                continue
            if cmd is Cmd.REPEAT:
                continue
            if cmd is Cmd.DECLINE:
                return "decline"
            return {Cmd.NEXT: "next", Cmd.SKIP: "skip", Cmd.POSE: "pose"}[cmd]

    # ------------------------------------------------------------------ display + motion
    def _show(self, what: str | None) -> None:
        if not what:
            return
        if what == "chosen":
            if self.artifact is not None:
                self.stage.show(View("artifact", artifact=self.artifact))
            return
        if what == "composition":
            if self.composition is not None:
                self.stage.show(View("composition", image=self.composition))
            return
        if what == "memes":
            self.stage.show(View("memes", memes=self.catalog.for_chapter("internet")))
            return
        if what in ("title", "blank"):
            self.stage.show(View(what))
            return
        a = self.catalog.get(what)
        if a is None:
            log.warning("script shows %s, which is not in the catalog — keeping the current picture", what)
            return
        self.stage.show(View("artifact", artifact=a))

    def _move(self, behavior: str | None) -> None:
        if behavior and self.motion is not None:
            self.motion.request(behavior)  # returns immediately; never awaited by the story

    # ------------------------------------------------------------------ chapters
    def _chapter_state(self, ch: Chapter) -> ShowState:
        if ch.id == "last_dog":
            return ShowState.INTRODUCTION
        if ch.interactive:
            return ShowState.INVITATION
        if ch.id in ("present", "rescue"):
            return ShowState.CONCLUSION
        return ShowState.NARRATION

    def _choose_artifact(self) -> ShowArtifact | None:
        a = self.catalog.choose_interactive(self.pose_preference, exclude=self._recent, rng=self.rng)
        if a is not None:
            self._recent.add(a.id)
        return a

    def _next_speech(self, cues: list[Cue], i: int) -> Cue | None:
        for c in cues[i + 1:]:
            if c.kind == "speech":
                return c
        return None

    async def _play_chapter(self, ch: Chapter, cues: list[Cue]) -> str:
        """→ "done" | "skip" | "pose" | "decline"."""
        self.chapter_id = ch.id
        self.chapters_played.append(ch.id)
        self.stage.chapter(ch.id, ch.title)
        state = self._chapter_state(ch)
        if self.script.is_presentation and getattr(self, "rounds_only", False):
            self._jump_to_pose = True
        i = 0
        start, self._start_cue = self._start_cue, None
        if start is not None:  # replaying from one of this chapter's steps
            i = next((j for j, c in enumerate(cues) if c.id == start), 0)
            self._jump_to_pose = False
        while i < len(cues):
            if self._goto is not None:
                return "goto"
            if self._jump_to_pose and self.script.is_presentation:
                # "go to interactive": skip the rest of this chapter's narration, straight to its round
                nxt = next((j for j in range(i, len(cues)) if cues[j].kind in ("round", "rescue", "consent")), None)
                if nxt is None:
                    return "done"  # nothing interactive left here: the next chapter goes straight to its cue
                self._jump_to_pose = False
                i = nxt
            elif self._jump_to_pose and not ch.interactive:
                return "pose"
            cue = cues[i]
            self._set_state(state)
            self.cue_id = cue.id
            if cue.show or cue.kind in ("round", "rescue"):
                getattr(self.stage, "step", lambda _: None)(cue.id)  # a picture step: the operator page shows it as #<cue id>
            self._show(cue.show)
            self._move(cue.motion)
            if cue.kind in ("consent", "round", "rescue"):
                handler = {"consent": self._consent_cue, "round": self._round, "rescue": self._rescue}[cue.kind]
                result = await handler(ch, cue)
                if result == "skip":
                    return "skip"
                i += 1
                continue
            if cue.kind == "try":
                result = await self._try_moment(ch, cue)
                if result in ("skip", "pose"):
                    return result
                if cue.hold_after and self.mode == "guided":
                    held = await self._hold(ch)
                    if held == "repeat":
                        continue
                    if held in ("skip", "pose"):
                        return held
                i += 1
                continue
            text = cue.render(
                pose_request=self.lines.text("encounter", "invite") if self.finale == "encounter" else "",
                practised=self.lines.text("invite", "practised") if any(seen for _, seen in self.tries) else "",
            ) if "{" in cue.text else cue.text
            if not text.strip():  # e.g. {practised} when nobody joined a moment
                i += 1
                continue
            nxt = self._next_speech(cues, i)
            if nxt is not None and "{" not in nxt.text:
                self.voice.prefetch(nxt.text, kind="narration", direction=ch.voice)
            result = await self._play_cue(ch, cue, text)
            if result == "pose" and self.script.is_presentation:
                self._jump_to_pose = True  # handled at the top of the loop: on to this chapter's round
                continue
            if result in ("skip", "pose", "decline"):
                return result
            if cue.hold_after and self.mode == "guided":
                held = await self._hold(ch)
                if held == "repeat":
                    continue
                if held in ("skip", "pose"):
                    return held
            i += 1
        return "done"

    async def _hold(self, ch: Chapter) -> str:
        """Guided mode: wait for the operator. A conversation window may open here."""
        if self._jump_to_pose and not ch.interactive:
            return "pose"
        window = None
        if self.conversation is not None and getattr(self.conversation, "enabled", False):
            self._set_state(ShowState.CONVERSATION)
            window = await self.conversation.open(self._context(ch))
            if window is not None:
                await self.say_line("conversation", "opening")
            else:
                self._set_state(ShowState.ARTWORK)
        else:
            self._set_state(ShowState.ARTWORK)
        try:
            while True:
                cmd = await self._await_cmd({Cmd.NEXT, Cmd.REPEAT, Cmd.SKIP, Cmd.POSE, Cmd.LISTEN})
                if cmd is Cmd.LISTEN:
                    if window is not None:
                        await window.toggle_listening()
                    continue
                return {Cmd.NEXT: "next", Cmd.REPEAT: "repeat", Cmd.SKIP: "skip", Cmd.POSE: "pose"}[cmd]
        finally:
            if window is not None:
                await window.close()

    def _context(self, ch: Chapter) -> dict:
        shown = [c.show for c in ch.cues if c.show and self.catalog.get(c.show)]
        return {"chapter": ch.title, "artworks": [self.catalog.get(s).facts() for s in dict.fromkeys(shown)],
                "pose": self.last_reading.summary() if self.last_reading else None}

    # ------------------------------------------------------------------ the interactive part
    def _open_camera(self):
        """One camera for the whole performance, opened when first needed."""
        if self._camera is not None:
            return self._camera
        if self.frame_source_factory is None or self._camera_failed:
            return None
        if self.wait_for_start and not self.photo_consent:  # no camera until the visitor agreed (lobby)
            return None
        try:
            self._camera = self.frame_source_factory()
        except Exception as exc:
            log.warning("camera unavailable: %s", exc)
            self._camera_failed = True  # never retried in this performance (a denied camera stays denied)
            self._alert(f"CAMERA FAILED — {exc}. The show continues without pose reading or photos.")
            return None
        self._robot_camera = bool(getattr(self._camera, "is_robot_camera", False))
        fell_back = getattr(self._camera, "fallback_from", "")
        if fell_back:
            self._alert(f"Laptop camera failed ({fell_back}). Using the ROBOT camera for poses — no photos of the "
                        f"person with the robot will be taken (it cannot photograph itself).")
        elif self._robot_camera:
            self._note("pose camera is the robot's own camera — no joint photos")
        return self._camera

    def _alert(self, text: str) -> None:
        self.encounter_log.append(text)
        log.warning("operator: %s", text)
        alert = getattr(self.stage, "alert", None)
        if alert is not None:
            try:
                alert(text)
            except Exception:
                pass

    @property
    def photos_possible(self) -> bool:
        """A photo of the person WITH the robot needs a camera that is not on the robot."""
        return self._open_camera() is not None and not self._robot_camera

    def _close_camera(self) -> None:
        for cam in (self._camera, self._robot_cam):
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass
        self._camera = None
        self._robot_cam = None

    def _open_robot_camera(self):
        """The dog's own camera (bridge stream), opened once. A failure is reported once and not retried."""
        if self._robot_cam is not None:
            return self._robot_cam
        if self.robot_frame_source_factory is None or self._robot_cam_failed:
            return None
        try:
            self._robot_cam = self.robot_frame_source_factory()
        except Exception as exc:
            self._robot_cam_failed = True
            self._alert(f"ROBOT CAMERA unavailable ({exc}) — is the bridge running? Poses are read with the laptop "
                        f"camera instead.")
            return None
        return self._robot_cam

    def _pose_camera(self):
        """Which camera reads the person's pose: the dog's own (pose_from="robot") when it works, else the laptop's."""
        if self.pose_from == "robot":
            cam = self._open_robot_camera()
            if cam is not None:
                return cam
        return self._open_camera()

    @staticmethod
    def _feed_of(frame) -> str:
        src = str(getattr(frame, "source", "") or "")
        return "robot" if src.split(":")[0] in ("zhaw_bridge", "unitree", "robot") else "laptop"

    # ------------------------------------------------------------------ small moments inside the story
    @staticmethod
    def _moment_seen(target: str, r: PoseReading) -> bool:
        from .pose_session import posture_ok

        if not r.person_visible:
            return False
        if target in ("standing", "sitting"):
            return r.framing in ("ok", "partial") and posture_ok(target, r.posture)
        if target == "head_tilt":
            return bool(r.head_tilt)
        return True  # "any": someone is there and took part

    async def _try_moment(self, ch: Chapter, cue: Cue) -> str:
        """A @try cue: ask, look for a few seconds, react. No photo, nothing stored.

        The camera view is on screen the whole time: while the robot asks, while it looks, during its
        reaction, and a moment after. A moment counts once the pose is HELD (moment_hold_s), not on
        a single frame. → "done" | "skip" | "pose". Without a camera the moment is left out entirely."""
        camera = self._open_camera()
        if camera is None or self.pose_reader is None:
            return "done"
        if self._jump_to_pose:
            return "pose"
        self._set_state(ShowState.PARTICIPATION)

        def checks(reading):
            return checks_for(reading, posture=cue.target if cue.target in ("standing", "sitting") else None,
                              head_tilt=cue.target == "head_tilt", any_person=cue.target == "any")

        result = "done"

        async def ask():
            nonlocal result
            result = await self._play_cue(ch, cue, cue.text)

        await self._with_live(camera, checks, ask())
        if result != "done":  # the operator moved on while the robot was still asking
            self.tries.append((cue.id, False))
            self._clear_live()
            return result if result in ("skip", "pose") else "done"
        deadline = self.clock() + self.try_seconds
        held_since, seen = None, False
        while self.clock() < deadline:
            if self._jump_to_pose:
                self.tries.append((cue.id, False))
                self._clear_live()
                return "pose"
            reading, frame = await self._read_pose(camera)
            await self._publish_live(frame, reading, checks(reading))
            if reading is not None:
                self.last_reading = reading
                if self._moment_seen(cue.target, reading):
                    held_since = held_since if held_since is not None else self.clock()
                    if self.clock() - held_since >= self.moment_hold_s:
                        seen = True
                        break
                else:
                    held_since = None
            try:
                cmd = await asyncio.wait_for(self._get_cmd(), self._tick())
            except asyncio.TimeoutError:
                continue
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.PAUSE:
                await self._paused()
                self._set_state(ShowState.PARTICIPATION)
                deadline = self.clock() + self.try_seconds
                continue
            if cmd is Cmd.READY:  # the operator saw it even if the camera didn't
                seen = True
                break
            if cmd is Cmd.REPEAT:
                await self._with_live(camera, checks, self.say(cue.text, kind="narration", direction=ch.voice))
                deadline = self.clock() + self.try_seconds
                continue
            if cmd in (Cmd.NEXT, Cmd.SKIP, Cmd.POSE):
                self.tries.append((cue.id, False))
                self._clear_live()
                return {Cmd.NEXT: "done", Cmd.SKIP: "skip", Cmd.POSE: "pose"}[cmd]
        self.tries.append((cue.id, seen))

        async def react():
            await self.say(cue.yes if seen else cue.no, kind="narration", direction=ch.voice)
            await asyncio.sleep(self.live_linger_s)

        await self._with_live(camera, checks, react())
        self._clear_live()
        return "pose" if self._jump_to_pose else "done"

    def _locked_read(self, camera):
        with self._camera_lock:
            return camera.read()

    async def _live_feed(self, camera, checks) -> None:
        """Keep the camera view moving while the robot talks (until cancelled)."""
        try:
            while True:
                reading, frame = await self._read_pose(camera)
                await self._publish_live(frame, reading, checks(reading))
                await asyncio.sleep(self._tick())
        except asyncio.CancelledError:
            pass

    async def _with_live(self, camera, checks, coro) -> None:
        """Run `coro` (usually a spoken line) while the live view keeps updating."""
        feed = asyncio.ensure_future(self._live_feed(camera, checks)) if self._live is not None else None
        try:
            await coro
        finally:
            if feed is not None:
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)

    async def _read_pose(self, camera) -> tuple[PoseReading | None, object | None]:
        if camera is None or self.pose_reader is None:
            return None, None
        try:
            frame = await asyncio.to_thread(self._locked_read, camera)
            if frame is None:
                return None, None
            return await asyncio.to_thread(self.pose_reader.read, frame.image, frame.source), frame
        except Exception as exc:
            log.warning("pose reading failed: %s", exc)
            return None, None

    @property
    def _live(self):
        return getattr(self.stage, "live", None)

    async def _publish_live(self, frame, reading: PoseReading | None, checks, ambient: bool = False) -> None:
        """Show the participant what the camera sees (mirrored, skeleton, checklist). In memory only."""
        live = self._live
        if live is None or frame is None:
            return
        feed = self._feed_of(frame)
        if not ambient:
            self._live_at[feed] = self.clock()
        from .live_view import render

        try:
            mirror = self._mirror(frame) and feed == "laptop"  # the dog's view is shown as the dog sees it
            if self.sim_robot is not None and feed == "laptop" and reading is not None and reading.landmarks is not None:
                self.sim_robot.see_person(reading.landmarks, mirror)
            jpeg = await asyncio.to_thread(render, frame.image, reading, mirror=mirror)
            try:
                live(jpeg, [(c.label, c.ok) for c in checks], feed=feed)
            except TypeError:  # a stage with a single feed
                if feed == "laptop":
                    live(jpeg, [(c.label, c.ok) for c in checks])
        except Exception as exc:
            log.debug("live view failed: %s", exc)

    def _mirror(self, frame) -> bool:
        """Show and keep images like a mirror, so left/right match what the participant saw while
        posing — and the painting. Not if the camera already mirrors, nor for the drawn stand-in."""
        return not getattr(self.pose_reader, "mirrored", False) and getattr(frame, "source", "") not in ("synthetic", "photo")

    async def _ambient_live(self) -> None:
        """The camera view stays up for the whole show; moments with their own checklist take over while they
        run (no flicker), and this resumes a moment after they stop."""
        from .live_view import checks_for as _checks

        try:
            while True:
                # looked up every time: the operator may switch from a picture to the camera mid-show
                cams = [c for c in (self._open_camera(), self._open_robot_camera()) if c is not None]
                if not cams or self.pose_reader is None:
                    await asyncio.sleep(0.5)
                    continue
                for camera in cams:
                    if self.state is ShowState.PAUSED:
                        break
                    reading, frame = await self._read_pose(camera)
                    if frame is not None and self.clock() - self._live_at[self._feed_of(frame)] > 0.6:
                        await self._publish_live(frame, reading, _checks(reading, any_person=True), ambient=True)
                await asyncio.sleep(max(self._tick(), 0.12))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("camera view stopped: %s", exc)

    async def _sim_feed(self) -> None:
        """Draw the simulated dog in the interface (no robot connected)."""
        sim = getattr(self.stage, "sim", None)
        if sim is None or self.sim_robot is None:
            return
        try:
            while True:
                try:
                    sim(self.sim_robot.snapshot())
                except Exception as exc:
                    log.debug("sim view failed: %s", exc)
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            pass

    def _clear_live(self) -> None:
        if self.live_always:
            self._live_at = {"laptop": -1e9, "robot": -1e9}  # hand the views back to the always-on cameras
            return
        live = self._live
        if live is not None:
            for feed in ("laptop", "robot"):
                try:
                    live(None, [], feed=feed)
                except TypeError:
                    if feed == "laptop":
                        live(None, [])
                except Exception:
                    pass

    def _tick(self) -> float:
        return self.live_tick_s if self._live is not None else self.tick_s

    async def _guide_pose(self, art: ShowArtifact, camera) -> str:
        """Participant readiness. → "ready" | "declined" | "timeout". No forced timer for capture."""
        spec_ = art.interactive
        note = (f"The robot will walk to the person's {(spec_.robot_side or 'front').upper()} by itself once they "
                f"are in place — keep its path clear, remote in hand." if self.approacher is not None
                else spec_.placement())
        self._set_state(ShowState.READINESS, note)
        spec = art.interactive
        coach = PoseCoach(spec, min_gap_s=self.hint_gap_s, clock=self.clock)
        deadline = self.clock() + self.readiness_timeout_s
        manual = camera is None or self.pose_reader is None
        if manual:
            await self.say_line("pose", "manual")
        prompted = False
        while True:
            if self.clock() > deadline:
                return "timeout"
            hint = None
            if not manual:
                reading, frame = await self._read_pose(camera)
                await self._publish_live(frame, reading, checks_for(reading, posture=spec.posture,
                                                                    head_tilt=spec.head_tilt))
                if reading is not None:
                    self.last_reading = reading
                    hint = coach.next_hint(reading)
            if hint is not None:
                await self.say_line("pose", hint)
                if hint == "got_it" and not prompted:
                    prompted = True
                    await self.say_line("pose", "ready_prompt")
            # wait a tick for operator commands
            try:
                cmd = await asyncio.wait_for(self._get_cmd(), self._tick())
            except asyncio.TimeoutError:
                continue
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.PAUSE:
                await self._paused()
                deadline = self.clock() + self.readiness_timeout_s
                continue
            if cmd is Cmd.READY:
                return "ready"
            if cmd in (Cmd.DECLINE, Cmd.SKIP):
                return "declined"
            if cmd is Cmd.REPEAT:
                await self.say(spec.request, kind="narration")

    async def _capture(self, camera) -> tuple[bytes | None, PoseReading | None]:
        self._set_state(ShowState.CAPTURE)
        if self._robot_camera:
            self._note("no photo: the pose camera is on the robot (it cannot photograph the two of you)")
            return None, None
        await self.say_line("consent", "capture")
        reading, frame = await self._read_pose(camera)
        self._clear_live()
        if frame is None and camera is not None:
            try:
                frame = await asyncio.to_thread(camera.read)
            except Exception:
                frame = None
        if frame is not None:
            from io import BytesIO

            from PIL import Image

            buf = BytesIO()
            img = Image.fromarray(frame.image)
            if self._mirror(frame):  # the photo looks exactly like the live view did
                img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            img.save(buf, format="JPEG", quality=92)
            self._capture_source = frame.source
            return buf.getvalue(), reading
        if not self.photo_upload_allowed:
            return None, None
        # camera failed: the operator may send the (consented) photo through Telegram
        self._set_state(ShowState.AWAITING_PHOTO)
        self._photo, self._photo_event = None, asyncio.Event()
        waiter = asyncio.ensure_future(self._photo_event.wait())
        cmd = await self._race(waiter, {Cmd.CONSENT_NO, Cmd.SKIP})
        if cmd is Cmd.PAUSE:
            await self._paused()
        if cmd is Cmd.CANCEL:
            raise ShowCancelled()
        return self._photo, None

    async def _interactive(self) -> str:
        outcome = await self._interactive_inner()
        try:
            await self._walk_home()
        except ShowCancelled:
            raise
        except Exception as exc:
            log.warning("walking home failed: %s", exc)
        return outcome

    async def _interactive_inner(self) -> str:
        if self.finale == "encounter":
            return await self._encounter_finale()
        return await self._imitation_finale()

    async def _imitation_finale(self) -> str:
        """(Earlier finale, kept as an option.) The robot shows a chosen picture; the person copies it →
        [walk + dog pose] → consent → capture → composition → confirmed delivery."""
        art = self.artifact
        if art is None:
            log.warning("no interactive artwork available; the story continues without a photo")
            return "alone"
        camera = self._open_camera()
        try:
            self._show("chosen")
            await self.say_line("invite", "found_like")
            await self.say(art.interactive.request, kind="narration")
            await self._robot_takes_its_place(art)
            ready = await self._guide_pose(art, camera)
            if ready != "ready":
                self._clear_live()
                await self.say_line("decline", "ok")
                return "alone"
            if self.approacher is not None:  # the person is in place: now the robot comes to them
                self._clear_live()
                if await self._walk_to_person(art):
                    ready = await self._guide_pose(art, camera)  # a last adjustment, side by side
                    if ready != "ready":
                        self._clear_live()
                        await self.say_line("decline", "ok")
                        return "alone"
            self._set_state(ShowState.CONSENT)
            await self.say_line("consent", "ask")
            await self.say_line("consent", "explain")
            cmd = await self._await_cmd({Cmd.CONSENT_YES, Cmd.CONSENT_NO, Cmd.DECLINE, Cmd.SKIP},
                                        timeout=self.consent_timeout_s)
            if cmd is not Cmd.CONSENT_YES:
                self._clear_live()
                await self.say_line("consent", "declined")
                return "alone"
            await self.say_line("consent", "thanks")
            photo, reading = await self._capture(camera)
            if not photo:
                await self.say_line("consent", "failed")
                return "alone"
            self._set_state(ShowState.COMPOSITION)
            path = await self._compose(art, photo, reading)
            self.composition = path
            self.stage.show(View("composition", image=path))
            for key in self.comparison.line_keys:
                await self.say_line("compare", key)
            await self._deliver(path)
            return "together"
        except ShowCancelled:
            raise
        except Exception as exc:  # a failed photo never prevents the ending
            log.exception("interactive part failed: %s", exc)
            return "together" if self.composition else "alone"

    # ------------------------------------------------------------------ the good boy encounter (finale)
    def _note(self, text: str) -> None:
        """Something the operator should know (terminal / operator page); never spoken, never sent."""
        self.encounter_log.append(text)
        log.info("encounter: %s", text)
        self.stage.status(self.state.value, text)

    async def _wait_for_steady_pose(self, camera) -> PoseReading | None:
        """The encounter starts by itself when the person holds a pose steadily (or the operator presses
        Ready). → the reading to match, or None (declined / nobody / no camera)."""
        from .encounter import StabilityTracker
        from .live_view import Check

        self._set_state(ShowState.READINESS, f"waiting for a steady pose ({self.stable_s:.0f} s) — starts by "
                                             f"itself; Ready starts now")
        if camera is None or self.pose_reader is None:
            self._note("no camera / pose reading: the encounter cannot match a picture — skipped")
            return None
        # the person is a photo (also when the dog's own camera reads it): always "steady", so Ready decides
        still = getattr(camera, "is_still", False) or getattr(self._open_camera(), "is_still", False)
        if still:
            self._set_state(ShowState.READINESS, "a photo stands in for the person — press ✓ Ready to go on")
        tracker = StabilityTracker(self.stable_s)
        deadline = self.clock() + self.readiness_timeout_s
        last_hint = -1e9
        latest: PoseReading | None = None
        while self.clock() < deadline:
            reading, frame = await self._read_pose(camera)
            now = self.clock()
            steady = tracker.update(reading, now)
            if reading is not None:
                latest = reading
                self.last_reading = reading
            checks = checks_for(reading) + [Check("holding still", steady or tracker.progress(now) >= 1.0)]
            await self._publish_live(frame, reading, checks)
            if steady and not still:  # a photo is always "steady": with one, Ready decides
                return reading
            hint = None
            if reading is None or not reading.person_visible:
                hint = "not_visible"
            elif reading.framing in ("partial", "too_close"):
                hint = "partly_visible" if reading.framing == "partial" else "too_close"
            elif reading.side != "center":
                hint = reading.side
            if hint and now - last_hint >= max(self.hint_gap_s, 0.0) * 1.5:
                last_hint = now
                await self._with_live(camera, lambda r: checks_for(r), self.say_line("pose", hint))
            try:
                cmd = await asyncio.wait_for(self._get_cmd(), self._tick())
            except asyncio.TimeoutError:
                continue
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.PAUSE:
                await self._paused()
                deadline = self.clock() + self.readiness_timeout_s
                continue
            if cmd is Cmd.REPEAT:
                await self._with_live(camera, lambda r: checks_for(r), self._say_again())
                continue
            if cmd is Cmd.READY:
                if latest is not None and latest.person_visible and latest.observation is not None:
                    return latest
                self._note("Ready pressed, but nobody is readable in the camera yet")
            if cmd in (Cmd.DECLINE, Cmd.SKIP):
                return None
        self._note("nobody held a steady pose — encounter skipped")
        return None

    def _match(self, reading: PoseReading):
        """Existing matcher, local only. Only images we may send are candidates. → (scene, artifact) | None"""
        from ..matching.matcher import SceneMatcher
        from ..scenes import SceneLibrary
        from .encounter import deliverable

        if reading.observation is None:
            return None
        if self.matcher is None:
            self.matcher = SceneMatcher(SceneLibrary(self.catalog.art_repo, self.catalog.meme_repo))
        exclude = set()
        if self.catalog.meme_repo is not None:
            exclude |= {m.id for m in self.catalog.meme_repo.all() if not self.catalog.meme_repo.local_image(m)}
        if self.catalog.art_repo is not None:
            exclude |= {a.id for a in self.catalog.art_repo.all() if not self.catalog.art_repo.local_image(a)}
        res = self.matcher.match(reading.observation, "both", exclude=exclude, recent=self._recent, rng=self.rng)
        if not res.found:
            self._note(f"no suitable picture: {res.reason}")
            return None
        scene = res.chosen.scene
        art = self.catalog.artifact_for_scene(scene.id)
        if not deliverable(art):
            self._note(f"{scene.id} matched but its image may not be sent — skipped")
            return None
        self._recent.add(scene.id)
        self._note(f"matched {scene.id} (score {res.chosen.scores.total:.2f}) — {res.reason}")
        return scene, art

    async def _send_archive_image(self, art: ShowArtifact) -> None:
        """One Telegram message: the archive image, one short caption, italic attribution. Never the
        visitor's photo. Sent automatically; a failure is reported to the operator only."""
        from .encounter import telegram_message

        if self.delivery is None:
            self.delivery_detail = "no Telegram delivery configured"
            self._note(self.delivery_detail)
            return
        self._set_state(ShowState.SENDING)
        text = telegram_message(art, self.lines.captions(), self.rng)
        res = await self._deliver_one(art.image_path, text, "archive image")
        self.delivered, self.delivery_detail = res.ok, res.detail

    async def _deliver_one(self, image: Path, caption: str, what: str):
        """One Telegram image message, bounded in time; the outcome goes to the operator only."""
        from .delivery import DeliveryResult

        try:
            res = await asyncio.wait_for(self.delivery.send(image, caption, self.session_id), 45)
        except asyncio.TimeoutError:
            res = DeliveryResult(False, "timed out")
        except Exception as exc:
            res = DeliveryResult(False, f"error: {type(exc).__name__}")
        self._note(f"Telegram ({what}): {res.detail}")
        return res

    def _behavior_for(self, scene, art: ShowArtifact | None) -> str | None:
        """A supported pose based on the selected image (planner fallbacks apply)."""
        planner = getattr(self.motion, "planner", None) if self.motion is not None else None
        if planner is not None and getattr(scene, "dog", None) is not None:
            return planner.plan(scene).behavior.value
        dog = getattr(scene, "dog", None)
        if dog is not None:
            return getattr(dog.robot_behavior, "value", dog.robot_behavior)
        if art is not None and art.interactive and art.interactive.robot_behavior:
            return art.interactive.robot_behavior
        return None

    async def _take_final_pose(self, scene, art: ShowArtifact, reading: PoseReading) -> bool:
        """Walk to the person (with --walk) and take the image's dog pose. → True only if the robot
        reports reaching it (command acceptance is not enough)."""
        from ..domain import ExecutionStatus
        from .encounter import side_for

        behavior = self._behavior_for(scene, art)
        if self.approacher is not None:
            arrived = await self._walk_to_person_at(side_for(scene, art), reading.posture or "sitting")
            if not arrived:
                self._note("could not reach the person — final pose skipped")
                return False
        if not behavior or behavior == "stillness":
            self._note("no supported pose for this picture — the robot stays still")
            return False
        if self.motion is None or not getattr(self.motion, "enabled", False):
            self._note(f"final pose '{behavior}' not performed (show motion off)")
            return False
        res = await asyncio.to_thread(self.motion.perform, behavior)
        ok = res is not None and res.status in (ExecutionStatus.SUCCESS, ExecutionStatus.SIMULATED)
        self._note(f"final pose '{behavior}': {res.status.value if res else 'none'} {res.detail if res else ''}")
        return ok

    async def _snap(self, camera) -> tuple[bytes | None, PoseReading | None]:
        """One consented frame, mirrored like the live view. Kept local."""
        reading, frame = await self._read_pose(camera)
        if frame is None:
            return None, None
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        img = Image.fromarray(frame.image)
        if self._mirror(frame):
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        img.save(buf, format="JPEG", quality=92)
        self._capture_source = frame.source
        return buf.getvalue(), reading

    async def _hold_final_pose(self, settled: bool, camera, consent: bool):
        """The good boy moment: hold for final_hold_s AFTER settling; interruptible (Skip ends it, Stop
        cancels). A consented photo is taken during the hold. → (photo, reading)"""
        return await self._hold_pose(settled, camera, consent, self.final_hold_s,
                                     ("encounter", "goodboy"), ("encounter", "goodboy_after"))

    async def _hold_pose(self, settled: bool, camera, consent: bool, hold_s: float, before, after,
                         unsettled_s: float = 0.0):
        from .live_view import checks_for as _checks

        self._set_state(ShowState.FINAL_POSE, f"holding the pose for {hold_s:.0f} s" if settled
                        else "pose not performed — no hold")
        photo, photo_reading = None, None
        if before:
            await self.say_line(*before)
        start = self.clock()
        duration = hold_s if settled else unsettled_s
        snap_at = start + min(duration * 0.4, 4.0)
        while True:
            now = self.clock()
            if consent and photo is None and now >= snap_at:
                photo, photo_reading = await self._snap(camera)
                if photo is None:
                    self._note("photo failed (no camera frame)")
                    consent = False
            if now - start >= duration and (photo is not None or not consent):
                break
            if camera is not None:
                reading, frame = await self._read_pose(camera)
                await self._publish_live(frame, reading, _checks(reading, any_person=True))
            try:
                cmd = await asyncio.wait_for(self._get_cmd(), self._tick())
            except asyncio.TimeoutError:
                continue
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.PAUSE:
                paused_at = self.clock()
                await self._paused()
                start += self.clock() - paused_at  # the hold resumes where it was
                snap_at += self.clock() - paused_at
                self._set_state(ShowState.FINAL_POSE)
                continue
            if cmd is Cmd.SKIP:
                self._note("hold ended early by the operator")
                break
        self._clear_live()
        if settled:
            self._note(f"held the pose for {min(self.clock() - start, hold_s):.1f} s")
        if after:
            await self.say_line(*after)
        return photo, photo_reading

    async def _encounter_finale(self) -> str:
        """The good boy encounter (docs/UX.md) as the show's finale:
        steady pose (automatic) → local match → short line → Telegram image → [consent?] →
        [walk] + the image's dog pose → hold 10 s → [local THEN/NOW]."""
        camera = self._open_camera()
        try:
            reading = await self._wait_for_steady_pose(camera)
            if reading is None:
                self._clear_live()
                await self.say_line("decline", "ok")
                return "alone"
            matched = await asyncio.to_thread(self._match, reading)
            if matched is None:
                self._clear_live()
                return "alone"  # no invented match: the operator sees why
            scene, art = matched
            self.artifact = art
            self._clear_live()
            self.stage.show(View("artifact", artifact=art))
            await self.say_line("encounter", "found")
            await self._send_archive_image(art)
            consent = False
            if self.photo_in_encounter and camera is not None and not self._robot_camera:
                self._set_state(ShowState.CONSENT, "photo stays on the local screen only")
                await self.say_line("consent", "ask")
                await self.say_line("consent", "explain_local")
                cmd = await self._await_cmd({Cmd.CONSENT_YES, Cmd.CONSENT_NO, Cmd.DECLINE, Cmd.SKIP},
                                            timeout=self.consent_timeout_s)
                consent = cmd is Cmd.CONSENT_YES
                await self.say_line("consent", "thanks" if consent else "declined")
            settled = await self._take_final_pose(scene, art, reading)
            photo, photo_reading = await self._hold_final_pose(settled, camera, consent)
            if photo:
                self._set_state(ShowState.COMPOSITION)
                path = await self._compose(art, photo, photo_reading)
                self.composition = path
                self.stage.show(View("composition", image=path))
                return "together"
            return "alone"
        except ShowCancelled:
            raise
        except Exception as exc:  # never prevents the ending
            log.exception("encounter failed: %s", exc)
            self._note(f"encounter error: {exc}")
            return "together" if self.composition else "alone"

    async def _walk_to_person_at(self, side: str, posture: str, speak: bool = True, facing: str = "") -> bool:
        self._set_state(ShowState.APPROACH, f"walking to the person's {side}")
        if self.sim_robot is not None:  # the simulated person takes the pose the camera read
            self.sim_robot.set_person_posture(posture)
        if speak:
            await self.say_line("approach", "ask")
        if self.motion is not None:
            await asyncio.to_thread(self.motion.perform, "stand")  # walking starts from standing
        res, cmd = await self._walk(self.approacher.approach, side=side, posture=posture, facing=facing)
        self._walk_cmd = cmd
        self.walk_log.append(f"approach: {getattr(res, 'status', 'interrupted')} {getattr(res, 'detail', '')}".strip())
        if cmd is Cmd.CANCEL:
            raise ShowCancelled()
        if cmd is Cmd.PAUSE:
            await self._paused()
        arrived = res is not None and res.arrived
        if speak:
            await self.say_line("approach", "arrived" if arrived else "not_found")
        return arrived

    async def _robot_takes_its_place(self, art: ShowArtifact) -> None:
        """The robot goes into the painted dog's pose BEFORE the person settles next to it.

        It never walks: the operator has placed it at the spot named in the catalog (shown to the
        operator in the readiness prompt). The pose goes through the existing MotionGuard."""
        spec = art.interactive
        placement = spec.placement()
        if placement:
            log.info("operator: %s", placement)
        if self.approacher is not None:
            return  # it will walk to the person once they are in place, and take the pose there
        if spec.robot_behavior and self.motion is not None and getattr(self.motion, "enabled", False):
            self.motion.request(spec.robot_behavior)
            await self.say_line("invite", "robot_pose")

    async def _walk(self, fn, **kw):
        """Run a walking routine in a thread; PAUSE / SKIP / CANCEL stop it at once. → (result, cmd)"""
        import threading

        stop = threading.Event()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, lambda: fn(should_stop=stop.is_set, **kw))
        waiter = asyncio.ensure_future(asyncio.shield(fut))
        cmd = await self._race(waiter, {Cmd.SKIP})
        if cmd is not None:
            stop.set()
        try:
            result = await asyncio.wait_for(asyncio.shield(fut), 5)
        except asyncio.TimeoutError:
            result = None
        return result, cmd

    async def _walk_to_person(self, art: ShowArtifact) -> bool:
        """The robot finds the participant with its own camera, walks to the painted dog's side of
        them, and takes the dog's pose there. → arrived?"""
        spec = art.interactive
        self._set_state(ShowState.APPROACH, f"walking to the person's {spec.robot_side or 'front'}")
        await self.say_line("approach", "ask")
        if self.motion is not None:
            await asyncio.to_thread(self.motion.perform, "stand")  # walking starts from standing
        res, cmd = await self._walk(self.approacher.approach, side=spec.robot_side or "front", posture=spec.posture)
        self.walk_log.append(f"approach: {getattr(res, 'status', 'interrupted')} {getattr(res, 'detail', '')}".strip())
        if cmd is Cmd.CANCEL:
            raise ShowCancelled()
        if cmd is Cmd.PAUSE:
            await self._paused()  # the robot stays where it stopped; the operator decides
        if res is not None and res.arrived:
            if spec.robot_behavior and self.motion is not None:
                await asyncio.to_thread(self.motion.perform, spec.robot_behavior)
            await self.say_line("approach", "arrived")
            return True
        await self.say_line("approach", "not_found")
        return False

    async def _walk_home(self, speak: bool = True) -> None:
        if self.approacher is None or self.approacher.memory is None or not self.approacher.memory.path:
            return
        self._set_state(ShowState.APPROACH, "walking back")
        if speak:
            await self.say_line("approach", "home")
        if self.motion is not None:
            await asyncio.to_thread(self.motion.perform, "stand")  # stand up before walking
        res, cmd = await self._walk(self.approacher.go_home)
        self.walk_log.append(f"home: {res or 'interrupted'}")
        if cmd is Cmd.CANCEL:
            raise ShowCancelled()
        if cmd is Cmd.PAUSE:
            await self._paused()

    async def _compose(self, art: ShowArtifact, photo: bytes, reading: PoseReading | None,
                       name: str = "is-this-us.jpg") -> Path:
        from io import BytesIO

        from PIL import Image

        from .composer import compose

        now_img = Image.open(BytesIO(photo)).convert("RGB")
        if reading is None and self.pose_reader is not None:
            try:
                import numpy as np

                reading = await asyncio.to_thread(self.pose_reader.read, np.asarray(now_img), "photo")
            except Exception:
                reading = None
        self.last_reading = reading or self.last_reading
        painted = None
        if self.pose_reader is not None:
            painted = await asyncio.to_thread(read_painted_figure, art, self.pose_reader.estimator,
                                              self.pose_reader.interpreter)
        if art.interactive is None:  # a matched picture: compare with how the curators describe its person
            from dataclasses import replace

            from .catalog import InteractiveSpec

            scene = self.catalog.art_repo.get(art.id) if self.catalog.art_repo is not None else None
            hp = str(getattr(getattr(getattr(scene, "human", None), "posture", None), "value", "") or "")
            art = replace(art, interactive=InteractiveSpec(pose_subject="human", safe_to_imitate=True, request="",
                                                           posture=hp if hp in ("sitting", "standing") else ""))
        self.comparison = compare(art, reading, painted)
        then_img = Image.open(art.image_path).convert("RGB")
        commentary = self.lines.text("compare", self.comparison.line_keys[-1])
        data = await asyncio.to_thread(compose, then_img, now_img, art, self.comparison, commentary=commentary,
                                       theme=self.theme, font_path=self.font_path, session_id=self.session_id,
                                       now_note=("Rehearsal stand-in — not a photograph" if self._capture_source == "synthetic"
                                                 else "Photograph taken with consent"))
        self.store.save(self.session_id, "now.jpg" if name == "is-this-us.jpg" else f"now-{name}", photo)
        return self.store.save(self.session_id, name, data)

    async def _deliver(self, path: Path) -> None:
        if self.delivery is None:
            self.delivery_detail = "no delivery configured"
            return
        self._set_state(ShowState.DELIVERY)
        await self.say_line("delivery", "ask_send")
        cmd = await self._await_cmd({Cmd.SEND_YES, Cmd.SEND_NO, Cmd.SKIP}, timeout=self.send_timeout_s)
        if cmd is not Cmd.SEND_YES:
            self.delivery_detail = "not sent (no confirmation)"
            await self.say_line("delivery", "not_sent")
            return
        try:
            res = await asyncio.wait_for(self.delivery.send(path, self.lines.text("delivery", "caption"),
                                                            self.session_id), 45)
        except asyncio.TimeoutError:
            from .delivery import DeliveryResult

            res = DeliveryResult(False, "timed out")
        self.delivered, self.delivery_detail = res.ok, res.detail
        await self.say_line("delivery", "sent" if res.ok else "failed")

    # ------------------------------------------------------------------ the presentation
    # intro (@consent) → one @round per era → @rescue finale. See data/performance/presentation.en.md.
    async def _speak_plain(self, text: str, direction: str = "") -> None:
        """Speak while ANOTHER task listens for commands (the walk). Never touches the command inbox."""
        if not text:
            return
        self.stage.subtitle(text)
        try:
            await self.voice.speak(text, kind="lines", direction=direction)
        finally:
            self.stage.subtitle(None)

    async def _lobby(self) -> None:
        """Nothing plays and no camera opens until the operator starts — choosing how photos are taken."""
        self._close_camera()  # a new start: back to the real camera (the photos are chosen again here)
        self.frame_source_factory, self.pose_reader = self._real_camera
        self._camera, self._camera_failed = None, False
        self._set_state(ShowState.LOBBY, "waiting for Start — nothing plays, no camera until then")
        self.stage.show(View("title"))
        while True:
            cmd = await self._await_cmd({Cmd.CONSENT_YES, Cmd.SAMPLE})
            if cmd is not Cmd.SAMPLE:
                break
            pic = self.photo_factory(None) if self.photo_factory else None
            if pic is not None:
                self._camera = None
                if getattr(pic, "pictures", 0) > 1:  # sample photos: a different one opens each show
                    pic.select(self.rng.randrange(pic.pictures))
                    self._shown_pictures = {pic._i}
                self.frame_source_factory = lambda: pic  # noqa: E731
                self.pose_reader = PoseReader(pic)
                break
            self._note("no sample photos available — choose another start")
        self.photo_consent, self._consent_decided = cmd is not Cmd.CONSENT_NO, True
        if self.photo_consent and self.delivery is not None:
            self._set_state(ShowState.DELIVERY, "send this presentation's photos to the Telegram chat too? "
                                                "(otherwise: big screen only)")
            self.send_ok = await self._await_cmd({Cmd.SEND_YES, Cmd.SEND_NO, Cmd.SKIP},
                                                 timeout=self.send_timeout_s) is Cmd.SEND_YES
        source = "the sample photos" if cmd is Cmd.SAMPLE else "the camera"
        self._show_source("photos" if cmd is Cmd.SAMPLE else "camera")
        self._note(f"start: {'photos from ' + source if self.photo_consent else 'no photos, no camera'}")

    async def _consent_cue(self, ch: Chapter, cue: Cue) -> str:
        """Asked once, in the intro. Consent to photos (for the visitor), then — separately — whether the
        operator allows sending them to the Telegram chat. Without consent no photo is ever taken."""
        if self._consent_decided:  # already answered at the start
            return "done"
        self.photo_consent, self.send_ok = False, False
        if not self.photos_possible:  # no laptop camera (or only the robot's): no photos, so nothing to ask
            self._note("no photos in this presentation — " + ("the pose camera is on the robot" if self._robot_camera
                                                              else "no camera"))
            return "done"
        telegram = self.delivery is not None
        self._set_state(ShowState.CONSENT, "photos during the presentation: did the visitor agree?"
                        + ("" if telegram else " (local display only — no Telegram configured)"))
        text = cue.render(where=self.lines.text("consent_where", "telegram" if telegram else "screen"))
        await self._play_cue(ch, cue, text)
        cmd = await self._await_cmd({Cmd.CONSENT_YES, Cmd.CONSENT_NO, Cmd.DECLINE, Cmd.SKIP},
                                    timeout=self.consent_timeout_s)
        self.photo_consent = cmd is Cmd.CONSENT_YES
        self.send_ok = False
        if self.photo_consent and self.delivery is not None:
            self._set_state(ShowState.DELIVERY, "send this presentation's photos to the Telegram chat too? "
                                                "(otherwise: big screen only)")
            sent = await self._await_cmd({Cmd.SEND_YES, Cmd.SEND_NO, Cmd.SKIP}, timeout=self.send_timeout_s)
            self.send_ok = sent is Cmd.SEND_YES
        self._note(f"photos: {'yes' if self.photo_consent else 'no'}; "
                   + (f"sending to Telegram: {'confirmed' if self.send_ok else 'no'}" if telegram
                      else "local display only"))
        await self.say_line("presentation_consent", "thanks" if self.photo_consent else "declined")
        return "done"

    async def _fit_picture(self, camera, era: str) -> None:
        """Sample stand-ins: put the person whose pose fits this era's pictures best in front of the camera
        (ones not shown yet first), so the round's match makes sense."""
        from .rounds import match_score

        if getattr(camera, "pictures", 0) < 2 or self.pose_reader is None:
            return
        used = self._shown_pictures

        def fit(i: int) -> float:
            camera.select(i)
            frame = camera.read()
            posture = self.pose_reader.read(frame.image, frame.source).posture
            return match_score(self.catalog, era, posture) - (100.0 if i in used else 0.0)  # never twice
        if len(used) >= camera.pictures:  # all shown: start over
            used.clear()
        scores = [await asyncio.to_thread(fit, i) for i in range(camera.pictures)]
        top = max(scores)
        best = self.rng.choice([i for i, sc in enumerate(scores) if sc == top])  # ties: not always the same one
        camera.select(best)
        used.add(best)

    async def _round(self, ch: Chapter, cue: Cue) -> str:
        """One era: any pose → the closest picture of this era → the robot remembers while it walks over and
        takes the painted dog's pose → the picture → hold + photo → beamer + Telegram → back to its place."""
        from .live_view import checks_for as _checks
        from .rounds import (MEME_BEHAVIOR, behavior_for, candidates, facing_for, pick_for_round, pose_group,
                             side_for)

        era = cue.target
        camera = self._open_camera()  # the laptop camera: the photo of the two together
        await self._fit_picture(camera, era)
        collage = [a for a in candidates(self.catalog, era) if a.displayable]
        if collage:  # the era's pictures stay up while the person poses (not the last narration picture)
            self.stage.show(View("consider", memes=collage, heading=ch.title))
        eyes = self._pose_camera()  # the dog's own camera reads the pose (laptop camera if it has none)
        looking = eyes is not None and self.pose_reader is not None
        start = ("the sample photo is the pose — the dog goes to it" if getattr(camera, "is_still", False)
                 else "waiting for a steady pose — starts by itself; Ready = now")
        self._set_state(ShowState.READINESS, f"{era}: {start}, Skip = skip this round")
        said = "done"

        async def invite():
            nonlocal said
            said = await self._play_cue(ch, cue, cue.text)

        still = self.pose_reader is not None and getattr(camera, "is_still", False)
        if still:  # a sample photo IS the pose (read from the photo itself, also with --sim-robot)
            reading = None
            for i in range(max(1, getattr(camera, "pictures", 1))):  # never "nobody": another sample if unreadable
                if i:
                    camera.select(i)
                r, frame = await self._read_pose(camera)
                await self._publish_live(frame, r, _checks(r, any_person=True))
                if r is not None and r.person_visible:
                    reading = r
                    break
        else:
            if looking:
                await self._with_live(eyes, lambda r: _checks(r, any_person=True), invite())
            else:
                await invite()
            if said == "skip":
                self._clear_live()
                return "done"
            reading = await self._wait_for_steady_pose(eyes) if looking else None
        if reading is None:
            self._clear_live()
            await self.say_line("round", "nobody")
        posture = reading.posture if reading else None
        gestures = ({str(getattr(g, "value", g)) for g, v in reading.observation.gestures.items() if v >= 0.5}
                    if reading is not None and reading.observation is not None else set())
        art = None
        if era != "internet":
            pick = pick_for_round(self.catalog, era, posture, head_tilt=bool(reading and reading.head_tilt),
                                  gestures=gestures, exclude=self._recent, rng=self.rng)
            if pick is not None:
                art = pick.artifact
                self._recent.add(art.id)
                self._note(f"{era}: picked {art.id} ({pick.why})")
            else:
                self._note(f"{era}: no picture with a local image in this era")
        self.artifact = art or self.artifact
        repo = self.catalog.art_repo
        behavior = behavior_for(art, repo) if art is not None else MEME_BEHAVIOR.get(pose_group(posture), "sit")
        side = side_for(art, repo)
        record = {"era": era, "artifact": art.id if art else None, "posture": pose_group(posture) or None,
                  "behavior": behavior, "side": side, "settled": False, "photo": False, "sent": []}
        self.rounds.append(record)
        self._clear_live()
        await self._remember_while(era, art, None)
        # the picture first, THEN the dog goes over and takes the painted dog's pose (it stays on screen meanwhile)
        if art is not None:
            self.stage.show(View("artifact", artifact=art))
            await self.say_line("round", "found")
        elif self.photo_consent and reading is not None:
            await self.say_line("round", "meme_found")
        settled = False
        if reading is not None:
            settled = await self._round_robot(behavior, side, posture, facing_for(art))
        record["settled"] = settled
        consent = (self.photo_consent and reading is not None and camera is not None and self.pose_reader is not None
                   and not self._robot_camera)
        photo, photo_reading = None, None
        if reading is not None:
            photo, photo_reading = await self._hold_pose(settled, camera, consent, self.round_hold_s,
                                                         ("round", "hold"), None,
                                                         unsettled_s=min(2.0, self.round_hold_s))
        path, kind = None, None
        if photo:
            record["photo"] = True
            self._set_state(ShowState.COMPOSITION)
            try:
                if art is not None:
                    path, kind = await self._compose(art, photo, photo_reading, name=f"{era}.jpg"), "together"
                else:
                    path, kind = await self._make_meme(photo, posture), "meme"
                self.composition = path
                self.stage.show(View("composition", image=path))
            except Exception as exc:  # a failed picture never stops the presentation
                log.exception("round picture failed: %s", exc)
                self._note(f"{era}: picture failed ({type(exc).__name__})")
                path = None
        if path is not None:
            await self._admire()
        elif art is not None:  # no photo: the found picture still stays until the operator moves on
            await self._wait_next("the picture is on the screen — press Next to continue")
        home = asyncio.ensure_future(self._walk_home(speak=False)) if reading is not None else None
        try:
            if path is None and era == "internet" and reading is not None:
                self.stage.show(View("memes", memes=self.catalog.for_chapter("internet")))
                await self._speak_plain(self.lines.pick("round", "meme_no_photo"))
            sent = await self._send_round(art, path, kind)
            record["sent"] = sent
            if path is not None and kind in sent:
                await self._speak_plain(self.lines.pick("round", "sent"))
        finally:
            if home is not None:
                await home
        return "done"

    async def _admire(self) -> None:
        """The photo stays on screen: the dog says something happy about it, then waits for the operator's Next
        (so everyone can really look at it)."""
        await self.say(self.lines.pick("round", "admire"))
        await self._wait_next("the photo is on the screen — press Next to continue")

    async def _wait_next(self, why: str) -> None:
        self._set_state(ShowState.ARTWORK, why)
        while (cmd := await self._await_cmd({Cmd.NEXT, Cmd.SKIP, Cmd.REPEAT},
                                            timeout=self.photo_view_timeout_s)) is Cmd.REPEAT:
            await self._say_again()
        self._note("picture viewed — continuing" if cmd else "picture viewed (time limit) — continuing")

    async def _round_robot(self, behavior: str, side: str, posture: str | None, facing: str = "") -> bool:
        """Walk to the person (only with --walk) and take the pose. → True only if the robot reports it."""
        from ..domain import ExecutionStatus

        if self.approacher is not None:
            # nothing moves on until the dog is at its spot: a paused or failed walk is walked again;
            # only the operator's Skip goes on without it
            tries = 0
            while not await self._walk_to_person_at(side, posture or "sitting", speak=False, facing=facing):
                if self._walk_cmd is Cmd.SKIP:
                    self._note("walk skipped by the operator — pose skipped")
                    return False
                tries += 1
                if self._walk_cmd is not Cmd.PAUSE and tries >= 2:
                    self._set_state(ShowState.READINESS, "the dog has not reached its spot — Ready = walk again, "
                                                         "Skip = go on without it")
                    if await self._await_cmd({Cmd.READY, Cmd.SKIP}) is not Cmd.READY:
                        self._note("could not reach the person — pose skipped")
                        return False
                self._note("the dog has not reached its spot yet — walking again")
        if self.motion is None or not getattr(self.motion, "enabled", False):
            self._note(f"pose '{behavior}' not performed (show motion off)")
            return False
        res = await asyncio.to_thread(self.motion.perform, behavior)
        ok = res is not None and res.status in (ExecutionStatus.SUCCESS, ExecutionStatus.SIMULATED)
        self._note(f"pose '{behavior}': {res.status.value if res else 'none'}"
                   f"{' [' + res.diagnostic + ']' if getattr(res, 'diagnostic', '') else ''} "
                   f"{res.detail if res else ''}".strip())
        return ok

    async def _remember_while(self, era: str, chosen: ShowArtifact | None, work) -> bool:
        """While the robot is busy (`work`), the era's other pictures flash by — at least a few, then as long
        as the robot needs. One short spoken line at the start; the found picture is the reveal (after)."""
        from .rounds import montage_order

        self._set_state(ShowState.REMEMBERING, "remembering — then the picture, then the dog walks over")
        flashes = montage_order(self.catalog, era, chosen)[:7]
        done = asyncio.Event()
        minimum = min(2, len(flashes)) if flashes else 1
        # the era's pictures stay on screen together; the dog's attention moves slowly from one to the next
        grid = list(flashes)
        if chosen is not None and chosen.displayable:
            grid.insert(self.rng.randrange(len(grid) + 1), chosen)  # it was there all along

        async def montage():
            line = asyncio.ensure_future(self._speak_plain(self.lines.pick("round", "thinking")))
            shown = 0
            focus = getattr(self.stage, "focus", None)
            try:
                if not flashes and era == "internet":
                    self.stage.show(View("memes", memes=self.catalog.for_chapter("internet")))
                elif grid:
                    self.stage.show(View("consider", memes=grid, heading="remembering…"))
                while True:
                    if self.state is not ShowState.PAUSED and flashes:
                        a = flashes[shown % len(flashes)]
                        if focus is not None:
                            focus(a.id)
                        else:
                            self.stage.show(View("flash", artifact=a))
                    if self.state is not ShowState.PAUSED:
                        shown += 1
                    await asyncio.sleep(self.flash_s)
                    if done.is_set() and shown >= minimum:
                        break
                await line
            finally:
                if not line.done():
                    line.cancel()
                    await asyncio.gather(line, return_exceptions=True)

        show = asyncio.ensure_future(montage())
        settled = False
        try:
            if work is not None:
                settled = await work
        except BaseException:
            show.cancel()
            await asyncio.gather(show, return_exceptions=True)
            raise
        done.set()
        await show
        return bool(settled)

    async def _make_meme(self, photo: bytes, posture: str | None) -> Path:
        from .rounds import make_meme, meme_phrases, pose_group

        phrases = meme_phrases(self.lines, pose_group(posture) or "any", self.rng)
        data = await asyncio.to_thread(make_meme, photo, phrases, font_path="", seed=self.rng.randrange(1 << 30))
        return self.store.save(self.session_id, "internet.jpg", data)

    async def _send_round(self, art: ShowArtifact | None, path: Path | None, kind: str | None) -> list[str]:
        """Telegram: the archive picture (caption + italic credit), then — only with consent AND the operator's
        separate OK — the photo the robot made with the participant. → what was sent."""
        from .encounter import deliverable, telegram_message
        from .rounds import photo_caption

        sent: list[str] = []
        if self.delivery is None:  # Telegram is optional: not configured = local display, not an error
            return sent
        if art is not None or (path is not None and self.send_ok):
            self._set_state(ShowState.SENDING)
        if art is not None and deliverable(art):
            res = await self._deliver_one(art.image_path, telegram_message(art, self.lines.captions(), self.rng),
                                          "archive picture")
            if res.ok:
                sent.append("painting")
        if path is not None and kind is not None:
            if self.send_ok:
                caption = photo_caption(self.lines.caption(kind), {"together": "photo"}.get(kind, kind))
                res = await self._deliver_one(path, caption, f"{kind} photo")
                if res.ok:
                    sent.append(kind)
                self.delivered = self.delivered or res.ok
            else:
                self._note(f"{kind} photo: big screen only (sending to Telegram not confirmed)")
        return sent

    async def _wait_for_posture(self, camera, targets: set[str], timeout: float, label: str) -> bool:
        """Until the camera sees one of `targets` held for a moment, or the operator presses Ready.
        → False on Skip/Decline/timeout."""
        from .live_view import Check

        deadline = self.clock() + timeout
        held_since = None
        looking = camera is not None and self.pose_reader is not None
        while self.clock() < deadline:
            if looking:
                reading, frame = await self._read_pose(camera)
                ok = bool(reading and reading.person_visible and reading.posture in targets)
                await self._publish_live(frame, reading, [Check(label, ok)])
                if ok:
                    held_since = held_since if held_since is not None else self.clock()
                    if self.clock() - held_since >= self.moment_hold_s:
                        return True
                else:
                    held_since = None
            try:
                cmd = await asyncio.wait_for(self._get_cmd(), self._tick())
            except asyncio.TimeoutError:
                continue
            if cmd is Cmd.CANCEL:
                raise ShowCancelled()
            if cmd is Cmd.PAUSE:
                await self._paused()
                self._set_state(ShowState.RESCUE)
                deadline = self.clock() + timeout
                continue
            if cmd is Cmd.READY:
                return True
            if cmd in (Cmd.SKIP, Cmd.DECLINE):
                return False
        return False

    async def _rescue(self, ch: Chapter, cue: Cue) -> str:
        """The finale: someone lies down (as if they fainted) → the robot comes, tries to wake them → they get
        up → the robot is happy, sits beside them → a last photo (consent) → beamer + Telegram."""
        from ..domain import ExecutionStatus
        from .rounds import credit_line, make_rescue_card, photo_caption

        camera = self._open_camera()  # photo
        eyes = self._pose_camera()  # who is lying / up again
        await self._play_cue(ch, cue, cue.text)
        self._set_state(ShowState.RESCUE, "waiting for someone to lie down (fainted) — Ready = they are down, "
                                          "Skip = no rescue")
        down = await self._wait_for_posture(eyes, {"lying", "reclining"}, self.rescue_wait_s, "someone lying down")
        self._clear_live()
        if not down:
            await self.say_line("rescue", "nobody")
            return "done"
        self._set_state(ShowState.CONCLUSION, "rescue: the robot comes to help")
        await self.say_line("rescue", "alarm")
        arrived = True
        if self.approacher is not None:
            coming = asyncio.ensure_future(self._speak_plain(self.lines.pick("rescue", "coming")))
            try:
                arrived = await self._walk_to_person_at("front", "lying", speak=False)
            finally:
                await asyncio.gather(coming, return_exceptions=True)
            await self.say_line("rescue", "arrived" if arrived else "stay")
        motion_on = self.motion is not None and getattr(self.motion, "enabled", False)
        if arrived and motion_on:
            await asyncio.to_thread(self.motion.perform, "greet")  # a paw: "wake up"
        await self.say_line("rescue", "wake")
        self._set_state(ShowState.RESCUE, "waiting for them to sit or stand up — Ready = they are up")
        ups = {"sitting", "standing", "kneeling", "crouching", "leaning_forward"}
        up = await self._wait_for_posture(eyes, ups, self.rescue_wake_s, "awake again")
        if not up:
            await self.say_line("rescue", "stay")
            up = await self._wait_for_posture(eyes, ups, self.rescue_wait_s, "awake again")
        self._clear_live()
        await self.say_line("rescue", "saved")
        settled = False
        if motion_on:
            res = await asyncio.to_thread(self.motion.perform, "sit")
            settled = res is not None and res.status in (ExecutionStatus.SUCCESS, ExecutionStatus.SIMULATED)
        consent = self.photo_consent and camera is not None and self.pose_reader is not None and not self._robot_camera
        photo, _ = await self._hold_pose(settled, camera, consent, self.round_hold_s, None, None,
                                         unsettled_s=min(2.0, self.round_hold_s))
        self.rounds.append({"era": "rescue", "artifact": None, "posture": None, "behavior": "greet → sit",
                            "settled": settled, "photo": bool(photo), "sent": []})
        if photo:
            data = await asyncio.to_thread(make_rescue_card, photo, heading="good boy?",
                                           note=credit_line("rescue"), font_path=self.font_path)
            path = self.store.save(self.session_id, "rescue.jpg", data)
            self.composition = path
            self.stage.show(View("composition", image=path))
            await self._admire()
            if self.delivery is not None and self.send_ok:
                self._set_state(ShowState.SENDING)
                res = await self._deliver_one(path, photo_caption(self.lines.caption("rescue"), "rescue"),
                                              "rescue photo")
                if res.ok:
                    self.rounds[-1]["sent"] = ["rescue"]
                    self.delivered = True
        self._set_state(ShowState.CONCLUSION)
        return "done"

    # ------------------------------------------------------------------ main
    async def run(self, start_at: str | None = None) -> ShowResult:
        """start_at: a chapter id to begin with (e.g. "invitation" = straight to the interactive part)."""
        if self.running:
            raise RuntimeError("a performance is already running")
        self.session_id = secrets.token_hex(3)
        self._inbox.clear()
        self.outcome, self.artifact, self.composition, self.delivered = "alone", None, None, False
        self.delivery_detail, self.last_reading, self.comparison = "", None, None
        self._capture_source = ""
        self.transitions, self.chapters_played = [], []
        self.tries = []
        self._jump_to_pose = False
        self.photo_consent, self.send_ok, self.rounds = False, False, []
        self._consent_decided, self._shown_pictures, self._goto = False, set(), None
        self.rounds_only = False
        if self.script.is_presentation and start_at in ("invitation", "interactive"):
            self.rounds_only, start_at = True, None  # every chapter: straight to its round (consent first)
        self.state = ShowState.IDLE
        self.store.cleanup()
        log.info("performance %s starting (%s mode)", self.session_id, self.mode)
        chapters = self.script.chapters
        idx = 0
        if start_at:
            ids = [c.id for c in chapters]
            if start_at not in ids:
                raise ValueError(f"unknown chapter {start_at!r}; one of {', '.join(ids)}")
            idx = ids.index(start_at)
        jumped_to_pose = False
        ambient = None
        simview = asyncio.ensure_future(self._sim_feed()) if self.sim_robot is not None else None
        try:
            if self.wait_for_start:
                await self._lobby()
            ambient = (asyncio.ensure_future(self._ambient_live())
                       if self.live_always and self._live is not None
                       and (self._real_camera[0] is not None or self.frame_source_factory is not None
                            or self.robot_frame_source_factory is not None)
                       else None)
            while idx < len(chapters):
                ch = chapters[idx]
                if ch.interactive and self.artifact is None and self.finale == "imitate":
                    self.artifact = self._choose_artifact()
                if ch.interactive and jumped_to_pose:
                    cues = [c for c in ch.cues if c.needs_pose_request or c.show == "chosen"]
                else:
                    cues = ch.cues_for(self.outcome) if ch.id == "present" else ch.cues
                try:
                    result = await self._play_chapter(ch, cues)
                except ShowCancelled:
                    if self._goto is None:
                        raise
                    self.stage.subtitle(None)
                    self._clear_live()
                if self._goto is not None:  # replay / jump (operator page hash): a chapter, or one of its steps
                    target, self._goto = self._goto, None
                    idx = [c.id for c in chapters].index(target.split(".")[0])
                    self._start_cue = target if "." in target else None
                    continue
                if result == "pose":
                    target = next((i for i, c in enumerate(chapters) if c.interactive), None)
                    if self.script.is_presentation or target is None:
                        self._jump_to_pose = True  # presentation: the next chapter starts at its round
                        idx += 1
                        continue
                    self._jump_to_pose = False
                    idx = target
                    jumped_to_pose = True
                    continue
                if ch.interactive:
                    if result == "decline":
                        await self.say_line("decline", "ok")
                        self.outcome = "alone"
                    else:
                        self.outcome = await self._interactive()
                idx += 1
            if self.script.is_presentation:
                self.outcome = "together" if any(r["photo"] for r in self.rounds) else "alone"
            self._set_state(ShowState.FINISHED)
        except ShowCancelled:
            self.stage.subtitle(None)
            self.stage.show(View("blank"))
            if self.motion is not None:
                self.motion.stop("performance cancelled")
            self.store.purge_session(self.session_id)
            self.composition = None
            self._set_state(ShowState.CANCELLED)
        except Exception as exc:
            log.exception("performance error: %s", exc)
            self.stage.subtitle(None)
            if self.motion is not None:
                self.motion.stop("performance error")
            self._set_state(ShowState.ERROR, str(exc))
        finally:
            for task in (ambient, simview):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.live_always, keep = False, self.live_always
            self._clear_live()
            self.live_always = keep
            self._close_camera()
        return ShowResult(self.session_id, self.state, self.outcome, self.artifact.id if self.artifact else None,
                          self.composition, self.delivered, self.delivery_detail, list(self.transitions),
                          list(self.chapters_played))
