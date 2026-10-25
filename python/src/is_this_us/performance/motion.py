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
        self.last_perform = ""

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

    def perform(self, name: str, *, should_stop=lambda: False, on_wait=None, max_wait_s: float | None = None):
        """Like request(), but waits for the result (used around walking, where order matters).

        If the guard's cooldown is still running, wait for exactly what is left — asking never extends it — then
        perform the motion ONCE. The wait is bounded (cooldown + 1 s), checks stop / disarm / cancel every 0.1 s,
        and is announced through `on_wait(seconds_left)` so the operator never sees a silent freeze.
        `self.last_perform` says what happened: executed | waited+executed | cancelled | refused."""
        import time

        from ..domain import ExecutionResult, ExecutionStatus

        try:
            behavior = RobotBehavior(name)
        except ValueError:
            return None
        if not self.enabled:
            self.log.append(f"{behavior.value}: not performed (show motion off)")
            return None
        guard = self.guard
        remaining = getattr(guard, "cooldown_remaining", None)
        limit = max_wait_s if max_wait_s is not None else float(getattr(guard, "cooldown_s", 0) or 0) + 1.0
        with self._busy:
            waited, start = 0.0, time.monotonic()
            left = remaining() if remaining is not None else 0.0
            if left > 0:
                if left > limit:
                    self.last_perform = "refused"
                    res = ExecutionResult(behavior=behavior, status=ExecutionStatus.REJECTED,
                                          detail=f"cooldown ({left:.1f}s left) longer than the {limit:.1f}s wait")
                    self.log.append(f"{behavior.value}: {res.status.value} {res.detail}")
                    return res
                log.info("motion %s: waiting %.1f s for a safe pose transition (cooldown)", behavior.value, left)
                if on_wait is not None:
                    try:
                        on_wait(left)
                    except Exception:
                        pass
                while (left := remaining()) > 0:
                    if should_stop() or getattr(guard, "stop_latched", False) or (
                            getattr(getattr(guard, "robot", None), "is_real", False) and not guard.armed):
                        self.last_perform = "cancelled"
                        res = ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                              detail="cancelled while waiting for the cooldown — nothing sent")
                        self.log.append(f"{behavior.value}: {res.status.value} {res.detail}")
                        return res
                    time.sleep(min(0.1, left))
                waited = time.monotonic() - start
            res = guard.execute(self._plan(behavior))
            self.last_perform = ("waited+executed" if waited else "executed") if res.status in (
                ExecutionStatus.SUCCESS, ExecutionStatus.SIMULATED) else "refused"
        detail = f"{res.detail} (waited {waited:.1f}s for the cooldown)" if waited else res.detail
        self.log.append(f"{behavior.value}: {res.status.value} {detail}".strip())
        return res

    def stop(self, reason: str) -> None:
        if self.guard is not None:
            try:
                self.guard.software_stop(reason)
            except Exception as exc:
                log.warning("software stop failed: %s", exc)
