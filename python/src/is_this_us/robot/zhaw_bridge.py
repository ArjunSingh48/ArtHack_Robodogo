"""Adapter for the ZHAW Physical AI Go2 bridge (github.com/zhaw-physical-ai/go2_main).

That bridge (`python -m go2_bridge`) owns the robot's DDS connection and exposes:
* REQ/REP JSON on port 5555: {"cmd": "action", "params": {"name": "sit"}} → {"ok", "msg", "data": {"code"}}
* PUB on port 5556: multipart [b"camera", jpeg] at ~10 FPS

Using it means our project needs no Unitree SDK or CycloneDDS at all, and only one
process ever talks to the robot. Protocol checked against go2_main commit 6bca98f.

go2_main is not ours to change. Stock, it gives the SDK 3 s per call, so a slow gesture (hello ≈ 4 s) is
performed but answered with code 3104 — outcome unknown. `scripts/start_bridge.sh` (in this repository)
starts it through `scripts/go2_bridge_patched.py`, which raises that to 10 s and reports it in `status`
(`sport_timeout_s`). Without that report the adapter connects (status, camera) but refuses to arm.

What the bridge does NOT offer (so we disable it): `Euler` (our look_aside / peer poses),
battery and error-code readings. The operator must watch the battery in the Unitree app.
Only these bridge actions are ever sent: stand_up, stand_down, balance_stand, sit, hello,
stretch, stop_move (+ the "stop" command). Never flips, jumps, dances, handstand or damp.

Actions are serialized: one at a time, each allowed the bridge's full SDK timeout; after an unknown outcome
(code 3104, or no answer) no further action is sent until the operator has looked at the dog and re-armed
(`acknowledge_unknown`).

Walking (`move`) is used only through `walk()`, which clamps every velocity to slow limits and keeps it
alive at 10 Hz while the caller renews it (the bridge zeroes velocity after 250 ms of silence). Every walk
ends with `end_walk()`: Move(0,0,0) → stop → stop_move, each confirmed, retried and logged — without
StopMove the Go2 stays in its walking gait.
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
REQUIRED_SDK_TIMEOUT_S = 10.0  # hello / stretch take ~4 s; go2_main's own 3 s is too short

# Walking keep-alive: the caller decides a velocity at ~8 Hz (camera + pose estimate can be slower), the bridge
# zeroes velocity if nothing new arrives within 250 ms. So the adapter re-sends the CURRENT velocity every
# STREAM_PERIOD_S (10 Hz) — but only while it is fresher than STREAM_STALE_S; after that it sends zero.
STREAM_PERIOD_S = 0.1
STREAM_STALE_S = 1.0
STREAM_GAP_WARN_S = 0.2  # a longer pause between two moves is logged (the bridge's deadman is 250 ms)

# end_walk(): each cleanup command is retried this often before it is reported as unverified
CLEANUP_ATTEMPTS = 3
CLEANUP_BACKOFF_S = 0.2
CLEANUP_DEADLINE_S = 30.0  # the whole cleanup gives up (and says so) after this, e.g. when the bridge is gone

START_BRIDGE_HINT = "start the bridge with python/scripts/start_bridge.sh <interface> (10 s SDK timeout)"


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
                                       f"3104) — outcome UNKNOWN; {START_BRIDGE_HINT}")
    return "sdk_error", f"{action}: {resp.get('msg')} (code {code})"


class ZhawBridgeAdapter:
    is_real = True
    reports_battery = False
    unsupported_behaviors = UNSUPPORTED_BEHAVIORS

    def __init__(self, host: str = "localhost", cmd_port: int = 5555, timeout_ms: int | None = None,
                 sdk_timeout_s: float = REQUIRED_SDK_TIMEOUT_S):
        self.endpoint = f"tcp://{host}:{cmd_port}"
        self.sdk_timeout_s = sdk_timeout_s
        # never time out before the bridge's own SDK call can return
        self.timeout_ms = timeout_ms or int((sdk_timeout_s + TRANSPORT_MARGIN_S) * 1000)
        self._stream_v = (0.0, 0.0, 0.0)
        self._stream_t = 0.0
        self._stream_thread: threading.Thread | None = None
        self._stream_lock = threading.Lock()
        self.stream_log: list[tuple[float, float, float, float]] = []  # (time, vx, vy, vyaw) actually sent
        self.move_latency_max = 0.0  # slowest move answer since the walk started (seconds)
        self._gap_warned = 0.0
        self._walk_t0 = 0.0
        self.body = BodyState.UNKNOWN
        self._ctx = None
        self._sock = None
        self._lock = threading.Lock()  # one request at a time on the REQ socket
        self._action_lock = threading.Lock()  # one action (sequence) at a time
        self._stop = threading.Event()
        self._connected = False
        self._last_status: dict = {}
        self.sent: list[dict] = []
        self.gait = False  # walked since the last verified end_walk(): the Go2 may still be in its walking gait
        self.outcome_unknown: str | None = None  # set by an unknown outcome; cleared by acknowledge_unknown()
        self.motion_blocker: str | None = None  # why this connection must not be armed (e.g. 3 s SDK timeout)
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

    def request(self, cmd: str, params: dict | None = None, *, _move_ok: bool = False) -> dict:
        if cmd == "action" and (params or {}).get("name") not in ALLOWED_ACTIONS:
            raise PermissionError(f"bridge action {(params or {}).get('name')!r} is not in the approved vocabulary")
        if cmd == "move" and not _move_ok:
            raise PermissionError("'move' is only sent through walk() / end_walk() (velocity-clamped)")
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
            self.motion_blocker = ("the bridge does not report its SDK timeout: it is stock go2_main with 3 s, "
                                   "too short for hello/stretch (code 3104) — " + START_BRIDGE_HINT)
            log.warning("%s", self.motion_blocker)
        elif float(bridge_t) < REQUIRED_SDK_TIMEOUT_S:
            self.motion_blocker = (f"the bridge's SDK timeout is {float(bridge_t):.0f} s, under "
                                   f"{REQUIRED_SDK_TIMEOUT_S:.0f} s — " + START_BRIDGE_HINT)
            log.warning("%s", self.motion_blocker)
        else:
            self.motion_blocker = None
        if bridge_t is not None and float(bridge_t) + TRANSPORT_MARGIN_S > self.timeout_ms / 1000:
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

    def acknowledge_unknown(self) -> str | None:
        """The operator has looked at the dog after an unknown outcome: actions may be sent again.
        → what was acknowledged (None if nothing was unknown)."""
        was, self.outcome_unknown = self.outcome_unknown, None
        if was:
            log.warning("unknown outcome acknowledged by the operator: %s", was)
        return was

    def execute(self, behavior: RobotBehavior, should_stop=lambda: False) -> ExecutionResult:
        if behavior in UNSUPPORTED_BEHAVIORS:
            return ExecutionResult(behavior=behavior, status=ExecutionStatus.REJECTED,
                                   detail=f"{behavior} needs Euler, which the ZHAW bridge does not expose")
        with self._action_lock:  # serialized: the next pose waits until this one is confirmed or failed
            if self.outcome_unknown:
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.REJECTED,
                                       diagnostic="previous_outcome_unknown",
                                       detail=f"not sent: the previous action's outcome is unknown "
                                              f"({self.outcome_unknown}) — check the dog, then re-arm")
            if self.gait and not self.end_walk(f"before {behavior.value}"):
                return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED, diagnostic="cleanup_failed",
                                       detail="walking could not be ended (Move 0 → stop → stop_move unverified)")
            return self._execute(behavior, should_stop)

    def _unknown(self, behavior, detail, diagnostic, start) -> ExecutionResult:
        self.body = BodyState.UNKNOWN
        self.outcome_unknown = detail
        log.error("UNKNOWN OUTCOME: %s — no further action until the operator re-arms", detail)
        return ExecutionResult(behavior=behavior, status=ExecutionStatus.FAILED, diagnostic=diagnostic,
                               detail=detail, duration_s=time.monotonic() - start)

    def _execute(self, behavior: RobotBehavior, should_stop) -> ExecutionResult:
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
                return self._unknown(behavior, f"{action}: bridge did not answer within {self.timeout_ms / 1000:.0f} s "
                                               f"— outcome unknown ({exc})", "transport_timeout", start)
            verdict, detail = classify_response(action, resp)
            if verdict == "rpc_timeout_unknown":
                return self._unknown(behavior, detail, verdict, start)
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
        t0 = time.monotonic()
        resp = self.request("move", {"vx": round(vx, 3), "vy": round(vy, 3), "vyaw": round(vyaw, 3)}, _move_ok=True)
        now = time.monotonic()
        self.move_latency_max = max(self.move_latency_max, now - t0)
        if self.stream_log and now - self.stream_log[-1][0] > STREAM_GAP_WARN_S and (vx or vy or vyaw) \
                and now - self._gap_warned > 1.0:
            # the bridge zeroes velocity after 250 ms without a move: a gap this long makes the dog stutter
            log.warning("walking: %.0f ms since the previous move (bridge deadman 250 ms) — the process is too busy",
                        (now - self.stream_log[-1][0]) * 1e3)
            self._gap_warned = now
        self.stream_log.append((now, vx, vy, vyaw))
        if len(self.stream_log) > 5000:
            del self.stream_log[:1000]
        if not resp.get("ok"):
            raise BridgeError(f"move refused: {resp.get('msg')}")

    def walk(self, vx: float, vy: float, vyaw: float) -> None:
        """Set the walking velocity (body frame: vx forward, vy left, vyaw counter-clockwise), clamped.

        Sent at once, then kept alive every STREAM_PERIOD_S by a small thread — but only while the caller keeps
        calling walk(): a velocity older than STREAM_STALE_S is replaced by zero."""
        if self._action_lock.locked():
            raise BridgeError("an action is running: no walking until it is confirmed")
        vx, vy, vyaw = clamp_velocity(vx, vy, vyaw)
        with self._stream_lock:
            if not self.gait:
                self._walk_t0, self.move_latency_max = time.monotonic(), 0.0
            self.gait = True
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
                    log.warning("walking: no new velocity for %.1f s — stopping", STREAM_STALE_S)
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
        """Zero velocity now (and end the keep-alive). The walk may continue: see end_walk()."""
        with self._stream_lock:
            self._stream_t = 0.0
            self._stream_v = (0.0, 0.0, 0.0)
        try:
            self.request("stop")
        except Exception:  # pragma: no cover
            log.exception("bridge halt failed")

    def _cleanup_step(self, name: str, send, deadline: float) -> bool:
        for attempt in range(1, CLEANUP_ATTEMPTS + 1):
            if time.monotonic() > deadline:
                log.error("end walk: %s skipped — cleanup deadline (%.0f s) passed", name, CLEANUP_DEADLINE_S)
                return False
            try:
                resp = send()
                code = (resp.get("data") or {}).get("code")
                if resp.get("ok") and code in (0, None):
                    log.info("end walk: %s confirmed%s", name, f" (attempt {attempt})" if attempt > 1 else "")
                    return True
                log.warning("end walk: %s not confirmed (attempt %d/%d): %s", name, attempt, CLEANUP_ATTEMPTS, resp)
            except Exception as exc:
                log.warning("end walk: %s failed (attempt %d/%d): %s", name, attempt, CLEANUP_ATTEMPTS, exc)
            time.sleep(CLEANUP_BACKOFF_S)
        log.error("end walk: %s NOT VERIFIED after %d attempts", name, CLEANUP_ATTEMPTS)
        return False

    def end_walk(self, reason: str = "walk finished") -> bool:
        """Leave walking for good: Move(0,0,0) → stop → stop_move, each confirmed (bounded retries, logged).
        Without StopMove the Go2 stays in its walking gait. → True when all three were confirmed."""
        with self._stream_lock:
            self._stream_t = 0.0
            self._stream_v = (0.0, 0.0, 0.0)
        log.info("end walk (%s): Move(0,0,0) → stop → stop_move", reason)
        st = self.stream_stats()
        if st["moves"]:
            log.info("walk stream: %d moves in %.1f s (%.1f Hz), longest gap %.0f ms, slowest answer %.0f ms",
                     st["moves"], st["seconds"], st["rate_hz"], st["max_gap_ms"], st["max_latency_ms"])
        deadline = time.monotonic() + CLEANUP_DEADLINE_S
        ok = all([  # a list, not a generator: every step is tried even if one fails
            self._cleanup_step("Move(0,0,0)", lambda: self.request("move", {"vx": 0.0, "vy": 0.0, "vyaw": 0.0},
                                                                     _move_ok=True), deadline),
            self._cleanup_step("stop", lambda: self.request("stop"), deadline),
            self._cleanup_step("stop_move", lambda: self.request("action", {"name": "stop_move"}), deadline),
        ])
        if ok:
            self.gait = False
        else:
            log.error("end walk (%s): cleanup NOT verified — the dog may still be in its walking gait; "
                      "use the Unitree remote", reason)
        return ok

    def stream_stats(self) -> dict:
        """Moves actually sent since the current/last walk started: count, rate, longest gap — the evidence that
        velocity was streamed continuously (and not one command followed by silence)."""
        t0 = self._walk_t0
        times = [t for t, *_ in self.stream_log if t >= t0] if t0 else []
        span = (times[-1] - times[0]) if len(times) > 1 else 0.0
        gaps = [b - a for a, b in zip(times, times[1:])]
        return {"moves": len(times), "seconds": round(span, 2),
                "rate_hz": round((len(times) - 1) / span, 1) if span > 0 else 0.0,
                "max_gap_ms": round(max(gaps) * 1e3, 1) if gaps else 0.0,
                "max_latency_ms": round(self.move_latency_max * 1e3, 1)}

    def stop(self) -> None:
        """Software stop: cancel our sequence, zero velocity, StopMove. NOT an emergency stop."""
        self._stop.set()
        self.end_walk("software stop")

    def close(self) -> None:
        if self.gait and self._sock is not None:
            self.end_walk("closing the connection")
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
        """The newest decodable frame, or None. A broken JPEG (truncated in transit — seen on the Go2) is skipped
        and the next frame is taken, within the same wait; it never raises into the walking loop."""
        import zmq

        from ..vision.frame_sources import Frame, decode_image_bytes

        for attempt in range(3):
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
                continue
            try:
                return Frame(decode_image_bytes(parts[1]), self.name)
            except Exception as exc:  # truncated / corrupt JPEG
                self.bad_frames = getattr(self, "bad_frames", 0) + 1
                if self.bad_frames == 1 or self.bad_frames % 20 == 0:
                    log.warning("robot camera: skipped a broken frame (%s; %d so far)", exc, self.bad_frames)
        return None

    def close(self) -> None:
        self._sock.close(linger=0)
