"""Adapter for the ZHAW Physical AI Go2 bridge (github.com/zhaw-physical-ai/go2_main).

That bridge (`python -m go2_bridge`) owns the robot's DDS connection and exposes:
* REQ/REP JSON on port 5555: {"cmd": "action", "params": {"name": "sit"}} → {"ok", "msg", "data": {"code"}}
* PUB on port 5556: multipart [b"camera", jpeg] at ~10 FPS

Using it means our project needs no Unitree SDK or CycloneDDS at all, and only one
process ever talks to the robot. Protocol checked against go2_main commit 6bca98f.

What the bridge does NOT offer (so we disable it): `Euler` (our look_aside / peer poses),
battery and error-code readings. The operator must watch the battery in the Unitree app.
Only these bridge actions are ever sent: stand_up, stand_down, balance_stand, sit, hello,
stretch, stop_move (+ the "stop" command). Never flips, jumps, dances, handstand, damp or move.
"""

from __future__ import annotations

import logging
import threading
import time

from ..domain import ExecutionResult, ExecutionStatus, RobotBehavior
from .base import BodyState, RobotStatus, steps_for

log = logging.getLogger(__name__)

# our SDK step name -> bridge action name (go2_bridge/protocol.py ACTION_REGISTRY)
STEP_TO_ACTION = {
    "StandUp": "stand_up",
    "StandDown": "stand_down",
    "BalanceStand": "balance_stand",
    "Sit": "sit",
    "RiseSit": "stand_up",  # the bridge has no RiseSit; its stand_up rises from sitting or lying
    "Hello": "hello",
    "Stretch": "stretch",
}
ALLOWED_ACTIONS = frozenset(STEP_TO_ACTION.values()) | {"stop_move"}
UNSUPPORTED_BEHAVIORS = frozenset({RobotBehavior.LOOK_ASIDE, RobotBehavior.PEER})  # need Euler


class BridgeError(RuntimeError):
    pass


