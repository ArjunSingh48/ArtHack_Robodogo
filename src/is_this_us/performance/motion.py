"""Optional robot cues during the show — through the existing MotionGuard, never around it.

Off by default (`SHOW_MOTION_ENABLED=false`). When on, a cue still needs every existing
gate: installation mode, real-robot settings, an operator `/arm`, the planner's approved
behaviors (fallbacks apply), cooldown, timeout and the stop latch. Cues are fire-and-forget:
the narration never waits for the body, and a refused or failed motion changes nothing
in the story. Only behaviors from the existing `RobotBehavior` vocabulary are possible —
no walking, jumping or balancing actions exist in this code.
"""

from __future__ import annotations

import logging
import threading

from ..domain import MotionPlan, Reproducibility, RobotBehavior
from ..motion.planner import FALLBACK

log = logging.getLogger(__name__)


class MotionCues:
    def __init__(self, guard=None, planner=None, *, enabled: bool = False):
        self.guard = guard
        self.planner = planner
        self.enabled = bool(enabled and guard is not None)
        self.log: list[str] = []
        self._busy = threading.Lock()

    def _plan(self, behavior: RobotBehavior) -> MotionPlan:
        allowed = self.planner.allowed if self.planner is not None else {RobotBehavior.STILLNESS}
        b, hops = behavior, 0
        while b not in allowed:
            b, hops = FALLBACK[b], hops + 1
        return MotionPlan(requested=behavior, behavior=b,
                          fidelity=Reproducibility.YES if hops == 0 else Reproducibility.APPROXIMATE,
                          explanation=f"performance cue {behavior.value}" + (f" → {b.value}" if hops else ""))

    def request(self, name: str) -> None:
        try:
            behavior = RobotBehavior(name)
        except ValueError:
            self.log.append(f"unknown behavior {name!r} — ignored")
            log.warning("script asks for unknown behavior %r; ignored", name)
            return
        if not self.enabled:
            self.log.append(f"{behavior.value}: not performed (show motion off)")
            return
        if not self._busy.acquire(blocking=False):
            self.log.append(f"{behavior.value}: skipped (previous motion still running)")
            return
        plan = self._plan(behavior)

        def run():
            try:
                res = self.guard.execute(plan)
                self.log.append(f"{behavior.value}: {res.status.value} {res.detail}".strip())
                log.info("motion cue %s → %s %s", behavior.value, res.status.value, res.detail)
            except Exception as exc:
                self.log.append(f"{behavior.value}: failed {exc}")
                log.warning("motion cue failed: %s", exc)
            finally:
                self._busy.release()

        threading.Thread(target=run, daemon=True).start()

    def stop(self, reason: str) -> None:
        if self.guard is not None:
            try:
                self.guard.software_stop(reason)
            except Exception as exc:
                log.warning("software stop failed: %s", exc)
