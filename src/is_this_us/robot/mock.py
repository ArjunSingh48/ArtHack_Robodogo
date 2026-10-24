"""Mock robot: runs the full experience without hardware.

Simulates the same SDK step sequences as the real adapter, including settle times
(scaled by `time_scale`), battery drain, cooperative stop, and optional injected
failures for testing.
"""

from __future__ import annotations

import logging
import threading
import time

from ..domain import ExecutionResult, ExecutionStatus, RobotBehavior
from .base import BodyState, RobotStatus, check_args, steps_for

log = logging.getLogger(__name__)


class MockRobotAdapter:
    is_real = False

    def __init__(self, *, time_scale: float = 0.15, battery_percent: int = 87,
                 fail_on: set[str] | None = None):
        self.time_scale = time_scale
        self.battery = float(battery_percent)
        self.body = BodyState.STANDING
        self.fail_on = set(fail_on or ())
        self.connected = False
        self.calls: list[str] = []
        self._stop = threading.Event()

    def connect(self) -> None:
        self.connected = True

    def status(self) -> RobotStatus:
        return RobotStatus(connected=self.connected, is_real=False, model="mock", body=self.body,
                           battery_percent=int(self.battery), error_code=0, hardware_verified=False,
                           detail="simulated robot")

    def execute(self, behavior: RobotBehavior, should_stop=lambda: False) -> ExecutionResult:
        start = time.monotonic()
        self._stop.clear()
        steps = steps_for(behavior, self.body)
        if not steps:
            return ExecutionResult(behavior=behavior, status=ExecutionStatus.SIMULATED,
                                   detail=f"already {self.body}; nothing to do")
        for call, settle, new_body, args in steps:
            if self._stop.is_set() or should_stop():
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                       detail=f"stopped before {call}", duration_s=time.monotonic() - start)
            check_args(call, args)
            self.calls.append(f"{call}{args}" if args else call)
            if call in self.fail_on:
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED,
                                       detail=f"simulated SDK error in {call} (code 3104)",
                                       duration_s=time.monotonic() - start)
            # sleep in small slices so stop is responsive
            end = time.monotonic() + settle * self.time_scale
            while time.monotonic() < end:
                if self._stop.is_set():
                    return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                           detail=f"stopped during {call}", duration_s=time.monotonic() - start)
                time.sleep(0.01)
            self.body = new_body
            self.battery = max(0.0, self.battery - 0.05)
        return ExecutionResult(behavior=behavior, status=ExecutionStatus.SIMULATED,
                               detail=" → ".join(st.call for st in steps), duration_s=time.monotonic() - start)

    def stop(self) -> None:
        self._stop.set()
        self.calls.append("StopMove")

    def close(self) -> None:
        self.connected = False
