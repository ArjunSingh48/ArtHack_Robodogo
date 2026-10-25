"""Fixes for what went wrong on the physical Go2 — all against FAKE bridges (real ZeroMQ, no robot, never the dog):

* hello was performed but answered with code 3104 (go2_main's 3 s SDK timeout) → unknown outcome, later poses
  skipped as "not armed" without saying why;
* a single `move` expires in the bridge's 250 ms watchdog → velocity must stream at ~10 Hz;
* after stopping, the dog stayed in its walking gait → every walk ends with Move(0,0,0) → stop → stop_move.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types

import pytest

zmq = pytest.importorskip("zmq")

from is_this_us.domain import ExecutionStatus, RobotBehavior  # noqa: E402
from is_this_us.motion.guard import MotionGuard  # noqa: E402
from is_this_us.robot import zhaw_bridge as zb  # noqa: E402
from is_this_us.robot.base import BodyState  # noqa: E402

from .test_zhaw_bridge_timing import DEADMAN_S, FakeBridge, connect, fast_steps, max_gap, plan  # noqa: E402,F401


@pytest.fixture
def bridge():
    b = FakeBridge()
    yield b
    b.close()


def actions(bridge):
    return [v[0] for _, c, v in bridge.log if c == "action"]


def cleanup_tail(bridge):
    """The last three robot-facing commands: what the dog got at the end."""
    return [(c, v) for _, c, v in bridge.log if c in ("move", "stop", "action")][-3:]


CLEANUP = [("move", (0.0, 0.0, 0.0)), ("stop", (0.0, 0.0, 0.0)), ("action", ("stop_move",))]


def walk_was_ended(bridge) -> bool:
    """After the last non-zero velocity, the full cleanup came, in order (extra zero-stops are harmless)."""
    cmds = [(c, v) for _, c, v in bridge.log if c in ("move", "stop", "action")]
    last = max(i for i, (c, v) in enumerate(cmds) if c == "move" and any(v))
    after = cmds[last + 1:]
    return any(after[i:i + 3] == CLEANUP for i in range(len(after)))


# ---------------------------------------------------------------- streaming
def test_one_walk_call_streams_at_10_hz_until_the_lease_runs_out(bridge):
    a = connect(bridge)
    try:
        t0 = time.monotonic()
        a.walk(0.2, 0.0, 0.0)
        time.sleep(zb.STREAM_STALE_S + 0.5)
        moves = [c for c in bridge.commands(since=t0) if c[1] == "move" and any(c[2])]
        assert len(moves) >= 8 * zb.STREAM_STALE_S  # ~10 Hz, not one command that the watchdog expires
        assert max_gap(moves) < DEADMAN_S
        assert bridge.velocity_at(t0 + zb.STREAM_STALE_S + 0.45) == (0.0, 0.0, 0.0)  # stale → stopped
    finally:
        a.close()


def test_walk_from_two_threads_never_trips_the_move_guard(bridge):
    """Regression: a shared 'walking' flag let the keep-alive thread's `move` be refused as unauthorised, which
    stopped the dog mid-walk."""
    a = connect(bridge)
    errors = []

    def caller():
        for _ in range(40):
            try:
                a.walk(0.1, 0.0, 0.1)
            except Exception as exc:
                errors.append(exc)
            time.sleep(0.01)

    try:
        ts = [threading.Thread(target=caller) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        assert a.end_walk("test")
    finally:
        a.close()


def test_moves_are_clamped_also_in_the_stream(bridge):
    a = connect(bridge)
    try:
        a.walk(3.0, -2.0, 9.0)
        time.sleep(0.3)
        assert {v for _, c, v in bridge.log if c == "move" and any(v)} == {(zb.MAX_VX, -zb.MAX_VY, zb.MAX_VYAW)}
    finally:
        a.close()


# ---------------------------------------------------------------- code 3104, serialization, arming
def test_3104_latches_no_next_pose_until_the_operator_rearms(fast_steps):
    bridge = FakeBridge(code=3104)
    try:
        a = connect(bridge)
        a.body = BodyState.STANDING
        guard = MotionGuard(a, hardware_gates_open=True, cooldown_s=0)
        seen = []
        guard.listeners.append(lambda g: seen.append(g.state_line()))
        assert guard.arm(1)[0]
        r = guard.execute(plan(RobotBehavior.GREET))
        assert r.status is ExecutionStatus.FAILED and r.diagnostic == "rpc_timeout_unknown"
        assert a.outcome_unknown and "3104" in a.outcome_unknown
        # disarmed — and it says why, also in the refusal of the next pose
        assert not guard.armed and "greet failed" in guard.disarm_reason and "3104" in guard.disarm_reason
        assert seen[-1].startswith("DISARMED") and "greet failed" in seen[-1]
        r2 = guard.execute(plan(RobotBehavior.SIT))
        assert r2.status is ExecutionStatus.SKIPPED and "greet failed" in r2.detail
        # even around the guard, the adapter sends nothing while the outcome is unknown
        assert a.execute(RobotBehavior.SIT).diagnostic == "previous_outcome_unknown"
        assert actions(bridge) == ["hello"]
        # the operator looks at the dog and re-arms: that clears the latch
        bridge.code = 0
        ok, why = guard.arm(1)
        assert ok and "unknown outcome is cleared" in why and a.outcome_unknown is None
        assert guard.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.SUCCESS
        assert actions(bridge)[-1] == "sit"
        assert [e for _, e, _ in guard.history] == ["armed", "disarmed", "armed"]
        a.close()
    finally:
        bridge.close()


def test_slow_actions_are_serialized_never_overlapping(fast_steps):
    bridge = FakeBridge(delay=0.4)
    try:
        a = connect(bridge)
        a.body = BodyState.STANDING
        out = []
        ts = [threading.Thread(target=lambda b=b: out.append(a.execute(b)))
              for b in (RobotBehavior.GREET, RobotBehavior.STRETCH)]
        for t in ts:
            t.start()
            time.sleep(0.05)
        for t in ts:
            t.join(10)
        assert all(r.status is ExecutionStatus.SUCCESS for r in out)
        sent = [t for t, c, _ in bridge.log if c == "action"]
        answered = [t for t, _ in bridge.answered]
        assert len(sent) == 2 and sent[1] >= answered[0]  # the second was sent only after the first was answered
        a.close()
    finally:
        bridge.close()


def test_actions_get_the_full_sdk_timeout(bridge):
    a = connect(bridge)
    assert a.timeout_ms >= (zb.REQUIRED_SDK_TIMEOUT_S + zb.TRANSPORT_MARGIN_S) * 1000 and a.motion_blocker is None
    a.close()


def test_no_walking_while_an_action_runs(fast_steps):
    bridge = FakeBridge(delay=0.5)
    try:
        a = connect(bridge)
        a.body = BodyState.STANDING
        t = threading.Thread(target=lambda: a.execute(RobotBehavior.GREET))
        t.start()
        time.sleep(0.1)
        with pytest.raises(zb.BridgeError, match="action is running"):
            a.walk(0.2, 0, 0)
        t.join(5)
        a.close()
    finally:
        bridge.close()


# ---------------------------------------------------------------- cleanup on every exit
def test_end_walk_is_move0_stop_stopmove_and_verified(bridge):
    a = connect(bridge)
    try:
        a.walk(0.2, 0, 0)
        time.sleep(0.2)
        assert a.gait and a.end_walk("arrived")
        assert cleanup_tail(bridge) == CLEANUP and not a.gait
        time.sleep(0.3)
        assert cleanup_tail(bridge) == CLEANUP  # the keep-alive did not resume
    finally:
        a.close()


def test_end_walk_retries_then_reports_unverified(bridge, caplog, monkeypatch):
    monkeypatch.setattr(zb, "CLEANUP_BACKOFF_S", 0.01)
    a = connect(bridge)
    try:
        a.walk(0.2, 0, 0)
        bridge.fail_next = {"stop_move": 2}
        assert a.end_walk("retry test")  # third attempt confirmed
        assert sum(1 for _, c, v in bridge.log if c == "refused" and v == ("stop_move",)) == 2
        a.walk(0.2, 0, 0)
        bridge.fail_next = {"stop_move": 99}
        assert not a.end_walk("unverified test") and a.gait  # still possibly in gait: said so
        assert "NOT VERIFIED" in caplog.text and "Unitree remote" in caplog.text
    finally:
        bridge.fail_next = {}
        a.close()


@pytest.mark.parametrize("how", ["disarm", "software_stop", "close", "pose"])
def test_every_exit_leaves_the_walking_gait(how, fast_steps):
    bridge = FakeBridge()
    try:
        a = connect(bridge)
        a.body = BodyState.STANDING
        guard = MotionGuard(a, hardware_gates_open=True, cooldown_s=0)
        assert guard.arm(1)[0]
        a.walk(0.2, 0, 0)
        time.sleep(0.2)
        if how == "disarm":
            guard.disarm("operator")
        elif how == "software_stop":
            guard.software_stop("Ctrl+C")
        elif how == "close":
            a.close()
        else:
            assert guard.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.SUCCESS
        tail = [(c, v) for _, c, v in bridge.log if c in ("move", "stop", "action")]
        i = tail.index(("action", ("stop_move",)))
        assert tail[i - 2:i + 1] == CLEANUP and not a.gait
        if how == "pose":
            assert tail[-1] == ("action", ("sit",))  # the pose only after the walk was ended
        a.close()
    finally:
        bridge.close()


def test_approach_exit_ends_with_stopmove():
    from .test_zhaw_bridge_timing import walker

    world, bridge, a, guard, ap = walker()
    try:
        assert ap.approach(side="left", posture="sitting").arrived
        assert cleanup_tail(bridge) == CLEANUP and not a.gait
    finally:
        a.close()
        bridge.close()


# ---------------------------------------------------------------- the bridge launcher's patch (go2_main stubbed)
def test_patched_bridge_sets_10s_reports_it_and_stops_on_exit(monkeypatch):
    from pathlib import Path

    calls = []

    class Sport:
        def SetTimeout(self, t):
            calls.append(("SetTimeout", t))

        def Move(self, *v):
            calls.append(("Move", v))
            return 0

        def StopMove(self):
            calls.append(("StopMove",))
            return 0

    class Robot:
        def init(self):
            self.sport = Sport()
            self.sport.SetTimeout(3.0)  # go2_main's own

        def shutdown(self):
            calls.append(("orig_shutdown",))

    class CommandHandler:
        def _handle_status(self):
            return json.dumps({"ok": True, "msg": "ok", "data": {"speed_level": 1}}).encode()

    pkg = types.ModuleType("go2_bridge")
    robot_mod = types.ModuleType("go2_bridge.robot")
    robot_mod.Robot = Robot
    ch_mod = types.ModuleType("go2_bridge.command_handler")
    ch_mod.CommandHandler = CommandHandler
    pkg.robot, pkg.command_handler = robot_mod, ch_mod
    monkeypatch.setitem(sys.modules, "go2_bridge", pkg)
    monkeypatch.setitem(sys.modules, "go2_bridge.robot", robot_mod)
    monkeypatch.setitem(sys.modules, "go2_bridge.command_handler", ch_mod)
    monkeypatch.delenv("GO2_SPORT_TIMEOUT_S", raising=False)
    path = Path(__file__).resolve().parents[1] / "scripts" / "go2_bridge_patched.py"
    ns: dict = {"__name__": "go2_bridge_patched"}
    exec(compile(path.read_text(), str(path), "exec"), ns)
    ns["apply"]()
    r = Robot()
    r.init()
    assert calls == [("SetTimeout", 3.0), ("SetTimeout", 10.0)]
    status = json.loads(CommandHandler()._handle_status())
    assert status["data"] == {"speed_level": 1, "sport_timeout_s": 10.0, "patched_by": "is_this_us"}
    r.shutdown()
    assert calls[2:] == [("Move", (0.0, 0.0, 0.0)), ("StopMove",), ("orig_shutdown",)]


# ---------------------------------------------------------------- itu web-link (the browser show → the robot)
async def _link_session(bridge, script):
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect as ws_connect
    from websockets.asyncio.server import serve as ws_serve

    from is_this_us.web_link import ORIGINS, WebLink

    a = connect(bridge)
    a.body = BodyState.STANDING
    guard = MotionGuard(a, hardware_gates_open=True, cooldown_s=0)
    guard.arm(0)
    link = WebLink(a, guard, "bridge")
    got = []
    async with ws_serve(link.handle, "127.0.0.1", 0, origins=ORIGINS) as server:
        port = server.sockets[0].getsockname()[1]
        async with ws_connect(f"ws://127.0.0.1:{port}") as ws:
            async def recv_until(pred, timeout=8.0):
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                    got.append(m)
                    if pred(m):
                        return m
            await script(ws, recv_until)
        await asyncio.sleep(0.5)  # the relay notices the disconnect
    return a, guard, got


async def test_web_link_pose_walk_and_cleanup(fast_steps):
    bridge = FakeBridge()

    async def script(ws, recv_until):
        await recv_until(lambda m: m.get("type") == "state" and m["armed"])
        await ws.send(json.dumps({"cmd": "pose", "id": 1, "name": "sit"}))
        assert (await recv_until(lambda m: m.get("id") == 1))["ok"]
        await ws.send(json.dumps({"cmd": "pose", "id": 2, "name": "stand"}))
        assert (await recv_until(lambda m: m.get("id") == 2))["ok"]
        # the real robot never takes velocities from the page's simulated room (it walks by its own camera)
        await ws.send(json.dumps({"cmd": "walk", "vx": 0.25, "vy": 0, "vyaw": 0}))
        assert "own camera" in (await recv_until(lambda m: m.get("type") == "walk_refused"))["msg"]
        await ws.send(json.dumps({"cmd": "walk_end", "id": 3}))
        assert (await recv_until(lambda m: m.get("id") == 3))["ok"]
        await ws.send(json.dumps({"cmd": "pose", "id": 4, "name": "front_flip"}))
        assert not (await recv_until(lambda m: m.get("id") == 4))["ok"]

    try:
        a, guard, got = await _link_session(bridge, script)
        assert actions(bridge)[:3] == ["sit", "stand_up", "balance_stand"]
        assert not [c for c in bridge.commands() if c[1] == "move" and any(c[2])]
        assert ("action", ("stop_move",)) in [(c, v) for _, c, v in bridge.log]
        assert "front_flip" not in actions(bridge)
        # the page went away: disarmed, with the reason
        assert not guard.armed and "page disconnected" in guard.disarm_reason
        a.close()
    finally:
        bridge.close()


async def test_web_link_disconnect_mid_walk_ends_the_walk():
    """Mock mode still relays page velocities (the simulated robot); a closed tab ends that walk too.
    (The real robot's camera-guided walk: tests/test_real_walk_contract.py.)"""
    from is_this_us.robot.mock import MockRobotAdapter
    from is_this_us.web_link import WebLink

    r = MockRobotAdapter(time_scale=0.0)
    r.connect()
    r.body = BodyState.STANDING
    link = WebLink(r, MotionGuard(r, hardware_gates_open=False, cooldown_s=0), "mock")
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect as ws_connect
    from websockets.asyncio.server import serve as ws_serve

    from is_this_us.web_link import ORIGINS

    async with ws_serve(link.handle, "127.0.0.1", 0, origins=ORIGINS) as server:
        port = server.sockets[0].getsockname()[1]
        async with ws_connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()
            for _ in range(5):
                await ws.send(json.dumps({"cmd": "walk", "vx": 0.25, "vy": 0, "vyaw": 0}))
                await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
    assert any(v[0] > 0 for v in r.velocities) and r.velocities[-1] == (0.0, 0.0, 0.0) and not r.gait


async def test_web_link_esc_stops_and_latches(fast_steps):
    bridge = FakeBridge()

    async def script(ws, recv_until):
        await recv_until(lambda m: m.get("type") == "state" and m["armed"])
        await ws.send(json.dumps({"cmd": "stop"}))
        st = await recv_until(lambda m: m.get("type") == "state" and not m["armed"])
        assert "STOP LATCHED" in st["line"] and "Esc" in st["line"]
        await ws.send(json.dumps({"cmd": "pose", "id": 9, "name": "sit"}))
        r = await recv_until(lambda m: m.get("id") == 9)
        assert not r["ok"] and "stop latched" in r["msg"]
        await ws.send(json.dumps({"cmd": "arm"}))
        assert (await recv_until(lambda m: m.get("type") == "state" and m["armed"]))["armed"]

    try:
        a, guard, got = await _link_session(bridge, script)
        assert "sit" not in actions(bridge)
        assert ("action", ("stop_move",)) in [(c, v) for _, c, v in bridge.log]
        a.close()
    finally:
        bridge.close()


async def test_web_link_refuses_other_origins():
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect as ws_connect
    from websockets.asyncio.server import serve as ws_serve
    from websockets.exceptions import InvalidStatus

    from is_this_us.robot.mock import MockRobotAdapter
    from is_this_us.web_link import ORIGINS, WebLink

    r = MockRobotAdapter()
    r.connect()
    link = WebLink(r, MotionGuard(r, hardware_gates_open=True), "mock")
    async with ws_serve(link.handle, "127.0.0.1", 0, origins=ORIGINS) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(InvalidStatus):
            async with ws_connect(f"ws://127.0.0.1:{port}", origin="https://evil.example"):
                pass
        async with ws_connect(f"ws://127.0.0.1:{port}", origin="https://isthisus.vercel.app") as ws:
            assert json.loads(await ws.recv())["type"] == "hello"
