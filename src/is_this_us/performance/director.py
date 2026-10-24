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
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .catalog import PerformanceCatalog, ShowArtifact
from .lines import Lines
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
           ShowState.INVITATION, ShowState.READINESS, ShowState.CONSENT, ShowState.CAPTURE,
           ShowState.AWAITING_PHOTO, ShowState.COMPOSITION, ShowState.DELIVERY, ShowState.CONCLUSION}
SPEAKING = {ShowState.INTRODUCTION, ShowState.NARRATION, ShowState.INVITATION, ShowState.CONCLUSION}
VALID: dict[Cmd, set[ShowState]] = {
    Cmd.NEXT: SPEAKING | {ShowState.ARTWORK, ShowState.CONVERSATION},
    Cmd.PAUSE: RUNNING - {ShowState.COMPOSITION},
    Cmd.RESUME: {ShowState.PAUSED},
    Cmd.REPEAT: SPEAKING | {ShowState.ARTWORK, ShowState.CONVERSATION, ShowState.READINESS},
    Cmd.SKIP: RUNNING - {ShowState.COMPOSITION},
    Cmd.CANCEL: RUNNING | {ShowState.PAUSED},
    Cmd.POSE: {ShowState.INTRODUCTION, ShowState.NARRATION, ShowState.ARTWORK, ShowState.CONVERSATION,
               ShowState.INVITATION},
    Cmd.READY: {ShowState.READINESS},
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
                 photo_upload_allowed: bool = False, clock=time.monotonic):
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
        self.clock = clock

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
                "composition": bool(self.composition), "delivered": self.delivered}

    # ------------------------------------------------------------------ state + commands
    def _set_state(self, st: ShowState, detail: str = "") -> None:
        if st is self.state:
            return
        self._inbox.clear()  # commands meant for the previous state are stale now
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
            cue = cues[i]
            self._set_state(state)
            self.cue_id = cue.id
            self._show(cue.show)
            self._move(cue.motion)
            text = cue.render(pose_request=self.artifact.interactive.request) if (
                cue.needs_pose_request and self.artifact) else cue.text
            nxt = self._next_speech(cues, i)
            if nxt is not None and not nxt.needs_pose_request:
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
        if self.frame_source_factory is None:
            return None
        try:
            return self.frame_source_factory()
        except Exception as exc:
            log.warning("camera unavailable: %s", exc)
            return None

    async def _read_pose(self, camera) -> tuple[PoseReading | None, object | None]:
        if camera is None or self.pose_reader is None:
            return None, None
        try:
            frame = await asyncio.to_thread(camera.read)
            if frame is None:
                return None, None
            return await asyncio.to_thread(self.pose_reader.read, frame.image, frame.source), frame
        except Exception as exc:
            log.warning("pose reading failed: %s", exc)
            return None, None

    async def _guide_pose(self, art: ShowArtifact, camera) -> str:
        """Participant readiness. → "ready" | "declined" | "timeout". No forced timer for capture."""
        self._set_state(ShowState.READINESS)
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
                reading, _ = await self._read_pose(camera)
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
                cmd = await asyncio.wait_for(self._get_cmd(), self.tick_s)
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
        if frame is None and camera is not None:
            try:
                frame = await asyncio.to_thread(camera.read)
            except Exception:
                frame = None
        if frame is not None:
            from io import BytesIO

            from PIL import Image

            buf = BytesIO()
            Image.fromarray(frame.image).save(buf, format="JPEG", quality=92)
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
        """Invitation → readiness → consent → capture → composition → delivery. → together | alone."""
        art = self.artifact
        if art is None:
            log.warning("no interactive artwork available; the story continues without a photo")
            return "alone"
        camera = self._open_camera()
        try:
            ready = await self._guide_pose(art, camera)
            if ready != "ready":
                await self.say_line("decline", "ok")
                return "alone"
            self._set_state(ShowState.CONSENT)
            await self.say_line("consent", "ask")
            await self.say_line("consent", "explain")
            cmd = await self._await_cmd({Cmd.CONSENT_YES, Cmd.CONSENT_NO, Cmd.DECLINE, Cmd.SKIP},
                                        timeout=self.consent_timeout_s)
            if cmd is not Cmd.CONSENT_YES:
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
        finally:
            if camera is not None:
                try:
                    camera.close()
                except Exception:
                    pass

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
    async def run(self) -> ShowResult:
        if self.running:
            raise RuntimeError("a performance is already running")
        self.session_id = secrets.token_hex(3)
        self._inbox.clear()
        self.outcome, self.artifact, self.composition, self.delivered = "alone", None, None, False
        self.delivery_detail, self.last_reading, self.comparison = "", None, None
        self._capture_source = ""
        self.transitions, self.chapters_played = [], []
        self.state = ShowState.IDLE
        self.store.cleanup()
        log.info("performance %s starting (%s mode)", self.session_id, self.mode)
        chapters = self.script.chapters
        idx = 0
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
        return ShowResult(self.session_id, self.state, self.outcome, self.artifact.id if self.artifact else None,
                          self.composition, self.delivered, self.delivery_detail, list(self.transitions),
                          list(self.chapters_played))