class ZhawBridgeAdapter:
    is_real = True
    reports_battery = False
    unsupported_behaviors = UNSUPPORTED_BEHAVIORS

    def __init__(self, host: str = "localhost", cmd_port: int = 5555, timeout_ms: int = 8000):
        self.endpoint = f"tcp://{host}:{cmd_port}"
        self.timeout_ms = timeout_ms
        self.body = BodyState.UNKNOWN
        self._ctx = None
        self._sock = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = False
        self._last_status: dict = {}
        self.sent: list[dict] = []

    # ------------------------------------------------------------------ transport
    def _new_socket(self):
        import zmq

        if self._sock is not None:
            self._sock.close(linger=0)
        self._sock = self._ctx.socket(zmq.REQ)
        self._sock.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._sock.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.connect(self.endpoint)

    def request(self, cmd: str, params: dict | None = None) -> dict:
        import zmq

        if cmd == "action" and (params or {}).get("name") not in ALLOWED_ACTIONS:
            raise PermissionError(f"bridge action {(params or {}).get('name')!r} is not in the approved vocabulary")
        if cmd not in {"action", "stop", "status", "list_actions"}:
            raise PermissionError(f"bridge command {cmd!r} is not used by this installation")
        msg = {"cmd": cmd, **({"params": params} if params else {})}
        with self._lock:
            if self._sock is None:
                raise BridgeError("not connected")
            try:
                self._sock.send_json(msg)
                self.sent.append(msg)
                return self._sock.recv_json()
            except zmq.ZMQError as exc:
                self._new_socket()  # REQ sockets are unusable after a timeout
                raise BridgeError(f"bridge did not answer {cmd!r}: {exc}") from exc

    # ------------------------------------------------------------------ adapter API
    def connect(self) -> None:
        import zmq

        self._ctx = zmq.Context.instance()
        self._new_socket()
        resp = self.request("status")
        if not resp.get("ok"):
            raise BridgeError(f"bridge status not ok: {resp}")
        self._last_status = resp.get("data") or {}
        self._connected = True
        log.info("connected to ZHAW Go2 bridge at %s (%s)", self.endpoint, self._last_status)

    def list_actions(self) -> list[str]:
        return list((self.request("list_actions").get("data") or {}).get("actions", []))

    def status(self) -> RobotStatus:
        alive = False
        if self._connected:
            try:
                resp = self.request("status")
                alive = bool(resp.get("ok"))
                self._last_status = resp.get("data") or {}
            except BridgeError:
                alive = False
        return RobotStatus(connected=alive, is_real=True, model="go2 via ZHAW bridge", body=self.body,
                           battery_percent=None, error_code=None, hardware_verified=False,
                           detail=f"bridge {self.endpoint} {'ok' if alive else 'unreachable'}; "
                                  f"battery not reported (watch the Unitree app)")

    def execute(self, behavior: RobotBehavior, should_stop=lambda: False) -> ExecutionResult:
        if behavior in UNSUPPORTED_BEHAVIORS:
            return ExecutionResult(behavior=behavior, status=ExecutionStatus.REJECTED,
                                   detail=f"{behavior} needs Euler, which the ZHAW bridge does not expose")
        self._stop.clear()
        start = time.monotonic()
        done = []
        for st in steps_for(behavior, self.body):
            if self._stop.is_set() or should_stop():
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                       detail=f"stopped before {st.call}", duration_s=time.monotonic() - start)
            action = STEP_TO_ACTION[st.call]
            try:
                resp = self.request("action", {"name": action})
            except BridgeError as exc:
                self.body = BodyState.UNKNOWN
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED, detail=str(exc),
                                       duration_s=time.monotonic() - start)
            code = (resp.get("data") or {}).get("code")
            if not resp.get("ok") or code not in (0, None):
                self.body = BodyState.UNKNOWN
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED,
                                       detail=f"{action}: {resp.get('msg')} (code {code})",
                                       duration_s=time.monotonic() - start)
            done.append(action)
            end = time.monotonic() + st.settle
            while time.monotonic() < end:
                if self._stop.is_set():
                    return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                           detail=f"stopped during {action}", duration_s=time.monotonic() - start)
                time.sleep(0.02)
            self.body = st.body
        return ExecutionResult(behavior=behavior, status=ExecutionStatus.SUCCESS,
                               detail=" → ".join(done) or "nothing to do", duration_s=time.monotonic() - start)

    def stop(self) -> None:
        """Software stop: cancel our sequence, zero velocity, StopMove. NOT an emergency stop."""
        self._stop.set()
        for cmd, params in (("stop", None), ("action", {"name": "stop_move"})):
            try:
                self.request(cmd, params)
            except Exception:  # pragma: no cover
                log.exception("bridge stop failed")

    def close(self) -> None:
        self._connected = False
        if self._sock is not None:
            self._sock.close(linger=0)
            self._sock = None


class ZhawBridgeCameraSource:
    """Latest JPEG frame from the bridge's camera stream (ZMQ SUB, topic b"camera")."""

    def __init__(self, host: str = "localhost", pub_port: int = 5556, timeout_ms: int = 3000):
        import zmq

        self.name = f"zhaw_bridge:{host}:{pub_port}"
        self._sock = zmq.Context.instance().socket(zmq.SUB)
        # CONFLATE does not support multipart messages, so we drain to the newest frame instead
        self._sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._sock.setsockopt(zmq.RCVHWM, 4)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.subscribe(b"camera")
        self._sock.connect(f"tcp://{host}:{pub_port}")

    def read(self):
        import zmq

        from ..vision.frame_sources import Frame, decode_image_bytes

        try:
            parts = self._sock.recv_multipart()
        except zmq.Again:
            log.warning("no camera frame from the bridge")
            return None
        while True:  # skip stale frames queued while we were busy
            try:
                parts = self._sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
        if len(parts) != 2 or parts[0] != b"camera" or not parts[1]:
            return None
        return Frame(decode_image_bytes(parts[1]), self.name)

    def close(self) -> None:
        self._sock.close(linger=0)
