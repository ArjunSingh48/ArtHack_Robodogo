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


RUNNING = {ShowState.INTRODUCTION, ShowState.NARRATION, ShowState.ARTWORK, ShowState.CONVERSATION,
           ShowState.PARTICIPATION, ShowState.APPROACH,
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
    Cmd.READY: {ShowState.READINESS, ShowState.PARTICIPATION},
    # (APPROACH accepts only PAUSE / SKIP / CANCEL — all of which stop the walk at once)  # in a moment: "yes, they did it"
    Cmd.DECLINE: {ShowState.INVITATION, ShowState.READINESS, ShowState.CONSENT},
    Cmd.CONSENT_YES: {ShowState.CONSENT},
    Cmd.CONSENT_NO: {ShowState.CONSENT, ShowState.AWAITING_PHOTO},
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
        self.walk_log: list[str] = []
        self.clock = clock
        self.tries: list[tuple[str, bool]] = []  # (cue id, seen) — what the participant joined
        self._camera = None
        self._camera_failed = False
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
        if cmd is Cmd.POSE:
            self._jump_to_pose = True  # sticky: honoured at the next safe point, whatever is playing
        self._inbox.append(cmd)
        self._has_cmd.set()
        return True, f"ok: {cmd.value}"

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

    async def say_line(self, section: str, key: str) -> None:
        await self.say(self.lines.pick(section, key), kind="lines")

    async def _play_cue(self, ch: Chapter, cue: Cue, text: str) -> str:
        """→ "done" | "next" | "skip" | "pose" (repeat and pause replay the cue)."""
        while True:
            if cue.kind == "pause":
                task = asyncio.ensure_future(self.voice.silence(cue.seconds))
            else:
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
        if ch.id == "present":
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
        i = 0
        while i < len(cues):
            if self._jump_to_pose and not ch.interactive:
                return "pose"
            cue = cues[i]
            self._set_state(state)
            self.cue_id = cue.id
            self._show(cue.show)
            self._move(cue.motion)
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
                pose_request=self.artifact.interactive.request if self.artifact else "",
                practised=self.lines.text("invite", "practised") if any(seen for _, seen in self.tries) else "",
            ) if "{" in cue.text else cue.text
            if not text.strip():  # e.g. {practised} when nobody joined a moment
                i += 1
                continue
            nxt = self._next_speech(cues, i)
            if nxt is not None and "{" not in nxt.text:
                self.voice.prefetch(nxt.text, kind="narration", direction=ch.voice)
            result = await self._play_cue(ch, cue, text)
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
        try:
            self._camera = self.frame_source_factory()
        except Exception as exc:
            log.warning("camera unavailable: %s", exc)
            self._camera_failed = True
            return None
        return self._camera

    def _close_camera(self) -> None:
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:
                pass
        self._camera = None

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

    async def _publish_live(self, frame, reading: PoseReading | None, checks) -> None:
        """Show the participant what the camera sees (mirrored, skeleton, checklist). In memory only."""
        live = self._live
        if live is None or frame is None:
            return
        from .live_view import render

        try:
            jpeg = await asyncio.to_thread(render, frame.image, reading, mirror=self._mirror(frame))
            live(jpeg, [(c.label, c.ok) for c in checks])
        except Exception as exc:
            log.debug("live view failed: %s", exc)

    def _mirror(self, frame) -> bool:
        """Show and keep images like a mirror, so left/right match what the participant saw while
        posing — and the painting. Not if the camera already mirrors, nor for the drawn stand-in."""
        return not getattr(self.pose_reader, "mirrored", False) and getattr(frame, "source", "") != "synthetic"

    def _clear_live(self) -> None:
        live = self._live
        if live is not None:
            try:
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
        """Invitation → readiness → [walk + dog pose] → consent → capture → composition → delivery."""
        art = self.artifact
        if art is None:
            log.warning("no interactive artwork available; the story continues without a photo")
            return "alone"
        camera = self._open_camera()
        try:
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

    async def _walk_home(self) -> None:
        if self.approacher is None or self.approacher.memory is None or not self.approacher.memory.path:
            return
        self._set_state(ShowState.APPROACH, "walking back")
        await self.say_line("approach", "home")
        if self.motion is not None:
            await asyncio.to_thread(self.motion.perform, "stand")  # stand up before walking
        res, cmd = await self._walk(self.approacher.go_home)
        self.walk_log.append(f"home: {res or 'interrupted'}")
        if cmd is Cmd.CANCEL:
            raise ShowCancelled()
        if cmd is Cmd.PAUSE:
            await self._paused()

    async def _compose(self, art: ShowArtifact, photo: bytes, reading: PoseReading | None) -> Path:
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
        self.comparison = compare(art, reading, painted)
        then_img = Image.open(art.image_path).convert("RGB")
        commentary = self.lines.text("compare", self.comparison.line_keys[-1])
        data = await asyncio.to_thread(compose, then_img, now_img, art, self.comparison, commentary=commentary,
                                       theme=self.theme, font_path=self.font_path, session_id=self.session_id,
                                       now_note=("Rehearsal stand-in — not a photograph" if self._capture_source == "synthetic"
                                                 else "Photograph taken with consent"))
        self.store.save(self.session_id, "now.jpg", photo)
        return self.store.save(self.session_id, "is-this-us.jpg", data)

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
        try:
            while idx < len(chapters):
                ch = chapters[idx]
                if ch.interactive and self.artifact is None:
                    self.artifact = self._choose_artifact()
                if ch.interactive and jumped_to_pose:
                    cues = [c for c in ch.cues if c.needs_pose_request or c.show == "chosen"]
                else:
                    cues = ch.cues_for(self.outcome) if ch.id == "present" else ch.cues
                result = await self._play_chapter(ch, cues)
                if result == "pose":
                    self._jump_to_pose = False
                    idx = next(i for i, c in enumerate(chapters) if c.interactive)
                    jumped_to_pose = True
                    continue
                if ch.interactive:
                    if result == "decline":
                        await self.say_line("decline", "ok")
                        self.outcome = "alone"
                    else:
                        self.outcome = await self._interactive()
                idx += 1
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
            self._clear_live()
            self._close_camera()
        return ShowResult(self.session_id, self.state, self.outcome, self.artifact.id if self.artifact else None,
                          self.composition, self.delivered, self.delivery_detail, list(self.transitions),
                          list(self.chapters_played))
