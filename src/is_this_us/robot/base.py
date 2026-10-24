"""Robot adapter interface and the shared high-level step sequences.

Body-state transitions are expressed in terms of official Go2 SportClient calls
(verified to exist in unitreerobotics/unitree_sdk2_python, 2026-09 HEAD):
StandUp, StandDown, BalanceStand, Sit, RiseSit, Hello, Stretch, StopMove.
The mock adapter simulates exactly the same sequences so simulation is faithful.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple, Protocol

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


class Step(NamedTuple):
    call: str  # SportClient method name
    settle: float  # seconds to wait after the call
    body: BodyState  # body state afterwards
    args: tuple[float, ...] = ()  # only for Euler; fixed constants below, never from data


# Body-orientation poses use SportClient.Euler(roll, pitch, yaw) in radians while standing.
# Unitree documents roughly ±0.75 roll/pitch and ±0.6 yaw; we stay far inside that.
# HARDWARE-UNVERIFIED: sign conventions (which way is "nose down") must be checked on the robot.
EULER_LIMIT = 0.3
LOOK_ASIDE_YAW = 0.25
PEER_PITCH = 0.15
_NEUTRAL = (0.0, 0.0, 0.0)

S = BodyState
_TO_STANDING: dict[BodyState, list[Step]] = {
    S.STANDING: [Step("BalanceStand", 0.5, S.STANDING)],
    S.SITTING: [Step("RiseSit", 2.5, S.STANDING), Step("BalanceStand", 0.5, S.STANDING)],
    S.LYING: [Step("StandUp", 2.5, S.STANDING), Step("BalanceStand", 1.0, S.STANDING)],
    S.UNKNOWN: [Step("StandUp", 2.5, S.STANDING), Step("BalanceStand", 1.0, S.STANDING)],
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
        return prep + [Step("Sit", 3.0, S.SITTING)]
    if behavior is RobotBehavior.LIE_DOWN:
        if body is BodyState.LYING:
            return []
        prep = [Step("RiseSit", 2.5, S.STANDING)] if body is BodyState.SITTING else []
        if body is BodyState.UNKNOWN:
            prep = _TO_STANDING[body]
        return prep + [Step("StandDown", 2.5, S.LYING)]
    if behavior is RobotBehavior.GREET:
        prep = [] if body is BodyState.STANDING else _TO_STANDING[body]
        return prep + [Step("Hello", 4.0, S.STANDING)]
    if behavior is RobotBehavior.STRETCH:
        prep = [] if body is BodyState.STANDING else _TO_STANDING[body]
        return prep + [Step("Stretch", 4.0, S.STANDING)]
    if behavior in (RobotBehavior.LOOK_ASIDE, RobotBehavior.PEER):
        prep = _TO_STANDING[body]  # always re-balance first
        pose = (0.0, 0.0, LOOK_ASIDE_YAW) if behavior is RobotBehavior.LOOK_ASIDE else (0.0, PEER_PITCH, 0.0)
        return prep + [Step("Euler", 2.5, S.STANDING, pose), Step("Euler", 0.8, S.STANDING, _NEUTRAL)]
    raise ValueError(f"Unsupported behavior {behavior!r}")


ALLOWED_SDK_CALLS = frozenset({"StandUp", "StandDown", "BalanceStand", "Sit", "RiseSit", "Hello", "Stretch", "Euler"})


def check_args(call: str, args: tuple[float, ...]) -> None:
    """Refuse any arguments except small Euler angles."""
    if call != "Euler":
        if args:
            raise PermissionError(f"{call} takes no arguments")
        return
    if len(args) != 3 or any(abs(a) > EULER_LIMIT for a in args):
        raise PermissionError(f"Euler{args} outside the approved ±{EULER_LIMIT} rad envelope")
