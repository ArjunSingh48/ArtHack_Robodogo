"""Go2 adapter against a FAKE unitree_sdk2py that mirrors the real module paths and
call signatures (read from unitreerobotics/unitree_sdk2_python). This checks our
wiring, not the robot: real behaviour is still hardware-unverified."""

from __future__ import annotations

import sys
import time
import types

import pytest

from is_this_us.domain import ExecutionStatus, RobotBehavior
from is_this_us.robot.base import BodyState


class FakeSport:
    def __init__(self, enableLease=False):
        self.calls, self.codes = [], {}

    def SetTimeout(self, t): self.timeout = t
    def Init(self): pass

    def __getattr__(self, name):
        if name[0].isupper():
            def call(*args):
                self.calls.append(f"{name}{args}" if args else name)
                return self.codes.get(name, 0)
            return call
        raise AttributeError(name)


class FakeSub:
    instances = []

    def __init__(self, topic, typ):
        self.topic, self.typ = topic, typ
        FakeSub.instances.append(self)

    def Init(self, handler=None, queueLen=0):
        self.handler = handler


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeSub.instances = []
    init_calls = []
    sport = FakeSport()

    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    mod("unitree_sdk2py")
    mod("unitree_sdk2py.core")
    mod("unitree_sdk2py.core.channel", ChannelFactoryInitialize=lambda i=0, n=None: init_calls.append(n),
        ChannelSubscriber=FakeSub)
    mod("unitree_sdk2py.go2")
    mod("unitree_sdk2py.go2.sport")
    mod("unitree_sdk2py.go2.sport.sport_client", SportClient=lambda *a, **k: sport)
    mod("unitree_sdk2py.idl")
    mod("unitree_sdk2py.idl.unitree_go")
    mod("unitree_sdk2py.idl.unitree_go.msg")
    mod("unitree_sdk2py.idl.unitree_go.msg.dds_", LowState_=object, SportModeState_=object)

    class MSC:
        def SetTimeout(self, t): pass
        def Init(self): pass
        def CheckMode(self): return 0, {"form": "0", "name": "normal"}
        def SelectMode(self, n): raise AssertionError("must never change modes")

    mod("unitree_sdk2py.comm")
    mod("unitree_sdk2py.comm.motion_switcher")
    mod("unitree_sdk2py.comm.motion_switcher.motion_switcher_client", MotionSwitcherClient=MSC)

    import is_this_us.robot.unitree_dds as dds
    monkeypatch.setattr(dds, "_initialized_iface", None)
    return sport, init_calls


def _feed(adapter, battery=80, error=0, topic="rt/lf/sportmodestate"):
    sub = next(s for s in FakeSub.instances if s.topic == topic)
    sub.handler(types.SimpleNamespace(error_code=error))
    low = next(s for s in FakeSub.instances if s.topic == "rt/lowstate")
    low.handler(types.SimpleNamespace(bms_state=types.SimpleNamespace(soc=battery)))


def test_connect_subscribes_and_reports_live_topic(fake_sdk):
    from is_this_us.robot.unitree_go2 import UnitreeGo2Adapter

    sport, init_calls = fake_sdk
    a = UnitreeGo2Adapter("enp3s0")
    a.connect()
    assert init_calls == ["enp3s0"]
    assert {s.topic for s in FakeSub.instances} == {"rt/sportmodestate", "rt/lf/sportmodestate", "rt/lowstate"}
    assert not a.status().connected  # nothing received yet
    _feed(a)
    st = a.status()
    assert st.connected and st.battery_percent == 80 and st.error_code == 0
    assert a.live_state_topic == "rt/lf/sportmodestate"
    assert a.check_mode() == (0, {"form": "0", "name": "normal"})


def test_dds_initialised_once_for_camera_and_robot(fake_sdk):
    from is_this_us.robot.unitree_dds import init_dds
    from is_this_us.robot.unitree_go2 import UnitreeGo2Adapter

    _, init_calls = fake_sdk
    UnitreeGo2Adapter("en7").connect()
    init_dds("en7")
    assert init_calls == ["en7"]
    with pytest.raises(RuntimeError):
        init_dds("eth1")


def test_execute_uses_only_planned_calls_and_tracks_body(fake_sdk, monkeypatch):
    from is_this_us.robot import base
    from is_this_us.robot.unitree_go2 import UnitreeGo2Adapter

    orig = base.steps_for
    monkeypatch.setattr("is_this_us.robot.unitree_go2.steps_for",
                        lambda beh, body: [st._replace(settle=0.01) for st in orig(beh, body)])
    sport, _ = fake_sdk
    a = UnitreeGo2Adapter()
    a.connect()
    r = a.execute(RobotBehavior.SIT)
    assert r.status is ExecutionStatus.SUCCESS
    assert sport.calls == ["StandUp", "BalanceStand", "Sit"] and a.body is BodyState.SITTING

    sport.codes["RiseSit"] = 3104
    r = a.execute(RobotBehavior.STAND)
    assert r.status is ExecutionStatus.FAILED and "3104" in r.detail and a.body is BodyState.UNKNOWN

    a.stop()
    assert sport.calls[-1] == "StopMove"


def test_euler_pose_is_bounded_and_resets(fake_sdk, monkeypatch):
    from is_this_us.robot import base
    from is_this_us.robot.unitree_go2 import UnitreeGo2Adapter

    orig = base.steps_for
    monkeypatch.setattr("is_this_us.robot.unitree_go2.steps_for",
                        lambda beh, body: [st._replace(settle=0.01) for st in orig(beh, body)])
    sport, _ = fake_sdk
    a = UnitreeGo2Adapter()
    a.connect()
    a.body = BodyState.STANDING
    assert a.execute(RobotBehavior.LOOK_ASIDE).status is ExecutionStatus.SUCCESS
    assert sport.calls == ["BalanceStand", "Euler(0.0, 0.0, 0.25)", "Euler(0.0, 0.0, 0.0)"]
    with pytest.raises(PermissionError):
        a._call("Euler", (0.0, 0.0, 0.6))
    with pytest.raises(PermissionError):
        a._call("Sit", (1.0,))
