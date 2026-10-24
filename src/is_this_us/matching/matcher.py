"""Relationship matching: find the painted human–dog relationship that the visitor's
body seems to invite.

We never compare the human's pose with a dog's pose. We ask two questions:

1. *Is the visitor's body like the painted human's body?* (posture + gestures)
2. *Does the visitor's body invite the relationship the painted dog has with its
   human?* (e.g. arms held together invite "in arms"; a crouch invites "in front";
   sitting invites "at feet / beside"; distance invites "waiting / accompanying").

Each answer is a transparent score in [0, 1]. Weights are configurable. Selection is
deterministic given a seed, with bounded randomness among near-ties so visitors do
not always receive the same painting.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable

from pydantic import BaseModel

from ..domain import (
    Artwork, HumanGesture, HumanObservation, HumanPosture, Interaction, MatchCandidate,
    MatchResult, Proximity, Reproducibility, ScoreBreakdown, SpatialRelation,
)

# Rough "body height" level of each posture; similarity decays with distance.
POSTURE_LEVEL: dict[HumanPosture, float] = {
    HumanPosture.STANDING: 3.0,
    HumanPosture.LEANING_FORWARD: 2.5,
    HumanPosture.SITTING: 2.0,
    HumanPosture.KNEELING: 1.5,
    HumanPosture.CROUCHING: 1.3,
    HumanPosture.RECLINING: 1.0,
    HumanPosture.LYING: 0.0,
}

# What relationships does a body posture invite from a dog? (0..1)
POSTURE_INVITES: dict[HumanPosture, dict[SpatialRelation | Interaction, float]] = {
    HumanPosture.STANDING: {
        SpatialRelation.BESIDE: 1.0, Interaction.TOUCHING_HEAD: 0.7, Interaction.ACCOMPANYING: 0.8,
        Interaction.WAITING: 0.7, Interaction.GAZING_UP: 0.7, SpatialRelation.APART: 0.6,
        SpatialRelation.IN_ARMS: 0.4,
    },
    HumanPosture.LEANING_FORWARD: {
        SpatialRelation.IN_FRONT: 1.0, Interaction.GREETING: 0.9, Interaction.TOUCHING_HEAD: 0.9,
        Interaction.OFFERING: 0.8, Interaction.GAZING_UP: 0.6,
    },
    HumanPosture.SITTING: {
        SpatialRelation.AT_FEET: 1.0, SpatialRelation.BESIDE: 0.9, SpatialRelation.ON_LAP: 0.8,
        Interaction.RESTING_TOGETHER: 1.0, Interaction.GREETING: 0.5, Interaction.GAZING_UP: 0.6,
    },
    HumanPosture.KNEELING: {
        SpatialRelation.IN_FRONT: 1.0, Interaction.OFFERING: 0.9, Interaction.TOUCHING_HEAD: 0.8,
        Interaction.EMBRACE: 0.8, SpatialRelation.BESIDE: 0.6,
    },
    HumanPosture.CROUCHING: {
        SpatialRelation.IN_FRONT: 1.0, Interaction.GREETING: 0.9, Interaction.OFFERING: 0.8,
        Interaction.EMBRACE: 0.7, Interaction.TOUCHING_HEAD: 0.7,
    },
    HumanPosture.RECLINING: {
        SpatialRelation.BESIDE: 1.0, Interaction.RESTING_TOGETHER: 1.0, Interaction.GAZING_UP: 0.8,
        Interaction.WAITING: 0.6,
    },
    HumanPosture.LYING: {
        SpatialRelation.BESIDE: 1.0, Interaction.RESTING_TOGETHER: 1.0, Interaction.WAITING: 0.7,
    },
}

GESTURE_INVITES: dict[HumanGesture, dict[SpatialRelation | Interaction, float]] = {
    HumanGesture.ARMS_HOLDING: {
        SpatialRelation.IN_ARMS: 1.0, SpatialRelation.ON_LAP: 0.8, Interaction.HOLDING: 1.0, Interaction.EMBRACE: 0.9,
    },
    HumanGesture.EXTENDING_ARM: {
        Interaction.TOUCHING_HEAD: 1.0, Interaction.OFFERING: 0.9, Interaction.GREETING: 0.8,
        Interaction.ACCOMPANYING: 0.5,
    },
    HumanGesture.LOOKING_DOWN: {
        SpatialRelation.AT_FEET: 0.8, Interaction.GAZING_UP: 0.9, Interaction.RESTING_TOGETHER: 0.5,
    },
    HumanGesture.TURNED_TOWARD: {Interaction.GREETING: 0.6, Interaction.GAZING_UP: 0.5},
    HumanGesture.TURNED_AWAY: {Interaction.ACCOMPANYING: 0.7, Interaction.WAITING: 0.7, SpatialRelation.APART: 0.6},
}

PROXIMITY_INVITES: dict[Proximity, dict[SpatialRelation, float]] = {
    Proximity.CLOSE: {SpatialRelation.IN_ARMS: 1.0, SpatialRelation.ON_LAP: 1.0, SpatialRelation.BESIDE: 0.9,
                      SpatialRelation.AT_FEET: 0.9, SpatialRelation.IN_FRONT: 0.8, SpatialRelation.APART: 0.1},
    Proximity.MEDIUM: {SpatialRelation.BESIDE: 0.9, SpatialRelation.IN_FRONT: 0.9, SpatialRelation.AT_FEET: 0.7,
                       SpatialRelation.APART: 0.5, SpatialRelation.BEHIND: 0.5},
    Proximity.FAR: {SpatialRelation.APART: 1.0, SpatialRelation.BEHIND: 0.7, SpatialRelation.IN_FRONT: 0.5},
}

FEASIBILITY = {Reproducibility.YES: 1.0, Reproducibility.APPROXIMATE: 0.65, Reproducibility.NO: 0.2}


class MatchWeights(BaseModel):
    posture: float = 0.30
    relationship: float = 0.30
    gesture: float = 0.15
    feasibility: float = 0.10
    confidence: float = 0.10
    novelty: float = 0.05
    visual: float = 0.0  # reserved for a later multimodal model


class MatchSettings(BaseModel):
    weights: MatchWeights = MatchWeights()
    min_score: float = 0.35  # below this: "I don't think I've seen this before"
    tie_margin: float = 0.06  # candidates within this of the best are near-ties
    max_choices: int = 3
    temperature: float = 0.03


def posture_similarity(observed: HumanPosture, painted: HumanPosture) -> float:
    if observed is HumanPosture.UNKNOWN or painted is HumanPosture.UNKNOWN:
        return 0.45  # neutral: neither confirms nor excludes
    if observed is painted:
        return 1.0
    d = abs(POSTURE_LEVEL[observed] - POSTURE_LEVEL[painted])
    return max(0.0, 1.0 - d / 2.0) * 0.85


def relationship_score(obs: HumanObservation, art: Artwork) -> float:
    rel, inter = art.dog.relation, art.dog.interaction
    invites: dict = dict(POSTURE_INVITES.get(obs.posture, {}))
    for g, strength in obs.gestures.items():
        for k, v in GESTURE_INVITES.get(g, {}).items():
            invites[k] = max(invites.get(k, 0.0), v * strength)
    rel_s = invites.get(rel, 0.25)
    inter_s = invites.get(inter, 0.25)
    prox = PROXIMITY_INVITES.get(obs.proximity, {}).get(rel, 0.5) if obs.proximity is not Proximity.UNKNOWN else 0.5
    return 0.4 * rel_s + 0.4 * inter_s + 0.2 * prox


def gesture_score(obs: HumanObservation, art: Artwork) -> float:
    relevant = {HumanGesture.ARMS_HOLDING, HumanGesture.EXTENDING_ARM, HumanGesture.LOOKING_DOWN}
    seen = {g: s for g, s in obs.gestures.items() if g in relevant and s >= 0.3}
    painted = set(art.human.gestures) & relevant
    if not seen and not painted:
        return 0.6  # both calm: a quiet match
    if not seen or not painted:
        return 0.3
    hits = sum(seen.get(g, 0.0) for g in painted)
    return min(1.0, hits / max(len(painted), len(seen)))


class WeightedRelationshipMatcher:
    """Implements the ArtworkMatcher role."""

    def __init__(self, settings: MatchSettings | None = None,
                 visual_scorer: Callable[[HumanObservation, Artwork], float] | None = None):
        self.settings = settings or MatchSettings()
        self.visual_scorer = visual_scorer

    def score(self, obs: HumanObservation, art: Artwork, recent: set[str]) -> ScoreBreakdown:
        w = self.settings.weights
        c = obs.posture_confidence
        posture = c * posture_similarity(obs.posture, art.human.posture) + (1 - c) * 0.45
        relationship = relationship_score(obs, art)
        gesture = gesture_score(obs, art)
        feasibility = FEASIBILITY[art.dog.reproducible]
        confidence = 0.6 * art.dog.confidence + 0.4 * art.human.confidence
        novelty = 0.0 if art.id in recent else 1.0
        visual = self.visual_scorer(obs, art) if self.visual_scorer else 0.0
        total = (w.posture * posture + w.relationship * relationship + w.gesture * gesture
                 + w.feasibility * feasibility + w.confidence * confidence + w.novelty * novelty
                 + w.visual * visual)
        norm = w.posture + w.relationship + w.gesture + w.feasibility + w.confidence + w.novelty + w.visual
        return ScoreBreakdown(
            posture=round(posture, 3), relationship=round(relationship, 3), gesture=round(gesture, 3),
            feasibility=round(feasibility, 3), confidence=round(confidence, 3), novelty=novelty,
            total=round(total / norm, 4),
        )

    def match(self, obs: HumanObservation, artworks: Iterable[Artwork], *, exclude: Iterable[str] = (),
              recent: Iterable[str] = (), rng: random.Random | None = None) -> MatchResult:
        rng = rng or random.Random()
        excluded, recent_set = set(exclude), set(recent)
        ranked = sorted(
            (MatchCandidate(artwork=a, scores=self.score(obs, a, recent_set))
             for a in artworks if a.id not in excluded),
            key=lambda c: c.scores.total, reverse=True,
        )
        if obs.posture is HumanPosture.UNKNOWN and obs.posture_confidence == 0 and not obs.gestures:
            return MatchResult(chosen=None, ranked=ranked, reason="nobody clearly visible")
        if not ranked or ranked[0].scores.total < self.settings.min_score:
            return MatchResult(chosen=None, ranked=ranked, reason="no painting relates closely enough")

        best = ranked[0].scores.total
        pool = [c for c in ranked[: self.settings.max_choices] if best - c.scores.total <= self.settings.tie_margin]
        t = max(self.settings.temperature, 1e-6)
        weights = [math.exp((c.scores.total - best) / t) for c in pool]
        chosen = rng.choices(pool, weights=weights, k=1)[0]
        reason = f"chose from {len(pool)} near-tie(s); best={best:.3f}"
        return MatchResult(chosen=chosen, ranked=ranked, reason=reason)
