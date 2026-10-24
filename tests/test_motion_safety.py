import threading
import time

import pytest

from is_this_us.artworks.curation import validate
from is_this_us.domain import (
    ExecutionStatus, MotionPlan, Reproducibility, RobotBehavior,
)
from is_this_us.motion.guard import MotionGuard
from is_this_us.motion.planner import SafeMotionPlanner
from is_this_us.robot.base import ALLOWED_SDK_CALLS, BodyState, RobotStatus, steps_for
from is_this_us.robot.mock import MockRobotAdapter
from is_this_us.robot.unitree_go2 import UnitreeGo2Adapter


def plan(b: RobotBehavior) -> MotionPlan:
    return MotionPlan(requested=b, behavior=b, fidelity=Reproducibility.YES, explanation="test")


# ------------------------------------------------------------------ planner

def test_non_reproducible_pose_means_stillness(mock_repo):
    p = SafeMotionPlanner().plan(mock_repo.get("mock:jump"))
    assert p.behavior is RobotBehavior.STILLNESS and p.fidelity is Reproducibility.NO


def test_fallback_chain_when_behavior_disabled(mock_repo):
    planner = SafeMotionPlanner(allowed=frozenset({RobotBehavior.STAND, RobotBehavior.SIT}))
    p = planner.plan(mock_repo.get("mock:feet"))  # lie_down requested
    assert p.behavior is RobotBehavior.SIT and p.fidelity is Reproducibility.APPROXIMATE
    p2 = SafeMotionPlanner(allowed=frozenset()).plan(mock_repo.get("mock:feet"))
    assert p2.behavior is RobotBehavior.STILLNESS


def test_no_painting_means_stillness():
    assert SafeMotionPlanner().plan(None).behavior is RobotBehavior.STILLNESS


def test_curated_archive_is_safe(repo):
    assert validate(repo) == []
    for a in repo.all():
        assert a.dog.robot_behavior in set(RobotBehavior)


# ------------------------------------------------------------------ step sequences

@pytest.mark.parametrize("behavior", list(RobotBehavior))
@pytest.mark.parametrize("body", list(BodyState))
def test_steps_only_use_approved_sdk_calls(behavior, body):
    from is_this_us.robot.base import check_args

    for st in steps_for(behavior, body):
        assert st.call in ALLOWED_SDK_CALLS and st.settle > 0
        check_args(st.call, st.args)  # raises if out of envelope


def test_step_sequences_respect_body_state():
    assert [s[0] for s in steps_for(RobotBehavior.SIT, BodyState.LYING)] == ["StandUp", "BalanceStand", "Sit"]
    assert [s[0] for s in steps_for(RobotBehavior.LIE_DOWN, BodyState.SITTING)] == ["RiseSit", "StandDown"]
    assert steps_for(RobotBehavior.SIT, BodyState.SITTING) == []
    assert [s[0] for s in steps_for(RobotBehavior.GREET, BodyState.SITTING)][-1] == "Hello"


def test_go2_adapter_refuses_unapproved_calls():
    adapter = UnitreeGo2Adapter()

    class FakeSport:
        def BackFlip(self):
            raise AssertionError("must never be called")

    adapter._sport = FakeSport()
    with pytest.raises(PermissionError):
        adapter._call("BackFlip")
    with pytest.raises(PermissionError):
        adapter._call("Move")


# ------------------------------------------------------------------ guard

class FakeRealRobot(MockRobotAdapter):
    is_real = True

    def __init__(self, **kw):
        super().__init__(time_scale=0.0, **kw)
        self.error_code = 0

    def status(self):
        st = super().status()
        return RobotStatus(**{**st.model_dump(), "is_real": True, "error_code": self.error_code})


def test_real_robot_needs_gates_and_arming():
    robot = FakeRealRobot()
    robot.connect()
    closed = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0)
    ok, why = closed.arm(1)
    assert not ok and "gates" in why
    assert closed.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.SKIPPED
    assert robot.calls == []

    open_ = MotionGuard(robot, hardware_gates_open=True, cooldown_s=0)
    assert open_.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.SKIPPED  # not armed
    assert open_.arm(1)[0]
    assert open_.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.SIMULATED
    assert robot.calls == ["Sit"]


def test_software_stop_latches_until_rearmed():
    robot = FakeRealRobot()
    robot.connect()
    g = MotionGuard(robot, hardware_gates_open=True, cooldown_s=0)
    g.arm(1)
    g.software_stop("test")
    assert "StopMove" in robot.calls and not g.armed
    r = g.execute(plan(RobotBehavior.SIT))
    assert r.status is ExecutionStatus.REJECTED and "re-arm" in r.detail
    assert robot.calls == ["StopMove"]
    g.arm(1)
    assert g.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.SIMULATED


def test_stop_interrupts_running_motion():
    robot = MockRobotAdapter(time_scale=1.0)
    robot.connect()
    robot.body = BodyState.LYING
    g = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0, timeout_s=10)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", g.execute(plan(RobotBehavior.SIT))))
    t.start()
    time.sleep(0.2)
    g.software_stop("visitor")
    t.join(3)
    assert result["r"].status is ExecutionStatus.STOPPED


def test_cooldown_battery_error_and_timeout():
    robot = FakeRealRobot()
    robot.connect()
    g = MotionGuard(robot, hardware_gates_open=True, cooldown_s=60)
    g.arm(1)
    assert g.execute(plan(RobotBehavior.STAND)).status is ExecutionStatus.SIMULATED
    r = g.execute(plan(RobotBehavior.SIT))
    assert r.status is ExecutionStatus.REJECTED and "cooldown" in r.detail

    g2 = MotionGuard(robot, hardware_gates_open=True, cooldown_s=0, min_battery=95)
    g2.arm(1)
    r = g2.execute(plan(RobotBehavior.SIT))
    assert r.status is ExecutionStatus.REJECTED and "battery" in r.detail

    robot.error_code = 7
    g3 = MotionGuard(robot, hardware_gates_open=True, cooldown_s=0)
    g3.arm(1)
    assert "error code" in g3.execute(plan(RobotBehavior.SIT)).detail

    slow = MockRobotAdapter(time_scale=5.0)
    slow.connect()
    slow.body = BodyState.LYING
    g4 = MotionGuard(slow, hardware_gates_open=False, cooldown_s=0, timeout_s=0.3)
    r = g4.execute(plan(RobotBehavior.SIT))
    assert r.status is ExecutionStatus.STOPPED and "timeout" in r.detail


def test_one_motion_at_a_time():
    robot = MockRobotAdapter(time_scale=1.0)
    robot.connect()
    robot.body = BodyState.LYING
    g = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0, timeout_s=10)
    t = threading.Thread(target=g.execute, args=(plan(RobotBehavior.SIT),))
    t.start()
    time.sleep(0.1)
    r = g.execute(plan(RobotBehavior.STAND))
    g.software_stop("cleanup")
    t.join(3)
    assert r.status is ExecutionStatus.REJECTED and "another motion" in r.detail


def test_failure_on_real_robot_disarms():
    robot = FakeRealRobot(fail_on={"Sit"})
    robot.connect()
    g = MotionGuard(robot, hardware_gates_open=True, cooldown_s=0)
    g.arm(1)
    assert g.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.FAILED
    assert not g.armed
