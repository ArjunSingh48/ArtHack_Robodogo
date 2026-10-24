"""ZhawBridgeAdapter against a FAKE go2_bridge speaking the real protocol over real ZeroMQ
sockets on localhost. Protocol and action names mirror zhaw-physical-ai/go2_main @ 6bca98f
(go2_bridge/protocol.py, command_handler.py, camera_publisher.py)."""

from __future__ import annotations

import io
import socket
import threading
import time

import pytest

zmq = pytest.importorskip("zmq")

from is_this_us.domain import ExecutionStatus, MotionPlan, Reproducibility, RobotBehavior  # noqa: E402
from is_this_us.motion.guard import MotionGuard  # noqa: E402
from is_this_us.motion.planner import SafeMotionPlanner  # noqa: E402
from is_this_us.robot.base import BodyState  # noqa: E402
from is_this_us.robot.zhaw_bridge import (  # noqa: E402
    ALLOWED_ACTIONS, BridgeError, ZhawBridgeAdapter, ZhawBridgeCameraSource,
)

# go2_bridge/protocol.py ACTION_REGISTRY keys at commit 6bca98f
GO2_MAIN_ACTIONS = ["stand_up", "stand_down", "balance_stand", "recovery_stand", "sit", "hello", "stretch",
                    "dance1", "dance2", "heart", "front_flip", "front_jump", "back_flip", "left_flip",
                    "hand_stand", "damp", "stop_move"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeBridge:
    def __init__(self, fail: dict[str, int] | None = None):
        self.cmd_port, self.pub_port = free_port(), free_port()
        self.received: list[dict] = []
        self.fail = fail or {}
        self._run = True
        ctx = zmq.Context.instance()
        self.rep = ctx.socket(zmq.REP)
        self.rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        self.pub = ctx.socket(zmq.PUB)
        self.pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        self.t1 = threading.Thread(target=self._serve, daemon=True)
        self.t2 = threading.Thread(target=self._camera, daemon=True)
        self.t1.start()
        self.t2.start()

    def _serve(self):
        poller = zmq.Poller()
        poller.register(self.rep, zmq.POLLIN)
        while self._run:
            if not dict(poller.poll(100)):
                continue
            req = self.rep.recv_json()
            self.received.append(req)
            cmd, params = req.get("cmd"), req.get("params", {})
            if cmd == "status":
                resp = {"ok": True, "msg": "ok", "data": {"obstacle_avoidance": True, "speed_level": 1, "light_on": False}}
            elif cmd == "list_actions":
                resp = {"ok": True, "msg": "actions", "data": {"actions": GO2_MAIN_ACTIONS}}
            elif cmd == "action":
                name = params.get("name")
                if name not in GO2_MAIN_ACTIONS:
                    resp = {"ok": False, "msg": f"unknown action: {name}", "data": None}
                else:
                    resp = {"ok": True, "msg": f"{name} executed", "data": {"code": self.fail.get(name, 0)}}
            elif cmd == "stop":
                resp = {"ok": True, "msg": "stopped", "data": None}
            else:
                resp = {"ok": False, "msg": f"unknown command: {cmd}", "data": None}
            self.rep.send_json(resp)

    def _camera(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (120, 90, 200)).save(buf, format="JPEG")
        jpeg = buf.getvalue()
        while self._run:
            self.pub.send_multipart([b"camera", jpeg])
            time.sleep(0.05)

    def actions(self):
        return [r["params"]["name"] for r in self.received if r["cmd"] == "action"]

    def close(self):
        self._run = False
        self.t1.join(1)
        self.t2.join(1)
        self.rep.close(linger=0)
        self.pub.close(linger=0)


@pytest.fixture
def bridge():
    b = FakeBridge()
    yield b
    b.close()


@pytest.fixture
def fast_steps(monkeypatch):
    from is_this_us.robot import base

    orig = base.steps_for
    monkeypatch.setattr("is_this_us.robot.zhaw_bridge.steps_for",
                        lambda beh, body: [st._replace(settle=0.01) for st in orig(beh, body)])


def adapter(b, **kw):
    a = ZhawBridgeAdapter("127.0.0.1", b.cmd_port, **kw)
    a.connect()
    return a


def test_our_actions_exist_in_go2_main():
    assert ALLOWED_ACTIONS <= set(GO2_MAIN_ACTIONS)
    for dangerous in ("front_flip", "back_flip", "left_flip", "front_jump", "hand_stand", "dance1", "dance2", "damp"):
        assert dangerous not in ALLOWED_ACTIONS


def test_connect_status_and_list(bridge):
    a = adapter(bridge)
    st = a.status()
    assert st.connected and st.is_real and st.battery_percent is None and "battery" in st.detail
    assert set(a.list_actions()) == set(GO2_MAIN_ACTIONS)
    a.close()


def test_sequences_map_to_bridge_actions(bridge, fast_steps):
    a = adapter(bridge)
    a.body = BodyState.LYING
    assert a.execute(RobotBehavior.SIT).status is ExecutionStatus.SUCCESS
    assert bridge.actions() == ["stand_up", "balance_stand", "sit"] and a.body is BodyState.SITTING
    assert a.execute(RobotBehavior.LIE_DOWN).status is ExecutionStatus.SUCCESS
    assert bridge.actions()[-2:] == ["stand_up", "stand_down"]  # RiseSit → stand_up on the bridge
    assert a.execute(RobotBehavior.GREET).status is ExecutionStatus.SUCCESS
    assert bridge.actions()[-1] == "hello"
    assert set(bridge.actions()) <= ALLOWED_ACTIONS
    a.close()


def test_never_sends_forbidden_commands(bridge):
    a = adapter(bridge)
    for name in ("back_flip", "dance1", "damp", "hand_stand"):
        with pytest.raises(PermissionError):
            a.request("action", {"name": name})
    for cmd in ("move", "speed_level", "obstacle_avoidance", "light"):
        with pytest.raises(PermissionError):
            a.request(cmd, {})
    assert all(r["cmd"] in {"status", "list_actions"} for r in bridge.received)
    a.close()


def test_euler_poses_rejected_and_planner_disables_them(bridge):
    a = adapter(bridge)
    r = a.execute(RobotBehavior.LOOK_ASIDE)
    assert r.status is ExecutionStatus.REJECTED and "Euler" in r.detail
    planner = SafeMotionPlanner(unavailable=a.unsupported_behaviors)
    assert not planner.approve(RobotBehavior.PEER) and RobotBehavior.PEER not in planner.allowed
    a.close()


def test_failure_code_and_stop(fast_steps):
    b = FakeBridge(fail={"sit": 3104})
    try:
        a = adapter(b)
        a.body = BodyState.STANDING
        r = a.execute(RobotBehavior.SIT)
        assert r.status is ExecutionStatus.FAILED and "3104" in r.detail and a.body is BodyState.UNKNOWN
        a.stop()
        assert b.received[-2]["cmd"] == "stop" and b.received[-1]["params"]["name"] == "stop_move"
        a.close()
    finally:
        b.close()


def test_guard_with_bridge_arms_without_battery_and_moves(bridge, fast_steps):
    a = adapter(bridge)
    a.body = BodyState.STANDING
    g = MotionGuard(a, hardware_gates_open=True, cooldown_s=0)
    ok, why = g.arm(1)
    assert ok and "Unitree app" in why
    res = g.execute(MotionPlan(requested=RobotBehavior.SIT, behavior=RobotBehavior.SIT,
                               fidelity=Reproducibility.YES, explanation="t"))
    assert res.status is ExecutionStatus.SUCCESS and bridge.actions()[-1] == "sit"
    g.shutdown()
    a.close()


def test_camera_stream(bridge):
    cam = ZhawBridgeCameraSource("127.0.0.1", bridge.pub_port, timeout_ms=2000)
    frame = None
    for _ in range(20):  # PUB/SUB "slow joiner": first frames may be missed
        frame = cam.read()
        if frame is not None:
            break
    cam.close()
    assert frame is not None and frame.image.shape == (48, 64, 3)


def test_bridge_not_running_fails_fast():
    a = ZhawBridgeAdapter("127.0.0.1", free_port(), timeout_ms=300)
    t = time.monotonic()
    with pytest.raises(BridgeError):
        a.connect()
    assert time.monotonic() - t < 2
    a.close()


def test_robot_check_command_against_bridge(bridge, monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace

    from is_this_us import hardware_cli
    from is_this_us.config import Settings

    s = Settings(_env_file=None, robot_model="zhaw_bridge", zhaw_bridge_host="127.0.0.1",
                 zhaw_bridge_cmd_port=bridge.cmd_port, zhaw_bridge_pub_port=bridge.pub_port)
    monkeypatch.setattr(hardware_cli, "get_settings", lambda: s)
    hardware_cli.cmd_robot_check(SimpleNamespace(interface=None, no_camera=False, save_frame=tmp_path / "f.jpg"))
    out = capsys.readouterr().out
    assert "✓ bridge answers" in out and "✓ bridge actions we need" in out and "✓ bridge camera" in out
    assert (tmp_path / "f.jpg").exists()
    assert not [r for r in bridge.received if r["cmd"] == "action"]  # read-only: no motion
