"""Unitree Go2 adapter (official `unitree_sdk2py`, high-level SportClient only).

STATUS: HARDWARE-UNVERIFIED. Written against unitreerobotics/unitree_sdk2_python at
its 2026-09-21 HEAD. API names verified in source; behaviour on a physical robot has
NOT been tested by this project yet. See docs/COMPATIBILITY.md.

Compatibility notes:
* The Python SDK talks DDS (CycloneDDS) over Ethernet. In practice this is supported on
  Go2 EDU. AIR/PRO units generally do not expose SDK access without vendor changes —
  confirm your edition and firmware before relying on this adapter.
* Only these calls are used: StandUp, StandDown, BalanceStand, Sit, RiseSit, Hello,
  Stretch, StopMove. Nothing that translates the body (no Move), no flips, jumps,
  pounces, dances or free-gait modes. Enforced by ALLOWED_SDK_CALLS.
* Battery from `rt/lowstate` (LowState_.bms_state.soc); error code / mode from the
  sport state topic (SportModeState_). Topic names are configurable.
* The Python SDK has no audio playback API (VUI client exposes volume/brightness only),
  so the voice is played on an external speaker.
"""

from __future__ import annotations

import logging
import threading
import time

from ..domain import ExecutionResult, ExecutionStatus, RobotBehavior
from .base import ALLOWED_SDK_CALLS, BodyState, RobotStatus, steps_for

log = logging.getLogger(__name__)

FALLBACK_STATE_TOPICS = ("rt/sportmodestate", "rt/lf/sportmodestate")


class UnitreeGo2Adapter:
    is_real = True

    def __init__(self, network_interface: str = "", *, state_topic: str = "rt/sportmodestate",
                 lowstate_topic: str = "rt/lowstate", rpc_timeout: float = 10.0):
        self.network_interface = network_interface
        self.state_topic = state_topic
        self.lowstate_topic = lowstate_topic
        self.rpc_timeout = rpc_timeout
        self.body = BodyState.UNKNOWN
        self._sport = None
        self._connected = False
        self._battery: int | None = None
        self._error_code: int | None = None
        self._last_state_t = 0.0
        self._stop = threading.Event()
        self._subs = []
        self.live_state_topic: str | None = None

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        from .unitree_dds import init_dds

        init_dds(self.network_interface)
        from unitree_sdk2py.core.channel import ChannelSubscriber  # type: ignore
        from unitree_sdk2py.go2.sport.sport_client import SportClient  # type: ignore
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_, SportModeState_  # type: ignore

        sport = SportClient()
        sport.SetTimeout(self.rpc_timeout)
        sport.Init()
        self._sport = sport

        # Topic names differ between models/firmware; listen on all known ones and report the live one.
        topics = list(dict.fromkeys([self.state_topic, *FALLBACK_STATE_TOPICS]))
        for topic in topics:
            sub = ChannelSubscriber(topic, SportModeState_)
            sub.Init(lambda msg, t=topic: self._on_sport_state(msg, t), 10)
            self._subs.append(sub)
        low_sub = ChannelSubscriber(self.lowstate_topic, LowState_)
        low_sub.Init(self._on_low_state, 10)
        self._subs.append(low_sub)
        self._connected = True
        log.info("Go2 adapter connected on interface %r", self.network_interface or "<default>")

    def _on_sport_state(self, msg, topic: str = "") -> None:
        self.live_state_topic = self.live_state_topic or topic
        self._error_code = int(getattr(msg, "error_code", 0))
        self._last_state_t = time.monotonic()

    def _on_low_state(self, msg) -> None:
        try:
            self._battery = int(msg.bms_state.soc)
        except AttributeError:
            pass

    def check_mode(self) -> tuple[int, dict | None]:
        """Read-only: ask the motion switcher which control mode is active.

        Never calls SelectMode/ReleaseMode — changing modes is left to the operator.
        """
        try:
            from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (  # type: ignore
                MotionSwitcherClient,
            )
        except ImportError:
            return -1, None
        msc = MotionSwitcherClient()
        msc.SetTimeout(5.0)
        msc.Init()
        return msc.CheckMode()

    def status(self) -> RobotStatus:
        fresh = (time.monotonic() - self._last_state_t) < 2.0 if self._last_state_t else False
        return RobotStatus(
            connected=self._connected and fresh,
            is_real=True,
            model="go2",
            body=self.body,
            battery_percent=self._battery,
            error_code=self._error_code,
            hardware_verified=False,
            detail="state stream fresh" if fresh else "no recent sport state received",
        )

    # ------------------------------------------------------------------ motion
    def _call(self, name: str) -> int:
        if name not in ALLOWED_SDK_CALLS:
            raise PermissionError(f"SDK call {name!r} is not in the approved vocabulary")
        fn = getattr(self._sport, name)
        code = fn()
        return int(code) if code is not None else 0

    def execute(self, behavior: RobotBehavior, should_stop=lambda: False) -> ExecutionResult:
        if self._sport is None:
            return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED, detail="not connected")
        self._stop.clear()
        start = time.monotonic()
        steps = steps_for(behavior, self.body)
        done = []
        for call, settle, new_body in steps:
            if self._stop.is_set() or should_stop():
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                       detail=f"stopped before {call}", duration_s=time.monotonic() - start)
            code = self._call(call)
            if code != 0:
                self.body = BodyState.UNKNOWN
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED,
                                       detail=f"{call} returned code {code}", duration_s=time.monotonic() - start)
            done.append(call)
            end = time.monotonic() + settle
            while time.monotonic() < end:
                if self._stop.is_set():
                    return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                           detail=f"stopped during {call}", duration_s=time.monotonic() - start)
                time.sleep(0.02)
            self.body = new_body
        return ExecutionResult(behavior=behavior, status=ExecutionStatus.SUCCESS,
                               detail=" → ".join(done) or "nothing to do", duration_s=time.monotonic() - start)

    def stop(self) -> None:
        """Software stop: cancel the sequence and send StopMove. NOT an emergency stop."""
        self._stop.set()
        if self._sport is not None:
            try:
                self._sport.StopMove()
            except Exception:  # pragma: no cover
                log.exception("StopMove failed")

    def close(self) -> None:
        self._connected = False
