"""Safe motion planning: curated painting annotation -> approved robot behavior.

Input is ONLY the curated, human-reviewed `DogAnnotation` (a closed enum), never free
text and never anything produced by a language model. Output is one member of the
`RobotBehavior` vocabulary. Unsupported or disabled behaviors degrade along a fixed
fallback chain that always ends in stillness.
"""

from __future__ import annotations

from ..domain import Artwork, MotionPlan, Reproducibility, RobotBehavior

FALLBACK: dict[RobotBehavior, RobotBehavior] = {
    RobotBehavior.LIE_DOWN: RobotBehavior.SIT,
    RobotBehavior.SIT: RobotBehavior.STILLNESS,
    RobotBehavior.GREET: RobotBehavior.STAND,
    RobotBehavior.STRETCH: RobotBehavior.STAND,
    RobotBehavior.STAND: RobotBehavior.STILLNESS,
    RobotBehavior.STILLNESS: RobotBehavior.STILLNESS,
}

DEFAULT_ALLOWED = frozenset(RobotBehavior)


class SafeMotionPlanner:
    """Implements the MotionPlanner role."""

    def __init__(self, allowed: frozenset[RobotBehavior] = DEFAULT_ALLOWED):
        self.allowed = frozenset(allowed) | {RobotBehavior.STILLNESS}

    def plan(self, artwork: Artwork | None) -> MotionPlan:
        if artwork is None:
            return MotionPlan(requested=RobotBehavior.STILLNESS, behavior=RobotBehavior.STILLNESS,
                              fidelity=Reproducibility.NO, explanation="no painting chosen; remain still")
        dog = artwork.dog
        requested = dog.robot_behavior
        if dog.reproducible is Reproducibility.NO:
            return MotionPlan(requested=requested, behavior=RobotBehavior.STILLNESS, fidelity=Reproducibility.NO,
                              explanation=f"painted dog is {dog.posture}; not representable — remain still")
        behavior, hops = requested, 0
        while behavior not in self.allowed:
            behavior, hops = FALLBACK[behavior], hops + 1
        fidelity = dog.reproducible if hops == 0 else Reproducibility.APPROXIMATE
        if behavior is RobotBehavior.STILLNESS and requested is not RobotBehavior.STILLNESS:
            fidelity = Reproducibility.NO
        explanation = (f"painted dog {dog.posture} ({dog.relation}, {dog.interaction}) -> {behavior}"
                       + (f" (fallback from {requested})" if hops else ""))
        return MotionPlan(requested=requested, behavior=behavior, fidelity=fidelity, explanation=explanation)
