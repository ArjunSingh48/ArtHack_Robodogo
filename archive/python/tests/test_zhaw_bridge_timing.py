"""ZHAW bridge adapter against a FAKE bridge (real ZeroMQ, no robot): action timeouts and continuous walking.

The fake bridge behaves like go2_main's: single-threaded REQ/REP, actions block for `delay` seconds and
return an SDK code, `move`/`stop` are recorded with timestamps (and drive a simulated room).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

zmq = pytest.importorskip("zmq")

from is_this_us.domain import ExecutionStatus, MotionPlan, Reproducibility, RobotBehavior  # noqa: E402
from is_this_us.motion.guard import MotionGuard  # noqa: E402
from is_this_us.robot import zhaw_bridge as zb  # noqa: E402
from is_this_us.robot.base import BodyState  # noqa: E402

DEADMAN_S = 0.25  # go2_main GO2_MOVE_TIMEOUT_MS


class FakeBridge:
    def __init__(self, delay=0.0, code=0, sdk_timeout_s=10.0, world=None):
        self.delay, self.code, self.sdk_timeout_s, self.world = delay, code, sdk_timeout_s, world
        self.log: list[tuple[float, str, tuple]] = []  # (time, cmd, velocity or action)
        ctx = zmq.Context.instance()
        self.sock = ctx.socket(zmq.REP)
        self.port = self.sock.bind_to_random_port("tcp://127.0.0.1")
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        poller = zmq.Poller()
        poller.register(self.sock, zmq.POLLIN)
        while self.running:
            if not dict(poller.poll(50)):
                continue
            msg = json.loads(self.sock.recv())
            cmd, p = msg.get("cmd"), msg.get("params") or {}
            now = time.monotonic()
            if cmd == "status":
                resp = {"ok": True, "msg": "ok", "data": {"sport_timeout_s": self.sdk_timeout_s}}
            elif cmd == "action":
                self.log.append((now, "action", (p.get("name"),)))
                time.sleep(self.delay)
                outcome = "success" if self.code == 0 else "rpc_timeout" if self.code == 3104 else "sdk_error"
                resp = {"ok": self.code == 0, "msg": f"{p.get('name')} executed",
                        "data": {"code": self.code, "outcome": outcome}}
            elif cmd == "move":
                v = (p.get("vx", 0.0), p.get("vy", 0.0), p.get("vyaw", 0.0))
                self.log.append((now, "move", v))
                if self.world is not None:
                    self.world.command(*v)
                resp = {"ok": True, "msg": "velocity updated"}
            elif cmd == "stop":
                self.log.append((now, "stop", (0.0, 0.0, 0.0)))
                if self.world is not None:
                    self.world.command(0.0, 0.0, 0.0)
                resp = {"ok": True, "msg": "stopped"}
            else:
                resp = {"ok": True, "msg": "", "data": {}}
            self.sock.send(json.dumps(resp).encode())

    def close(self):
        self.running = False
        self.thread.join(1)
        self.sock.close(linger=0)

    def velocity_at(self, t: float) -> tuple:
        """What the real bridge would be sending to the robot at time t (deadman included)."""
        last = None
        for when, cmd, v in self.log:
            if cmd in ("move", "stop") and when <= t:
                last = (when, cmd, v)
        if last is None or last[1] == "stop" or t - last[0] > DEADMAN_S:
            return (0.0, 0.0, 0.0)
        return last[2]

    def commands(self, since=0.0):
        return [(t, c, v) for t, c, v in self.log if c in ("move", "stop") and t >= since]


@pytest.fixture
def fast_steps(monkeypatch):
    orig = zb.steps_for
    monkeypatch.setattr(zb, "steps_for", lambda b, body: [st._replace(settle=0.05) for st in orig(b, body)])


def connect(bridge, **kw):
    a = zb.ZhawBridgeAdapter("127.0.0.1", bridge.port, **kw)
    a.connect()
    return a


def plan(b):
    return MotionPlan(requested=b, behavior=b, fidelity=Reproducibility.YES, explanation="test")


# ---------------------------------------------------------------- actions: timeouts and outcomes
def test_transport_waits_longer_than_the_sdk_timeout():
    bridge = FakeBridge(sdk_timeout_s=10.0)
    try:
        a = connect(bridge, sdk_timeout_s=10.0)
        assert a.timeout_ms >= 13000  # 10 s SDK + 3 s margin: we never give up first
        guard = MotionGuard(a, hardware_gates_open=True, timeout_s=12.0)
        a.body = BodyState.UNKNOWN
        assert guard.limit_for(RobotBehavior.GREET) >= a.time_budget(RobotBehavior.GREET) > 3 * 13
        a.close()
    finally:
        bridge.close()


def test_slow_actions_are_confirmed_and_poses_continue(fast_steps):
    """A response after 3.4 s (> the old 3 s SDK timeout, < the new 10 s) is a confirmed success, and
    several poses in a row keep the guard armed."""
    bridge = FakeBridge(delay=3.4)
    try:
        a = connect(bridge)
        a.body = BodyState.STANDING
        guard = MotionGuard(a, hardware_gates_open=True, cooldown_s=0, timeout_s=12.0)
        assert guard.arm(1)[0]
        for b in (RobotBehavior.GREET, RobotBehavior.SIT, RobotBehavior.STAND):
            r = guard.execute(plan(b))
            assert r.status is ExecutionStatus.SUCCESS and r.diagnostic == "confirmed", (b, r)
            assert guard.armed
        assert [x[2][0] for x in bridge.log if x[1] == "action"] == ["hello", "sit", "stand_up", "balance_stand"]
        a.close()
    finally:
        bridge.close()


def test_code_3104_is_unknown_and_disarms(fast_steps):
    bridge = FakeBridge(code=3104)
    try:
        a = connect(bridge)
        a.body = BodyState.STANDING
        guard = MotionGuard(a, hardware_gates_open=True, cooldown_s=0)
        guard.arm(1)
        r = guard.execute(plan(RobotBehavior.GREET))
        assert r.status is ExecutionStatus.FAILED and r.diagnostic == "rpc_timeout_unknown"
        assert "UNKNOWN" in r.detail and not guard.armed
        assert guard.execute(plan(RobotBehavior.STAND)).status is ExecutionStatus.SKIPPED  # until re-armed
        a.close()
    finally:
        bridge.close()


def test_transport_timeout_and_sdk_error_are_distinguished(fast_steps):
    bridge = FakeBridge(delay=1.2)
    try:
        a = connect(bridge, timeout_ms=500)
        a.timeout_ms = 500  # pretend a too-short client timeout
        a._new_socket()
        a.body = BodyState.STANDING
        r = a.execute(RobotBehavior.GREET)
        assert r.status is ExecutionStatus.FAILED and r.diagnostic == "transport_timeout"
        a.close()
    finally:
        bridge.close()
    bridge = FakeBridge(code=4001)
    try:
        a = connect(bridge)
        a.body = BodyState.STANDING
        r = a.execute(RobotBehavior.GREET)
        assert r.status is ExecutionStatus.FAILED and r.diagnostic == "sdk_error"
        a.close()
    finally:
        bridge.close()


def test_no_dangerous_action_is_reachable():
    a = zb.ZhawBridgeAdapter("127.0.0.1", 1)
    for name in ("front_flip", "back_flip", "left_flip", "front_jump", "dance1", "dance2", "hand_stand", "damp",
                 "heart", "recovery_stand"):
        with pytest.raises(PermissionError):
            a.request("action", {"name": name})
    with pytest.raises(PermissionError):
        a.request("move", {"vx": 1.0})  # only through walk(), clamped
    with pytest.raises(PermissionError):
        a.request("speed_level", {"level": 3})
    assert set(zb.STEP_TO_ACTION.values()) | {"stop_move"} == zb.ALLOWED_ACTIONS
    assert zb.clamp_velocity(5, -5, 9) == (zb.MAX_VX, -zb.MAX_VY, zb.MAX_VYAW)


# ---------------------------------------------------------------- walking: continuous, stops on every exit
class SlowEstimator:
    """Pose estimate that takes `delay` seconds (slower than the bridge's 250 ms deadman)."""

    def __init__(self, world, delay=0.3, fail_after=None, hang_after=None):
        self.world, self.delay, self.fail_after, self.hang_after = world, delay, fail_after, hang_after
        self.t0 = time.monotonic()

    def estimate(self, rgb):
        time.sleep(self.delay)
        el = time.monotonic() - self.t0
        if self.fail_after is not None and el > self.fail_after:
            raise RuntimeError("estimator crashed")
        if self.hang_after is not None and el > self.hang_after:
            time.sleep(2.0)
        return self.world.estimate(rgb)


def walker(person=(1.4, 0.25), **est):
    from is_this_us.performance.approach import Approacher, ApproachSettings
    from is_this_us.performance.sim_world import SimWorld

    world = SimWorld(person, "sitting", clock=time.monotonic)
    bridge = FakeBridge(world=world)
    a = connect(bridge)
    a.body = BodyState.STANDING
    guard = MotionGuard(a, hardware_gates_open=True, cooldown_s=0)
    assert guard.arm(1)[0]
    ap = Approacher(a, guard, world.camera, SlowEstimator(world, **est),
                    ApproachSettings(max_seconds=20, beside_m=0.6))
    return world, bridge, a, guard, ap


def max_gap(cmds):
    ts = [t for t, _, _ in cmds]
    return max((b - t for t, b in zip(ts, ts[1:])), default=0.0)


def test_approach_keeps_velocity_fresh_with_slow_perception():
    world, bridge, a, guard, ap = walker()
    try:
        t0 = time.monotonic()
        res = ap.approach(side="left", posture="sitting")
        assert res.arrived, res
        cmds = bridge.commands(since=t0)
        moving = [c for c in cmds if c[1] == "move" and any(c[2])]
        assert len(moving) > 10
        during = [c for c in cmds if c[0] <= moving[-1][0]]
        assert max_gap(during) < DEADMAN_S  # the bridge never had to zero a velocity the approach still wanted
        assert cmds[-1][1] == "stop"  # every completion ends with an explicit stop
        time.sleep(0.8)
        assert all(not any(v) or c == "stop" for t, c, v in bridge.commands(since=cmds[-1][0]))
        # dead reckoning counted the real time each velocity ran: it stays close to the simulated truth
        assert abs(res.robot_pose[0] - world.pose[0]) < 0.25 and abs(res.robot_pose[1] - world.pose[1]) < 0.25
        assert world.min_distance >= ap.s.min_gap_m - 0.2
    finally:
        a.close()
        bridge.close()


def test_go_home_refreshes_continuously_and_stops():
    world, bridge, a, guard, ap = walker()
    try:
        assert ap.approach(side="left", posture="sitting").arrived
        t0 = time.monotonic()
        assert ap.go_home() == "done"
        cmds = bridge.commands(since=t0)
        moving = [c for c in cmds if c[1] == "move" and any(c[2])]
        assert moving and max_gap([c for c in cmds if c[0] <= moving[-1][0]]) < DEADMAN_S
        assert cmds[-1][1] == "stop"
    finally:
        a.close()
        bridge.close()


def test_hanging_client_goes_stale_and_stops():
    """If the approach itself hangs (2 s pose estimate), the keep-alive must not keep the robot walking."""
    world, bridge, a, guard, ap = walker(hang_after=1.0)
    try:
        t = threading.Thread(target=lambda: ap.approach(side="left", posture="sitting"))
        t.start()
        time.sleep(2.2)  # inside the 2 s hang
        moves = [c for c in bridge.commands() if c[1] == "move" and any(c[2])]
        hang_start = moves[-1][0]
        assert bridge.velocity_at(hang_start + zb.STREAM_STALE_S + 0.35) == (0.0, 0.0, 0.0)
        t.join(30)
    finally:
        a.close()
        bridge.close()


@pytest.mark.parametrize("how", ["disarm", "camera", "exception", "cancel"])
def test_every_exit_stops_walking(how):
    world, bridge, a, guard, ap = walker(person=(2.6, 0.2), fail_after=1.0 if how == "exception" else None)
    stop_flag = threading.Event()
    try:
        if how == "camera":
            real = world.camera

            def blind_camera():
                cam = real()

                class Cam:
                    def read(self_):
                        return None if time.monotonic() - t0 > 1.0 else cam.read()

                    def close(self_):
                        pass
                return Cam()
            ap.camera_factory = blind_camera
        out = {}
        t0 = time.monotonic()
        th = threading.Thread(target=lambda: out.setdefault("r", ap.approach(side="left", posture="sitting",
                                                                             should_stop=stop_flag.is_set)))
        th.start()
        time.sleep(1.0)
        event = time.monotonic()
        if how == "disarm":
            guard.disarm("operator")
        if how == "cancel":
            stop_flag.set()
        th.join(10)
        res = out["r"]
        expected = {"disarm": "stopped", "cancel": "stopped", "camera": "camera_lost", "exception": "error"}[how]
        assert res.status == expected, res
        # the robot is commanded to zero quickly after the event (camera: within its blind grace time)
        limit = {"camera": ap.s.camera_lost_seconds + 0.6}.get(how, 0.7)
        assert bridge.velocity_at(event + limit) == (0.0, 0.0, 0.0)
        assert bridge.commands()[-1][1] == "stop"
        time.sleep(0.8)
        assert bridge.velocity_at(time.monotonic()) == (0.0, 0.0, 0.0)
    finally:
        a.close()
        bridge.close()
