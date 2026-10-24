"""InteractionOrchestrator — the application state machine.

    IDLE → OBSERVING → INTERPRETING → MATCHING → PLANNING → MOVING → NARRATING
         → DELIVERING → AWAITING_RESPONSE → IDLE
    any state → STOPPED (software stop) | ERROR

Artistic decisions (interpretation, matching, dialogue) and physical control are
separated: the only thing that crosses into the robot is a `MotionPlan.behavior`,
a member of the closed `RobotBehavior` enum produced from curated annotations, and
it always passes through `MotionGuard`.

Two origins:
* installation — frame from the configured camera; motion via the guarded robot
  (real only if armed by an operator).
* photo — a visitor's Telegram photo; motion is ALWAYS simulated, never real.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from .artworks.repository import ArtworkRepository
from .dialogue.engine import DialogueEngine, Utterance
from .domain import (
    ArchiveSource, Artwork, ExecutionResult, ExecutionStatus, HumanObservation, MatchResult, MotionPlan,
)
from .matching.matcher import MemeMatcher, SceneMatcher, WeightedRelationshipMatcher
from .scenes import SceneLibrary
from .motion.guard import MotionGuard
from .motion.planner import SafeMotionPlanner
from .notifier import Notifier
from .robot.mock import MockRobotAdapter
from .speech.synthesizer import LocalSpeakerPlayer, NullSynthesizer, SpeechAudio, SpeechSynthesizer
from .storage import Storage
from .vision.frame_sources import FrameSource, decode_image_bytes
from .vision.interpreter import GeometricSceneInterpreter
from .vision.pose import PoseEstimator

log = logging.getLogger(__name__)


class State(StrEnum):
    IDLE = "idle"
    OBSERVING = "observing"
    INTERPRETING = "interpreting"
    MATCHING = "matching"
    PLANNING = "planning"
    MOVING = "moving"
    NARRATING = "narrating"
    DELIVERING = "delivering"
    AWAITING_RESPONSE = "awaiting_response"
    STOPPED = "stopped"
    ERROR = "error"


ALLOWED = {
    State.IDLE: {State.OBSERVING, State.INTERPRETING, State.MATCHING},
    State.OBSERVING: {State.INTERPRETING},
    State.INTERPRETING: {State.MATCHING},
    State.MATCHING: {State.PLANNING, State.NARRATING},
    State.PLANNING: {State.MOVING},
    State.MOVING: {State.NARRATING},
    State.NARRATING: {State.DELIVERING},
    State.DELIVERING: {State.AWAITING_RESPONSE},
    State.AWAITING_RESPONSE: {State.IDLE},
    State.STOPPED: {State.IDLE},
    State.ERROR: {State.IDLE},
}


@dataclass
class InteractionOutcome:
    interaction_id: str
    origin: str
    observation: HumanObservation
    match: MatchResult
    plan: MotionPlan
    result: ExecutionResult
    utterance: Utterance
    speech: SpeechAudio | None
    artwork: Artwork | None  # the chosen scene: a painting (Artwork) or a Meme
    trace: list[State] = field(default_factory=list)
    delivery_status: str = "pending"
    archive: str = "art"

    @property
    def scene(self):
        return self.artwork

    def debug_lines(self) -> list[str]:
        o, lines = self.observation, []
        lines.append(f"interaction   {self.interaction_id} ({self.origin}, archive={self.archive})")
        lines.append(f"posture       {o.posture} (conf {o.posture_confidence:.2f}); people={o.people_detected}; "
                     f"landmarks {o.landmark_confidence:.2f}; proximity {o.proximity}")
        if o.gestures:
            lines.append("gestures      " + ", ".join(f"{g}={v:.2f}" for g, v in o.gestures.items()))
        if o.notes:
            lines.append("notes         " + "; ".join(o.notes))
        for i, c in enumerate(self.match.ranked[:3]):
            s = c.scores
            mark = "→" if self.match.chosen and c.artwork.id == self.match.chosen.artwork.id else " "
            lines.append(f"{mark} match {i + 1}    {s.total:.3f}  {c.artwork.id:12} post={s.posture:.2f} rel={s.relationship:.2f} "
                         f"gest={s.gesture:.2f} feas={s.feasibility:.2f} conf={s.confidence:.2f} nov={s.novelty:.0f}  "
                         f"[{c.artwork.kind}] {c.artwork.title[:36]}")
        lines.append(f"match reason  {self.match.reason}")
        lines.append(f"motion plan   {self.plan.explanation} [{self.plan.fidelity}]")
        lines.append(f"execution     {self.result.status}: {self.result.detail}")
        lines.append(f"dialogue      {self.utterance.spoken}")
        lines.append(f"voice         {self.speech.provider + (' (cached)' if self.speech.cached else '') if self.speech else 'text-only'}")
        lines.append(f"delivery      {self.delivery_status}")
        lines.append("states        " + " → ".join(self.trace))
        return lines


class StateMachine:
    def __init__(self):
        self.state = State.IDLE
        self.trace: list[State] = [State.IDLE]

    def go(self, new: State) -> None:
        if new in (State.STOPPED, State.ERROR) or new in ALLOWED[self.state]:
            self.state = new
            self.trace.append(new)
        else:
            raise RuntimeError(f"illegal transition {self.state} → {new}")


class InteractionOrchestrator:
    def __init__(self, *, repo: ArtworkRepository | SceneLibrary, estimator: PoseEstimator | None, storage: Storage,
                 guard: MotionGuard, notifier: Notifier, synthesizer: SpeechSynthesizer | None = None,
                 interpreter: GeometricSceneInterpreter | None = None,
                 matcher: WeightedRelationshipMatcher | None = None, meme_matcher: MemeMatcher | None = None,
                 planner: SafeMotionPlanner | None = None, player: LocalSpeakerPlayer | None = None,
                 default_lang: str = "en", default_archive: str = "both", seed: int | None = None):
        # `repo` may be a plain painting repository (v0.2 API) or a SceneLibrary with memes.
        self.repo = repo if isinstance(repo, SceneLibrary) else SceneLibrary(repo)
        self.default_archive = ArchiveSource(default_archive)
        self.estimator = estimator
        self.storage = storage
        self.guard = guard
        self.notifier = notifier
        self.synth = synthesizer or NullSynthesizer()
        self.interpreter = interpreter or GeometricSceneInterpreter()
        self.matcher = SceneMatcher(self.repo, art=matcher, meme=meme_matcher)
        self.planner = planner or SafeMotionPlanner()
        self.player = player
        self.default_lang = default_lang
        self.rng = random.Random(seed)
        self.installation_lock = asyncio.Lock()
        self.current: StateMachine | None = None
        self.observation_open = True  # operator may close observation

    # ------------------------------------------------------------------ perception
    async def perceive(self, rgb: np.ndarray, source: str) -> HumanObservation:
        if self.estimator is None:
            raise RuntimeError("Pose estimator unavailable (install mediapipe and run `itu setup`).")
        people = await asyncio.to_thread(self.estimator.estimate, rgb)
        return self.interpreter.interpret(people, source=source)

    # ------------------------------------------------------------------ entry points
    def archive_for(self, chat_id: int, archive: str | None = None) -> ArchiveSource:
        return ArchiveSource(archive or self.storage.visitor_archive(chat_id, self.default_archive.value))

    async def observe_installation(self, chat_id: int, source: FrameSource,
                                   archive: str | None = None) -> InteractionOutcome:
        if self.installation_lock.locked():
            raise BusyError()
        async with self.installation_lock:
            sm = self.current = StateMachine()
            sm.go(State.OBSERVING)
            frame = await asyncio.to_thread(source.read)
            if frame is None:
                obs = HumanObservation(posture="unknown", posture_confidence=0, people_detected=0,
                                       notes=["camera returned no frame"], source=source.name)
                sm.go(State.INTERPRETING)
            else:
                sm.go(State.INTERPRETING)
                obs = await self.perceive(frame.image, frame.source)
                del frame  # frames are never retained
            return await self._respond(sm, chat_id, obs, origin="installation",
                                       archive=self.archive_for(chat_id, archive))

    async def observe_photo(self, chat_id: int, image_bytes: bytes, archive: str | None = None) -> InteractionOutcome:
        sm = StateMachine()
        sm.go(State.INTERPRETING)
        rgb = await asyncio.to_thread(decode_image_bytes, image_bytes)
        obs = await self.perceive(rgb, "telegram_photo")
        del rgb, image_bytes  # visitor photos are processed in memory only
        return await self._respond(sm, chat_id, obs, origin="photo", archive=self.archive_for(chat_id, archive))

    async def observe_given(self, chat_id: int, obs: HumanObservation, origin: str = "photo",
                            archive: str | None = None) -> InteractionOutcome:
        """Run the pipeline from an already-interpreted observation (tests, fixtures)."""
        sm = StateMachine()
        sm.go(State.INTERPRETING)
        return await self._respond(sm, chat_id, obs, origin=origin, archive=self.archive_for(chat_id, archive))

    async def another(self, chat_id: int, interaction_id: str) -> InteractionOutcome | None:
        row = self.storage.get_interaction(interaction_id, chat_id)
        if row is None or row.expired:
            return None
        obs = HumanObservation.model_validate(row.observation)
        sm = StateMachine()
        sm.go(State.MATCHING)
        if row.origin == "installation" and self.installation_lock.locked():
            raise BusyError()
        # stay in the same memory; a surprise keeps surprising
        return await self._respond(sm, chat_id, obs, origin=row.origin, exclude=row.shown_ids,
                                   interaction_id=interaction_id, is_retry=True, already_matching=True,
                                   archive=ArchiveSource(row.archive))

    # ------------------------------------------------------------------ core
    async def _respond(self, sm: StateMachine, chat_id: int, obs: HumanObservation, *, origin: str,
                       exclude: list[str] | None = None, interaction_id: str | None = None,
                       is_retry: bool = False, already_matching: bool = False,
                       archive: ArchiveSource = ArchiveSource.ART) -> InteractionOutcome:
        lang = self.storage.visitor_lang(chat_id, self.default_lang)
        turn = self.storage.count_interactions(chat_id)
        interaction_id = interaction_id or self.storage.new_interaction_id()
        exclude = list(exclude or [])

        if not already_matching:
            sm.go(State.MATCHING)
        first_meme = not self.storage.has_seen_meme(chat_id)
        match = self.matcher.match(obs, archive, exclude=exclude,
                                   recent=self.storage.recent_artworks(chat_id), rng=self.rng)
        artwork = match.chosen.artwork if match.chosen else None

        if artwork is not None:
            sm.go(State.PLANNING)
            plan = self.planner.plan(artwork)
            sm.go(State.MOVING)
            result = await self._move(plan, origin)
            if result.status is ExecutionStatus.STOPPED:
                sm.trace.append(State.STOPPED)  # recorded; the robot still says a gentle line
        else:
            plan = self.planner.plan(None)
            result = ExecutionResult(behavior=plan.behavior, status=ExecutionStatus.SKIPPED, detail="no match")

        sm.go(State.NARRATING)
        dialogue = DialogueEngine(lang)
        utterance = dialogue.compose(seed=f"{chat_id}:{interaction_id}:{len(exclude)}", observation=obs,
                                     artwork=artwork, plan=plan if artwork else None,
                                     result=result if artwork else None, turn=turn, is_retry=is_retry,
                                     first_meme=first_meme)
        speech = await asyncio.to_thread(self.synth.synthesize, utterance.spoken, lang)
        if speech and origin == "installation" and self.player and self.player.available:
            self.player.play(speech.audio_path)

        shown = exclude + ([artwork.id] if artwork else [])
        self.storage.save_interaction(
            interaction_id=interaction_id, chat_id=chat_id, origin=origin, posture=obs.posture.value,
            posture_confidence=obs.posture_confidence, artwork_id=artwork.id if artwork else None,
            shown_ids=shown, behavior=plan.behavior.value, status=result.status.value,
            observation=obs.model_dump(mode="json", include={"posture", "posture_confidence", "gestures",
                                                              "proximity", "people_detected", "landmark_confidence"}),
            archive=archive.value,
        )
        outcome = InteractionOutcome(
            interaction_id=interaction_id, origin=origin, observation=obs, match=match, plan=plan, result=result,
            utterance=utterance, speech=speech, artwork=artwork, trace=sm.trace, archive=archive.value,
        )
        sm.go(State.DELIVERING)
        try:
            outcome.delivery_status = await self.notifier.deliver(chat_id, outcome)
        except Exception as exc:  # delivery must never crash the installation
            log.exception("delivery failed")
            outcome.delivery_status = f"failed: {exc}"
        sm.go(State.AWAITING_RESPONSE)
        sm.go(State.IDLE)
        for line in outcome.debug_lines():
            log.info("[debug] %s", line)
        return outcome

    async def _move(self, plan: MotionPlan, origin: str) -> ExecutionResult:
        if origin == "photo":
            # Never move a real robot because of a photo sent from anywhere in the world.
            sim = MockRobotAdapter(time_scale=0.0)
            sim.connect()
            return await asyncio.to_thread(sim.execute, plan.behavior)
        return await asyncio.to_thread(self.guard.execute, plan)

    def stop(self, reason: str = "visitor /stop") -> None:
        self.guard.software_stop(reason)


class BusyError(RuntimeError):
    pass
