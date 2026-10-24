"""Motion safety guard: the only path from a MotionPlan to a robot adapter.

Enforces, in order:
1. software stop latch (after /stop the robot stays disarmed until re-armed)
2. real hardware requires static gates (installation mode, go2, ROBOT_MOTION_ENABLED)
   AND a runtime arm by an authenticated operator
3. one motion at a time (lock), cooldown between motions
4. robot-state checks: connected, no error code, battery above minimum
5. per-motion timeout; on timeout a software stop is issued
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
        self._lock = threading.Lock()
        self._last_motion_end = 0.0
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="motion")

    # ------------------------------------------------------------------ operator controls
    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self, operator_id: int) -> tuple[bool, str]:
        if self.robot.is_real and not self.hardware_gates_open:
            return False, "hardware gates closed (APP_MODE/ROBOT_MODEL/ROBOT_MOTION_ENABLED)"
        st = self.robot.status()
        if self.robot.is_real and not st.connected:
            return False, f"robot not connected: {st.detail}"
        self._armed, self._armed_by, self._stop_latched = True, operator_id, False
        log.warning("Motion ARMED by operator %s", operator_id)
        if self.robot.is_real and not getattr(self.robot, "reports_battery", True):
            return True, "armed — this connection does not report battery: watch it in the Unitree app"
        return True, "armed"

    def disarm(self, reason: str = "operator") -> None:
        self._armed = False
        log.warning("Motion disarmed (%s)", reason)

    def software_stop(self, reason: str = "stop requested") -> None:
        """Cancel any motion and latch: no motion until an operator re-arms."""
        self._stop_latched = True
        self._armed = False
        self.robot.stop()
        log.warning("SOFTWARE STOP: %s", reason)

    # ------------------------------------------------------------------ execution
    def check(self, behavior: RobotBehavior) -> tuple[bool, str]:
        if behavior is RobotBehavior.STILLNESS:
            return True, "stillness"
        if self._stop_latched:
            if not self.robot.is_real:
                self._stop_latched = False  # simulation: a new moment may begin
            else:
                return False, "stop latched; operator must re-arm"
        if self.robot.is_real:
            if not self.hardware_gates_open:
                return False, "hardware gates closed"
            if not self._armed:
                return False, "not armed by operator"
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
            future = self._pool.submit(self.robot.execute, behavior, lambda: self._stop_latched)
            try:
                result = future.result(timeout=self.timeout_s)
            except concurrent.futures.TimeoutError:
                self.software_stop(f"{behavior} exceeded {self.timeout_s}s timeout")
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                       detail=f"timeout after {self.timeout_s}s")
            if result.status is ExecutionStatus.FAILED and self.robot.is_real:
                self.disarm("motion failure")
            return result
        finally:
            self._last_motion_end = time.monotonic()
            self._lock.release()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
