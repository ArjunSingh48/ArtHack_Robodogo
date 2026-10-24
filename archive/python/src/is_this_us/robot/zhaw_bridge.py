"""Adapter for the ZHAW Physical AI Go2 bridge (github.com/zhaw-physical-ai/go2_main).

That bridge (`python -m go2_bridge`) owns the robot's DDS connection and exposes:
* REQ/REP JSON on port 5555: {"cmd": "action", "params": {"name": "sit"}} → {"ok", "msg", "data": {"code"}}
* PUB on port 5556: multipart [b"camera", jpeg] at ~10 FPS

Using it means our project needs no Unitree SDK or CycloneDDS at all, and only one
process ever talks to the robot. Protocol checked against go2_main commit 6bca98f.

What the bridge does NOT offer (so we disable it): `Euler` (our look_aside / peer poses),
battery and error-code readings. The operator must watch the battery in the Unitree app.
Only these bridge actions are ever sent: stand_up, stand_down, balance_stand, sit, hello,
stretch, stop_move (+ the "stop" command). Never flips, jumps, dances, handstand or damp.

Walking (`move`) is used ONLY by the show's camera-guided approach, through `walk()`, which
clamps every velocity to slow limits. The bridge zeroes velocity by itself if no new `move`
arrives within its heartbeat timeout, and keeps its obstacle avoidance on by default.
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

# Hard limits for walk(): slow, short steps near people. m/s and rad/s.
MAX_VX, MAX_VY, MAX_VYAW = 0.3, 0.15, 0.6

RPC_ERR_CLIENT_API_TIMEOUT = 3104  # Unitree SDK: the robot did not answer in time — outcome UNKNOWN
TRANSPORT_MARGIN_S = 3.0  # our socket waits this much longer than the bridge's SDK timeout

# Walking keep-alive: the approach decides a velocity ~8 Hz (camera + pose estimate can be slower), the bridge
# zeroes velocity if nothing new arrives within 250 ms. So the adapter re-sends the CURRENT velocity every
# STREAM_PERIOD_S — but only while it is fresher than STREAM_STALE_S; after that it sends zero and stops.
STREAM_PERIOD_S = 0.1
STREAM_STALE_S = 0.6


def clamp_velocity(vx: float, vy: float, vyaw: float) -> tuple[float, float, float]:
    def c(v, m):
        return max(-m, min(m, float(v)))
    return c(vx, MAX_VX), c(vy, MAX_VY), c(vyaw, MAX_VYAW)


class BridgeError(RuntimeError):
    pass


def classify_response(action: str, resp: dict) -> tuple[str, str]:
    """Bridge answer → (confirmed | sdk_error | rpc_timeout_unknown | rejected, operator detail).

    Code 3104 (RPC_ERR_CLIENT_API_TIMEOUT) is NOT treated as success: the robot may or may not have done it."""
    data = resp.get("data") or {}
    code, outcome = data.get("code"), data.get("outcome")
    if resp.get("ok") and code in (0, None) and outcome in (None, "success"):
        return "confirmed", f"{action} confirmed"
    if outcome == "rejected":
        return "rejected", f"{action}: bridge refused — {resp.get('msg')}"
    if code == RPC_ERR_CLIENT_API_TIMEOUT or outcome == "rpc_timeout":
        return "rpc_timeout_unknown", (f"{action}: the robot did not answer within the bridge's SDK timeout (code "
                                       f"3104) — outcome UNKNOWN. Raise GO2_SPORT_TIMEOUT_S on the bridge (10 s).")
    return "sdk_error", f"{action}: {resp.get('msg')} (code {code})"


class ZhawBridgeAdapter:
    is_real = True
    reports_battery = False
    unsupported_behaviors = UNSUPPORTED_BEHAVIORS

    def __init__(self, host: str = "localhost", cmd_port: int = 5555, timeout_ms: int | None = None,
                 sdk_timeout_s: float = 10.0):
        self.endpoint = f"tcp://{host}:{cmd_port}"
        self.sdk_timeout_s = sdk_timeout_s
        # never time out before the bridge's own SDK call can return
        self.timeout_ms = timeout_ms or int((sdk_timeout_s + TRANSPORT_MARGIN_S) * 1000)
        self._stream_v = (0.0, 0.0, 0.0)
        self._stream_t = 0.0
        self._stream_thread: threading.Thread | None = None
        self._stream_lock = threading.Lock()
        self.stream_log: list[tuple[float, float, float, float]] = []  # (time, vx, vy, vyaw) actually sent
        self.body = BodyState.UNKNOWN
        self._ctx = None
        self._sock = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = False
        self._last_status: dict = {}
        self.sent: list[dict] = []
        self._walking = False
        self.can_walk = True
        # longest a velocity can keep running without a new walk(): keep-alive staleness + the bridge's deadman
        self.coast_s = STREAM_STALE_S + 0.25

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
        if cmd == "action" and (params or {}).get("name") not in ALLOWED_ACTIONS:
            raise PermissionError(f"bridge action {(params or {}).get('name')!r} is not in the approved vocabulary")
        if cmd == "move" and not self._walking:
            raise PermissionError("'move' is only sent through walk() (velocity-clamped)")
        if cmd not in {"action", "stop", "status", "list_actions", "move"}:
            raise PermissionError(f"bridge command {cmd!r} is not used by this installation")
        import zmq  # after the permission checks: refusing never needs the transport

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
        bridge_t = self._last_status.get("sport_timeout_s")
        if bridge_t is None:
            log.warning("the bridge does not report its SDK timeout (old go2_main?): slow actions such as hello may "
                        "come back as code 3104 'outcome unknown'. Update go2_main / set GO2_SPORT_TIMEOUT_S=10.")
        elif float(bridge_t) + TRANSPORT_MARGIN_S > self.timeout_ms / 1000:
            self.timeout_ms = int((float(bridge_t) + TRANSPORT_MARGIN_S) * 1000)
            self._new_socket()
            log.info("bridge SDK timeout %.1f s → waiting up to %.1f s per action", bridge_t, self.timeout_ms / 1000)
        log.info("connected to ZHAW Go2 bridge at %s (%s)", self.endpoint, self._last_status)

    def time_budget(self, behavior: RobotBehavior) -> float:
        """Longest an execute() may legitimately take (every step answering at the last moment + settling).
        The MotionGuard never times out before this."""
        return sum(self.timeout_ms / 1000 + st.settle for st in steps_for(behavior, self.body)) + 1.0

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
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED, diagnostic="transport_timeout",
                                       detail=f"{action}: bridge did not answer within {self.timeout_ms / 1000:.0f} s "
                                              f"— outcome unknown ({exc})", duration_s=time.monotonic() - start)
            verdict, detail = classify_response(action, resp)
            if verdict != "confirmed":
                self.body = BodyState.UNKNOWN
                status = ExecutionStatus.REJECTED if verdict == "rejected" else ExecutionStatus.FAILED
                return ExecutionResult(behavior=behavior, status=status, diagnostic=verdict, detail=detail,
                                       duration_s=time.monotonic() - start)
            done.append(action)
            end = time.monotonic() + st.settle
            while time.monotonic() < end:
                if self._stop.is_set():
                    return ExecutionResult(behavior=behavior, status=ExecutionStatus.STOPPED,
                                           detail=f"stopped during {action}", duration_s=time.monotonic() - start)
                time.sleep(0.02)
            self.body = st.body
        return ExecutionResult(behavior=behavior, status=ExecutionStatus.SUCCESS, diagnostic="confirmed",
                               detail=" → ".join(done) or "nothing to do", duration_s=time.monotonic() - start)

    # ------------------------------------------------------------------ walking
    def _send_move(self, vx: float, vy: float, vyaw: float) -> None:
        self._walking = True
        try:
            resp = self.request("move", {"vx": round(vx, 3), "vy": round(vy, 3), "vyaw": round(vyaw, 3)})
        finally:
            self._walking = False
        self.stream_log.append((time.monotonic(), vx, vy, vyaw))
        if not resp.get("ok"):
            raise BridgeError(f"move refused: {resp.get('msg')}")

    def walk(self, vx: float, vy: float, vyaw: float) -> None:
        """Set the walking velocity (body frame: vx forward, vy left, vyaw counter-clockwise), clamped.

        Sent at once, then kept alive every STREAM_PERIOD_S by a small thread — but only while the caller keeps
        calling walk(): a velocity older than STREAM_STALE_S is replaced by zero and the thread ends."""
        vx, vy, vyaw = clamp_velocity(vx, vy, vyaw)
        with self._stream_lock:
            self._stream_v, self._stream_t = (vx, vy, vyaw), time.monotonic()
            if self._stream_thread is None or not self._stream_thread.is_alive():
                self._stream_thread = threading.Thread(target=self._stream, daemon=True, name="walk-keepalive")
                self._stream_thread.start()
        self._send_move(vx, vy, vyaw)

    def _stream(self) -> None:
        while True:
            time.sleep(STREAM_PERIOD_S)
            with self._stream_lock:
                v, t = self._stream_v, self._stream_t
                if t == 0.0:
                    return  # halted
                if time.monotonic() - t > STREAM_STALE_S:  # the caller stopped deciding: stop the robot
                    self._stream_t = 0.0
                    v = None
            try:
                if v is None:
                    self.request("stop")
                    return
                self._send_move(*v)
            except Exception as exc:
                log.warning("walking keep-alive failed (%s) — stopping", exc)
                with self._stream_lock:
                    self._stream_t = 0.0
                try:
                    self.request("stop")
                except Exception:
                    pass
                return

    def halt(self) -> None:
        """Zero velocity now (and end the keep-alive)."""
        with self._stream_lock:
            self._stream_t = 0.0
            self._stream_v = (0.0, 0.0, 0.0)
        try:
            self.request("stop")
        except Exception:  # pragma: no cover
            log.exception("bridge halt failed")

    def stop(self) -> None:
        """Software stop: cancel our sequence, zero velocity, StopMove. NOT an emergency stop."""
        self._stop.set()
        with self._stream_lock:
            self._stream_t = 0.0
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
