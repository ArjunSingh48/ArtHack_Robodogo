"""Robot adapter interface and the shared high-level step sequences.

Body-state transitions are expressed in terms of official Go2 SportClient calls
(verified to exist in unitreerobotics/unitree_sdk2_python, 2026-09 HEAD):
StandUp, StandDown, BalanceStand, Sit, RiseSit, Hello, Stretch, StopMove.
The mock adapter simulates exactly the same sequences so simulation is faithful.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from ..domain import ExecutionResult, RobotBehavior


class BodyState(StrEnum):
    STANDING = "standing"
    SITTING = "sitting"
    LYING = "lying"
    UNKNOWN = "unknown"


class RobotStatus(BaseModel):
    connected: bool
    is_real: bool
    model: str
    body: BodyState = BodyState.UNKNOWN
    battery_percent: int | None = None
    error_code: int | None = None
    hardware_verified: bool = False
    detail: str = ""


class RobotAdapter(Protocol):
    is_real: bool

    def connect(self) -> None: ...
    def status(self) -> RobotStatus: ...
    def execute(self, behavior: RobotBehavior, should_stop: callable) -> ExecutionResult: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


# (sdk_call_name, settle_seconds, resulting_body_state)
Step = tuple[str, float, BodyState]

_TO_STANDING: dict[BodyState, list[Step]] = {
    BodyState.STANDING: [("BalanceStand", 0.5, BodyState.STANDING)],
    BodyState.SITTING: [("RiseSit", 2.5, BodyState.STANDING), ("BalanceStand", 0.5, BodyState.STANDING)],
    BodyState.LYING: [("StandUp", 2.5, BodyState.STANDING), ("BalanceStand", 1.0, BodyState.STANDING)],
    BodyState.UNKNOWN: [("StandUp", 2.5, BodyState.STANDING), ("BalanceStand", 1.0, BodyState.STANDING)],
}


def steps_for(behavior: RobotBehavior, body: BodyState) -> list[Step]:
    """Return the ordered SDK steps to perform `behavior` from the current body state."""
    if behavior is RobotBehavior.STILLNESS:
        return []
    if behavior is RobotBehavior.STAND:
        return _TO_STANDING[body]
    if behavior is RobotBehavior.SIT:
        if body is BodyState.SITTING:
            return []
        prep = [] if body is BodyState.STANDING else _TO_STANDING[body]
        return prep + [("Sit", 3.0, BodyState.SITTING)]
    if behavior is RobotBehavior.LIE_DOWN:
        if body is BodyState.LYING:
            return []
        prep = [("RiseSit", 2.5, BodyState.STANDING)] if body is BodyState.SITTING else []
        if body is BodyState.UNKNOWN:
            prep = _TO_STANDING[body]
        return prep + [("StandDown", 2.5, BodyState.LYING)]
    if behavior is RobotBehavior.GREET:
        prep = [] if body is BodyState.STANDING else _TO_STANDING[body]
        return prep + [("Hello", 4.0, BodyState.STANDING)]
    if behavior is RobotBehavior.STRETCH:
        prep = [] if body is BodyState.STANDING else _TO_STANDING[body]
        return prep + [("Stretch", 4.0, BodyState.STANDING)]
    raise ValueError(f"Unsupported behavior {behavior!r}")


ALLOWED_SDK_CALLS = frozenset({"StandUp", "StandDown", "BalanceStand", "Sit", "RiseSit", "Hello", "Stretch"})
