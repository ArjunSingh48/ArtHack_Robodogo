"""Motion safety guard: the only path from a MotionPlan to a robot adapter.

Enforces, in order:
1. software stop latch (after /stop the robot stays disarmed until re-armed)
2. real hardware requires static gates (installation mode, go2, ROBOT_MOTION_ENABLED)
   AND a runtime arm by an authenticated operator
3. one motion at a time (lock), cooldown between motions
4. robot-state checks: connected, no error code, battery above minimum
5. per-motion timeout; on timeout a software stop is issued

The arming state is always explainable: every disarm and stop records why (`disarm_reason`, `history`), the
refusal of a later pose says it ("not armed — disarmed 14:02:11: greet failed: …"), and listeners (the show's
terminal, the web link's page) are told at once. A motion failure or an unknown outcome disarms; re-arming is
the operator saying "I have looked at the dog" and also clears the adapter's unknown-outcome latch.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time

from ..domain import ExecutionResult, ExecutionStatus, MotionPlan, RobotBehavior
from ..robot.base import RobotAdapter

log = logging.getLogger(__name__)


class MotionGuard:
    def __init__(self, robot: RobotAdapter, *, hardware_gates_open: bool, cooldown_s: float = 8.0,
                 timeout_s: float = 12.0, min_battery: int = 25):
        self.robot = robot
        self.hardware_gates_open = hardware_gates_open
        self.cooldown_s = cooldown_s
        self.timeout_s = timeout_s
        self.min_battery = min_battery
        self._armed = False
        self._armed_by: int | None = None
        self._stop_latched = False
        self.disarm_reason = "not armed yet"
        self.history: list[tuple[float, str, str]] = []  # (wall time, armed|disarmed|stopped|refused, reason)
        self.listeners: list = []  # fn(guard) — called after every change of the arming state
        self._lock = threading.Lock()
        self._last_motion_end = 0.0
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="motion")

    # ------------------------------------------------------------------ operator controls
    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def stop_latched(self) -> bool:
        return self._stop_latched

    def _record(self, event: str, reason: str) -> None:
        self.history.append((time.time(), event, reason))
        for fn in list(self.listeners):
            try:
                fn(self)
            except Exception as exc:  # pragma: no cover
                log.warning("arming listener failed: %s", exc)

    def state_line(self) -> str:
        """One line for the operator: ARMED, or why not."""
        if self._armed:
            return "ARMED"
        return ("STOP LATCHED" if self._stop_latched else "DISARMED") + f" — {self.disarm_reason}"

    def arm(self, operator_id: int) -> tuple[bool, str]:
        def refuse(why: str) -> tuple[bool, str]:
            log.warning("Arming refused: %s", why)
            self._record("refused", why)
            return False, why

        if self.robot.is_real and not self.hardware_gates_open:
            return refuse("hardware gates closed (APP_MODE/ROBOT_MODEL/ROBOT_MOTION_ENABLED)")
        blocker = getattr(self.robot, "motion_blocker", None)
        if self.robot.is_real and blocker:
            return refuse(blocker)
        st = self.robot.status()
        if self.robot.is_real and not st.connected:
            return refuse(f"robot not connected: {st.detail}")
        ack = getattr(self.robot, "acknowledge_unknown", None)
        cleared = ack() if ack is not None else None
        self._armed, self._armed_by, self._stop_latched = True, operator_id, False
        self.disarm_reason = ""
        log.warning("Motion ARMED by operator %s%s", operator_id, f" (cleared: {cleared})" if cleared else "")
        self._record("armed", f"operator {operator_id}" + (f"; cleared unknown outcome: {cleared}" if cleared else ""))
        msg = "armed"
        if cleared:
            msg += " — the previous unknown outcome is cleared (you checked the dog)"
        if self.robot.is_real and not getattr(self.robot, "reports_battery", True):
            msg += " — this connection does not report battery: watch it in the Unitree app"
        return True, msg

    def _end_walking(self) -> None:
        """A walk in progress stops at once (not at its next step) and leaves the walking gait."""
        try:
            if getattr(self.robot, "gait", False) and hasattr(self.robot, "end_walk"):
                self.robot.end_walk("disarmed")
            elif hasattr(self.robot, "halt"):
                self.robot.halt()
        except Exception as exc:
            log.warning("stopping the walk on disarm failed: %s", exc)

    def disarm(self, reason: str = "operator") -> None:
        self._armed = False
        self.disarm_reason = f"disarmed {time.strftime('%H:%M:%S')}: {reason}"
        log.warning("Motion DISARMED: %s", reason)
        self._end_walking()
        self._record("disarmed", reason)

    def software_stop(self, reason: str = "stop requested") -> None:
        """Cancel any motion and latch: no motion until an operator re-arms."""
        self._stop_latched = True
        self._armed = False
        self.disarm_reason = f"software stop {time.strftime('%H:%M:%S')}: {reason}"
        self.robot.stop()
        log.warning("SOFTWARE STOP: %s", reason)
        self._record("stopped", reason)

    # ------------------------------------------------------------------ execution
    def check(self, behavior: RobotBehavior) -> tuple[bool, str]:
        if behavior is RobotBehavior.STILLNESS:
            return True, "stillness"
        if self._stop_latched:
            if not self.robot.is_real:
                self._stop_latched = False  # simulation: a new moment may begin
            else:
                return False, f"stop latched — {self.disarm_reason}; operator must re-arm"
        if self.robot.is_real:
            if not self.hardware_gates_open:
                return False, "hardware gates closed"
            if not self._armed:
                return False, f"not armed — {self.disarm_reason}"
        since = time.monotonic() - self._last_motion_end
        if self._last_motion_end and since < self.cooldown_s:
            return False, f"cooldown ({self.cooldown_s - since:.1f}s left)"
        st = self.robot.status()
        if not st.connected:
            return False, f"robot not connected ({st.detail})"
        if st.error_code not in (None, 0):
            return False, f"robot reports error code {st.error_code}"
        if st.battery_percent is not None and st.battery_percent < self.min_battery:
            return False, f"battery {st.battery_percent}% below minimum {self.min_battery}%"
        if self.robot.is_real and st.battery_percent is None and getattr(self.robot, "reports_battery", True):
            return False, "battery level unknown"
        return True, "ok"

    def walk_allowed(self) -> tuple[bool, str]:
        """Walking (the show's approach) needs everything a pose needs — and a robot that can walk.

        Checked before the approach starts AND on every step, so /disarm or a software stop ends it."""
        if not getattr(self.robot, "can_walk", False):
            return False, "this robot connection cannot walk (use the ZHAW bridge)"
        if self._stop_latched and self.robot.is_real:
            return False, f"stop latched — {self.disarm_reason}; operator must re-arm"
        if self.robot.is_real and not (self.hardware_gates_open and self._armed):
            return False, f"not armed — {self.disarm_reason}" if self.hardware_gates_open else "hardware gates closed"
        st = self.robot.status()
        if not st.connected:
            return False, f"robot not connected ({st.detail})"
        if st.error_code not in (None, 0):
            return False, f"robot reports error code {st.error_code}"
        return True, "ok"

    def limit_for(self, behavior: RobotBehavior) -> float:
        """Never time out before the adapter's own (per-step SDK + transport + settle) budget."""
        budget = getattr(self.robot, "time_budget", None)
        try:
            return max(self.timeout_s, float(budget(behavior)) if budget else 0.0)
        except Exception:
            return self.timeout_s

    def execute(self, plan: MotionPlan) -> ExecutionResult:
        behavior = plan.behavior
        if behavior is RobotBehavior.STILLNESS:
            return ExecutionResult(behavior=behavior, status=ExecutionStatus.SKIPPED, detail="stillness chosen")
        if not self._lock.acquire(blocking=False):
            return ExecutionResult(behavior=behavior, status=ExecutionStatus.REJECTED, detail="another motion running")
        try:
            ok, why = self.check(behavior)
            if not ok:
                status = ExecutionStatus.SKIPPED if why.startswith(("not armed", "hardware")) else ExecutionStatus.REJECTED
                return ExecutionResult(behavior=behavior, status=status, detail=why)
            limit = self.limit_for(behavior)
            future = self._pool.submit(self.robot.execute, behavior, lambda: self._stop_latched)
            try:
                result = future.result(timeout=limit)
            except concurrent.futures.TimeoutError:
                self.software_stop(f"{behavior} exceeded {limit:.0f}s timeout")
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED, diagnostic="guard_timeout",
                                       detail=f"timeout after {limit:.0f}s — outcome unknown; stop latched")
            if result.status is ExecutionStatus.FAILED and self.robot.is_real:
                # a genuine failure OR an unknown outcome (SDK/transport timeout): disarm, the operator decides
                self.disarm(f"{behavior.value} failed ({result.diagnostic or 'failed'}): {result.detail} — "
                            f"check the dog, then re-arm")
            return result
        finally:
            self._last_motion_end = time.monotonic()
            self._lock.release()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
