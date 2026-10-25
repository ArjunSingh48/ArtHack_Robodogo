"""The real-walking fixes, against FAKES only (fake ZeroMQ bridge, simulated room, mocked camera lists) — never the dog.

What the Go2 log showed (Oct 2026):
    approach: arrived (already close to the person — stayed), 0.0 m, pose (0,0,0), person None
    pose 'sit': rejected cooldown (7.5s left)
1. a clipped first detection was reported as "arrived" → the robot never walked;
2. every cooldown retry reset the cooldown → the final pose starved.
Plus: the browser show must ask the Python backend to approach (the robot's camera decides), the photo camera on a
Mac is chosen by name (never the iPhone), and at most one bridge runs.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time

import pytest

from is_this_us.domain import ExecutionStatus, MotionPlan, Reproducibility, RobotBehavior
from is_this_us.motion.guard import MotionGuard
from is_this_us.performance.approach import APPROACH_PHASES, Approacher, ApproachSettings
from is_this_us.robot.base import BodyState
from is_this_us.robot.mock import MockRobotAdapter
from is_this_us.vision.frame_sources import Frame
from is_this_us.vision.synthetic import synthetic_person


def plan(b):
    return MotionPlan(requested=b, behavior=b, fidelity=Reproducibility.YES, explanation="test")


# ================================================================ 1. cooldown starvation
def mock_guard(cooldown=8.0):
    r = MockRobotAdapter(time_scale=0.0)
    r.connect()
    return r, MotionGuard(r, hardware_gates_open=False, cooldown_s=cooldown)


def test_a_cooldown_rejection_never_moves_last_motion_end():
    r, g = mock_guard()
    assert g.execute(plan(RobotBehavior.SIT)).status is ExecutionStatus.SIMULATED
    end = g._last_motion_end
    for _ in range(5):  # what MotionCues.perform used to do every 0.5 s
        res = g.execute(plan(RobotBehavior.GREET))
        assert res.status is ExecutionStatus.REJECTED and res.detail.startswith("cooldown")
        assert g._last_motion_end == end
        g.check(RobotBehavior.GREET)
        g.cooldown_remaining()
    assert g._last_motion_end == end


def test_checks_do_not_extend_the_cooldown(monkeypatch):
    r, g = mock_guard(cooldown=8.0)
    t = [100.0]
    monkeypatch.setattr("is_this_us.motion.guard.time.monotonic", lambda: t[0])
    g.execute(plan(RobotBehavior.SIT))
    t[0] += 5.0
    for _ in range(10):
        g.execute(plan(RobotBehavior.GREET))
    assert g.cooldown_remaining() == pytest.approx(3.0)
    t[0] += 3.01
    assert g.execute(plan(RobotBehavior.GREET)).status is ExecutionStatus.SIMULATED


def test_redundant_stand_is_not_sent_and_starts_no_cooldown():
    r, g = mock_guard()
    r.body = BodyState.STANDING
    n = len(r.executed) if hasattr(r, "executed") else None
    res = g.execute(plan(RobotBehavior.STAND))
    assert res.status is ExecutionStatus.SUCCESS and res.diagnostic == "noop" and "nothing sent" in res.detail
    assert g._last_motion_end == 0.0 and g.cooldown_remaining() == 0.0
    if n is not None:
        assert len(r.executed) == n


def test_perform_waits_out_the_cooldown_once_then_executes():
    from is_this_us.performance.motion import MotionCues

    r, g = mock_guard(cooldown=1.0)
    cues = MotionCues(g, None, enabled=True)
    cues.planner = type("P", (), {"allowed": set(RobotBehavior)})()
    g.execute(plan(RobotBehavior.GREET))
    waits = []
    t0 = time.monotonic()
    res = cues.perform("sit", on_wait=waits.append)
    assert res.status is ExecutionStatus.SIMULATED and cues.last_perform == "waited+executed"
    assert 0.8 < time.monotonic() - t0 < 2.0
    assert waits and 0.5 < waits[0] <= 1.0  # the operator is told how long ("waiting 0.9 s …")


def test_stop_while_waiting_sends_nothing():
    from is_this_us.performance.motion import MotionCues

    r, g = mock_guard(cooldown=2.0)
    cues = MotionCues(g, None, enabled=True)
    cues.planner = type("P", (), {"allowed": set(RobotBehavior)})()
    g.execute(plan(RobotBehavior.GREET))
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    end = g._last_motion_end
    res = cues.perform("sit", should_stop=stop.is_set)
    assert res.status is ExecutionStatus.STOPPED and cues.last_perform == "cancelled"
    assert g._last_motion_end == end and r.body is not BodyState.SITTING


# ================================================================ 2. target acquisition (no false "arrived")
class ScriptedRoom:
    """A camera + estimator that shows what a test wants: a person box per frame (or nobody / no frame)."""

    def __init__(self, frames):
        self.frames = list(frames)  # each: None (no frame) | "nobody" | (posture, scale, shift_x, dy)
        self.i = 0

    def camera(self):
        room = self

        class Cam:
            def read(self):
                f = room.frames[min(room.i, len(room.frames) - 1)]
                room.i += 1
                room.current = f
                return None if f is None else Frame(image=None, source="test")

            def close(self):
                pass
        return Cam()

    def estimate(self, _img):
        f = self.current
        if f == "nobody" or f is None:
            return []
        posture, scale, shift_x, dy = f
        p = synthetic_person(posture, scale=scale, shift_x=shift_x)
        if dy:
            p.xy[:, 1] += dy
        return [p]


def approacher_for(room, **kw):
    r = MockRobotAdapter(time_scale=0.0)
    r.connect()
    r.body = BodyState.STANDING
    g = MotionGuard(r, hardware_gates_open=False, cooldown_s=0)
    t = [0.0]
    s = ApproachSettings(**{"max_seconds": 30, "unreliable_seconds": 1.0, **kw})
    a = Approacher(r, g, room.camera, room, s, clock=lambda: t[0], sleep=lambda d: t.__setitem__(0, t[0] + d))
    return a, r


def forward(robot):
    return [v for v in robot.velocities if v[0] > 0]


def clipped_view():
    return ("standing", 2.4, 0.0, 0.0)  # far too tall for the image: head and feet cut off


def test_clipped_first_detection_without_memory_is_target_unreliable_not_arrived():
    room = ScriptedRoom([clipped_view()] * 50)
    a, r = approacher_for(room)
    res = a.approach(side="right", posture="standing")
    assert res.status == "target_unreliable" and not res.arrived and res.person_at is None
    assert "reframe" in res.detail and res.diagnostics["clipped"] is True and res.diagnostics["bbox"]
    assert not forward(r)  # never walked on it


def test_clipped_then_full_body_walks():
    full = ("standing", 1.0, 0.0, 0.0)
    room = ScriptedRoom([clipped_view()] * 3 + [full] * 400)
    a, r = approacher_for(room)
    res = a.approach(side="left", posture="standing")
    assert res.person_at is not None and forward(r)  # it waited, then saw the whole person and walked
    assert res.status in ("arrived", "lost", "timeout", "too_far")


def test_clipped_after_valid_memory_uses_the_memory():
    from is_this_us.performance.sim_world import SimWorld

    # a real-ish walk in the simulated room, where the person fills the frame (clipped) once close
    t = [0.0]
    world = SimWorld((1.4, 0.2), "sitting", clock=lambda: t[0])
    r = MockRobotAdapter(time_scale=0.0)
    r.connect()
    r.world = world
    r.body = BodyState.STANDING
    g = MotionGuard(r, hardware_gates_open=False, cooldown_s=0)
    a = Approacher(r, g, world.camera, world, ApproachSettings(max_seconds=60), clock=lambda: t[0],
                   sleep=lambda d: t.__setitem__(0, t[0] + d))
    res = a.approach(side="left", posture="sitting")
    assert res.arrived and res.person_at is not None and res.travelled_m > 0.3


def test_no_person_is_not_found_and_never_arrived():
    a, r = approacher_for(ScriptedRoom(["nobody"] * 500))
    res = a.approach(side="left", posture="sitting")
    assert res.status == "not_found" and res.person_at is None and not forward(r)


def test_delayed_or_missing_frames_stop_instead_of_walking_blind():
    a, r = approacher_for(ScriptedRoom([None] * 100))
    res = a.approach(side="left", posture="sitting")
    assert res.status == "camera_lost" and not forward(r)


def test_person_near_the_horizontal_edge_turns_first():
    room = ScriptedRoom([("standing", 0.6, 280.0, 0.0)] * 400)  # far right of the image
    a, r = approacher_for(room)
    res = a.approach(side="left", posture="standing")
    assert res.person_at is not None
    first = r.velocities[0]
    assert first[2] < 0  # turns right towards them before going forward


def test_full_body_at_about_two_metres_walks():
    from is_this_us.performance.sim_world import SimWorld

    t = [0.0]
    world = SimWorld((2.0, 0.0), "standing", clock=lambda: t[0])
    r = MockRobotAdapter(time_scale=0.0)
    r.connect()
    r.world = world
    r.body = BodyState.STANDING
    g = MotionGuard(r, hardware_gates_open=False, cooldown_s=0)
    a = Approacher(r, g, world.camera, world, ApproachSettings(max_seconds=60), clock=lambda: t[0],
                   sleep=lambda d: t.__setitem__(0, t[0] + d))
    res = a.approach(side="right", posture="standing")
    assert res.arrived and res.person_at is not None and 1.6 < math.hypot(*res.person_at) < 2.4
    assert res.moves > 10


def test_no_position_can_never_be_an_arrival():
    """The last line of every approach: an 'arrived' without a person position is corrected, whatever set it."""
    from is_this_us.performance.approach import ApproachResult, validate_arrival

    bad = validate_arrival(ApproachResult("arrived", "already close to the person — stayed"))
    assert bad.status == "target_unreliable" and not bad.arrived
    good = validate_arrival(ApproachResult("arrived", "left of the person", person_at=(1.0, 0.2)))
    assert good.arrived


# ================================================================ 3. the real adapter path (fake ZeroMQ bridge)
zmq = pytest.importorskip("zmq")
from tests.test_zhaw_bridge_timing import FakeBridge, connect  # noqa: E402

CLEANUP = [("move", (0.0, 0.0, 0.0)), ("stop", (0.0, 0.0, 0.0)), ("action", ("stop_move",))]


def tail(bridge, n=3):
    return [(c, v) for _, c, v in bridge.log if c in ("move", "stop", "action")][-n:]


def real_walker(person=(1.3, 0.2), cooldown=0.0, estimator=None, camera=None, posture="sitting"):
    from is_this_us.performance.sim_world import SimWorld

    world = SimWorld(person, posture, clock=time.monotonic)
    bridge = FakeBridge(world=world)
    a = connect(bridge)
    a.body = BodyState.STANDING
    guard = MotionGuard(a, hardware_gates_open=True, cooldown_s=cooldown)
    assert guard.arm(1)[0]
    ap = Approacher(a, guard, camera or world.camera, estimator or world, ApproachSettings(max_seconds=25, speed=0.2))
    return world, bridge, a, guard, ap


def test_real_adapter_streams_many_moves_at_about_10_hz_then_cleans_up():
    world, bridge, a, guard, ap = real_walker()
    try:
        t0 = time.monotonic()
        res = ap.approach(side="left", posture="sitting")
        assert res.arrived, res
        moving = [x for x in bridge.commands(since=t0) if x[1] == "move" and any(x[2])]
        assert len(moving) > 10  # a sustained series, not one twitch
        span = moving[-1][0] - moving[0][0]
        rate = (len(moving) - 1) / span
        assert 7.0 < rate < 30.0, rate  # approach decisions + the adapter's 10 Hz keep-alive
        assert max(b[0] - a_[0] for a_, b in zip(moving, moving[1:])) < 0.25  # never past the bridge's deadman
        assert tail(bridge) == CLEANUP and not a.gait  # velocity back to zero, stop, stop_move
        st = a.stream_stats()
        assert st["moves"] > 10 and st["rate_hz"] > 7
    finally:
        a.close()
        bridge.close()


def test_real_camera_loss_mid_walk_stops_and_cleans_up():
    from is_this_us.performance.sim_world import SimWorld

    world = SimWorld((2.0, 0.2), "sitting", clock=time.monotonic)
    t0 = time.monotonic()

    class Flaky:
        def read(self):
            return None if time.monotonic() - t0 > 1.5 else world.camera().read()

        def close(self):
            pass

    world_, bridge, a, guard, ap = real_walker(camera=lambda: Flaky())
    bridge.world = world
    ap.estimator = world
    try:
        res = ap.approach(side="left", posture="sitting")
        assert res.status == "camera_lost"
        assert tail(bridge) == CLEANUP and not a.gait
    finally:
        a.close()
        bridge.close()


@pytest.mark.parametrize("how", ["exception", "cancel", "disarm"])
def test_every_exit_of_a_real_walk_ends_with_stopmove(how):
    from is_this_us.performance.sim_world import SimWorld

    world = SimWorld((2.2, 0.2), "sitting", clock=time.monotonic)
    t0 = time.monotonic()

    class Est:
        def estimate(self, img):
            if how == "exception" and time.monotonic() - t0 > 1.2:
                raise RuntimeError("pose model crashed")
            return world.estimate(img)

    _, bridge, a, guard, ap = real_walker(estimator=Est())
    bridge.world = world
    ap.camera_factory = world.camera
    stop = threading.Event()
    if how == "cancel":
        threading.Timer(1.2, stop.set).start()
    if how == "disarm":
        threading.Timer(1.2, lambda: guard.disarm("test")).start()
    try:
        res = ap.approach(side="left", posture="sitting", should_stop=stop.is_set)
        assert res.status in ("error", "stopped"), res
        assert not res.arrived
        time.sleep(0.4)
        assert ("action", ("stop_move",)) in tail(bridge, 6) and not a.gait
        assert not [x for x in bridge.commands(since=time.monotonic() - 0.3) if x[1] == "move" and any(x[2])]
    finally:
        a.close()
        bridge.close()


def test_stand_short_approach_then_sit_after_the_legitimate_wait(fast_steps):
    """stand (already standing: nothing sent) → a short approach → sit: the pose runs, no cooldown starvation."""
    from is_this_us.performance.motion import MotionCues

    world, bridge, a, guard, ap = real_walker(person=(0.9, 0.3), cooldown=1.5)
    cues = MotionCues(guard, None, enabled=True)
    cues.planner = type("P", (), {"allowed": set(RobotBehavior)})()
    try:
        guard.execute(plan(RobotBehavior.GREET))  # a pose just before: the cooldown runs
        assert cues.perform("stand").diagnostic == "noop"  # redundant stand: not sent
        res = ap.approach(side="left", posture="sitting")
        assert res.person_at is not None
        sit = cues.perform("sit")
        assert sit.status is ExecutionStatus.SUCCESS, sit
        acts = [v[0] for _, c, v in bridge.log if c == "action"]
        assert acts[-1] == "sit" and "stop_move" in acts[:-1]  # the walk was ended before the pose
        assert acts.count("stand_up") == 0  # no redundant stand was sent
    finally:
        a.close()
        bridge.close()


@pytest.fixture
def fast_steps(monkeypatch):
    from is_this_us.robot import zhaw_bridge as zb

    orig = zb.steps_for
    monkeypatch.setattr(zb, "steps_for", lambda b, body: [st._replace(settle=0.05) for st in orig(b, body)])


def test_a_broken_jpeg_from_the_robot_camera_is_skipped_not_fatal():
    """Seen on the Go2: 'image file is truncated' ended the approach with an error. Now the next frame is taken."""
    import io

    import numpy as np
    from PIL import Image

    from is_this_us.robot.zhaw_bridge import ZhawBridgeCameraSource

    buf = io.BytesIO()
    Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(buf, format="JPEG")
    good = buf.getvalue()
    pub = zmq.Context.instance().socket(zmq.PUB)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    cam = ZhawBridgeCameraSource("127.0.0.1", port, timeout_ms=1500)
    try:
        time.sleep(0.3)  # SUB connects

        def send():
            time.sleep(0.1)
            pub.send_multipart([b"camera", good[:-40]])  # truncated
            time.sleep(0.05)
            pub.send_multipart([b"camera", good])
        threading.Thread(target=send).start()
        f = cam.read()
        assert f is not None and f.image.shape == (8, 8, 3) and cam.bad_frames == 1
    finally:
        cam.close()
        pub.close(linger=0)


def test_person_cut_off_at_the_side_is_turned_towards_not_walked_to():
    room = ScriptedRoom([("standing", 0.6, -330.0, 0.0)] * 6 + [("standing", 0.6, 0.0, 0.0)] * 400)
    a, r = approacher_for(room)
    res = a.approach(side="left", posture="standing")
    first = r.velocities[:3]
    assert first and all(v[0] == 0 and v[2] > 0 for v in first)  # turns left, on the spot
    assert res.person_at is not None


# ================================================================ 4. one contract for the browser: sim and hardware
async def _session(link, script):
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect as ws_connect
    from websockets.asyncio.server import serve as ws_serve

    from is_this_us.web_link import ORIGINS

    got = []
    async with ws_serve(link.handle, "127.0.0.1", 0, origins=ORIGINS) as server:
        port = server.sockets[0].getsockname()[1]
        async with ws_connect(f"ws://127.0.0.1:{port}") as ws:
            async def recv_until(pred, timeout=30.0):
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                    got.append(m)
                    if pred(m):
                        return m
            await script(ws, recv_until)
        await asyncio.sleep(0.6)
    return got


async def _approach_script(ws, recv_until):
    await recv_until(lambda m: m.get("type") == "state" and m["armed"])
    await ws.send(json.dumps({"cmd": "approach", "id": 1, "side": "left", "posture": "sitting", "facing": "person"}))
    done = await recv_until(lambda m: m.get("id") == 1)
    assert done["ok"] and done["status"] == "arrived" and done["person"] is not None
    await ws.send(json.dumps({"cmd": "pose", "id": 2, "name": "sit"}))
    assert (await recv_until(lambda m: m.get("id") == 2))["ok"]


def _phases(got):
    return [m["phase"] for m in got if m.get("type") == "walk_state"]


async def test_browser_contract_is_the_same_for_simulation_and_hardware():
    from is_this_us.performance.sim_robot import SimRobot
    from is_this_us.web_link import WebLink

    sim = SimRobot(ApproachSettings(speed=0.5, max_seconds=30), person=(1.2, 0.3), pose_time_scale=0.0)
    got_sim = await _session(WebLink(sim.robot, sim.guard, "mock", sim.approacher), _approach_script)

    world, bridge, a, guard, ap = real_walker()
    try:
        got_hw = await _session(WebLink(a, guard, "bridge", ap), _approach_script)
        for got in (got_sim, got_hw):
            ph = _phases(got)
            assert set(ph) <= set(APPROACH_PHASES), ph
            assert "target_acquired" in ph and "walking" in ph and "arrived" in ph and ph[-1] == "final_pose"
            done = next(m for m in got if m.get("id") == 1)
            assert set(done) >= {"ok", "status", "msg", "robot", "person", "phase"}
        acts = [v[0] for _, c, v in bridge.log if c == "action"]
        assert acts[-1] == "sit" and acts[-2] == "stop_move"  # the pose only after the walk was ended
    finally:
        a.close()
        bridge.close()


async def test_browser_disconnect_mid_approach_stops_the_robot():
    from is_this_us.web_link import WebLink

    world, bridge, a, guard, ap = real_walker(person=(2.8, 0.3))

    async def script(ws, recv_until):
        await recv_until(lambda m: m.get("type") == "state" and m["armed"])
        await ws.send(json.dumps({"cmd": "approach", "id": 1, "side": "left", "posture": "sitting"}))
        await recv_until(lambda m: m.get("phase") == "walking")
        # the tab closes while the dog walks

    try:
        await _session(WebLink(a, guard, "bridge", ap), script)
        await asyncio.sleep(0.5)
        assert ("action", ("stop_move",)) in tail(bridge, 6) and not a.gait
        assert not guard.armed and "page disconnected" in guard.disarm_reason
        assert ap.memory.status == "stopped"
    finally:
        a.close()
        bridge.close()


async def test_browser_cancel_stops_the_walk():
    from is_this_us.web_link import WebLink

    world, bridge, a, guard, ap = real_walker(person=(2.8, 0.3))

    async def script(ws, recv_until):
        await recv_until(lambda m: m.get("type") == "state" and m["armed"])
        await ws.send(json.dumps({"cmd": "approach", "id": 1, "side": "left", "posture": "sitting"}))
        await recv_until(lambda m: m.get("phase") == "walking")
        await ws.send(json.dumps({"cmd": "cancel_walk"}))
        done = await recv_until(lambda m: m.get("id") == 1)
        assert not done["ok"] and done["status"] == "stopped"
        assert _phases([done]) == [] and done["phase"] == "stopped"

    try:
        await _session(WebLink(a, guard, "bridge", ap), script)
        assert tail(bridge) == CLEANUP or ("action", ("stop_move",)) in tail(bridge, 6)
    finally:
        a.close()
        bridge.close()


async def test_real_robot_refuses_open_loop_velocities_from_the_page():
    from is_this_us.web_link import WebLink

    world, bridge, a, guard, ap = real_walker()

    async def script(ws, recv_until):
        await recv_until(lambda m: m.get("type") == "state")
        await ws.send(json.dumps({"cmd": "walk", "vx": 0.25, "vy": 0, "vyaw": 0}))
        m = await recv_until(lambda m: m.get("type") == "walk_refused")
        assert "own camera" in m["msg"]

    try:
        await _session(WebLink(a, guard, "bridge", ap), script)
        assert not [x for x in bridge.commands() if x[1] == "move" and any(x[2])]
    finally:
        a.close()
        bridge.close()


async def test_walking_off_says_why():
    from is_this_us.web_link import WebLink

    world, bridge, a, guard, ap = real_walker()

    async def script(ws, recv_until):
        hello = await recv_until(lambda m: m.get("type") == "hello")
        assert "--walk" in hello["walking"]
        await ws.send(json.dumps({"cmd": "approach", "id": 1, "side": "left"}))
        r = await recv_until(lambda m: m.get("id") == 1)
        assert not r["ok"] and r["status"] == "refused"

    try:
        await _session(WebLink(a, guard, "bridge", None, "start itu web-link with --walk"), script)
    finally:
        a.close()
        bridge.close()


# ================================================================ 5. the MacBook camera, by name
from is_this_us.vision.cameras import (CameraInfo, CameraSelectionError, order_like_opencv,  # noqa: E402
                                       resolve)


def devices():
    # (name, uniqueID) — the iPhone sorts FIRST, i.e. it is webcam:0
    return [("FaceTime HD Camera", "47B4B64B70674B9CAD2BAE273A71F4B5"),
            ("Sanny's iPhone Camera", "0A1B2C3D-CONTINUITY"),
            ("Logitech BRIO", "0x14200000046d085e")]


def test_opencv_order_puts_the_iphone_at_index_0_here():
    cams = order_like_opencv(devices(), "avfoundation")
    assert cams[0].name == "Sanny's iPhone Camera" and cams[0].index == 0


def test_macbook_mode_selects_the_builtin_camera_even_when_the_iphone_is_index_0():
    cams = order_like_opencv(devices(), "avfoundation")
    c = resolve("macbook", cams)
    assert c.name == "FaceTime HD Camera" and c.index == 2 and c.index_verified


@pytest.mark.parametrize("name", ["MacBook Pro Camera", "MacBook Air Camera", "FaceTime HD Camera (Built-in)"])
def test_builtin_names(name):
    assert resolve("macbook", [CameraInfo(0, "iPhone Camera"), CameraInfo(1, name)]).index == 1


@pytest.mark.parametrize("bad", ["iPhone Camera", "Sanny's iPhone Desk View Camera", "Continuity Camera",
                                 "MacBook Continuity Camera"])
def test_macbook_mode_never_picks_phone_or_desk_view(bad):
    with pytest.raises(CameraSelectionError, match="built-in MacBook camera was not found"):
        resolve("macbook", [CameraInfo(0, bad), CameraInfo(1, "Logitech BRIO")])


def test_by_name_and_ambiguity():
    cams = order_like_opencv(devices(), "avfoundation")
    assert resolve("name:brio", cams).name == "Logitech BRIO"
    with pytest.raises(CameraSelectionError, match="no camera named"):
        resolve("name:Elgato", cams)
    with pytest.raises(CameraSelectionError, match="several"):
        resolve("name:camera", cams)


def test_named_spec_opens_that_index(monkeypatch):
    from is_this_us.vision import cameras, frame_sources

    monkeypatch.setattr(cameras, "list_cameras", lambda **k: order_like_opencv(devices(), "avfoundation"))
    opened = []
    monkeypatch.setattr(frame_sources, "WebcamSource", lambda index, label="": opened.append((index, label)) or
                        type("S", (), {"name": f"webcam:{index}"})())
    frame_sources.make_frame_source("macbook")
    assert opened == [(2, "FaceTime HD Camera")]


def test_webcam_keeps_only_the_newest_frame():
    import numpy as np

    from is_this_us.vision.frame_sources import WebcamSource

    class Cap:
        def __init__(self):
            self.n = 0

        def isOpened(self):
            return True

        def read(self):
            time.sleep(0.01)
            self.n += 1
            return True, np.full((4, 4, 3), self.n % 255, dtype=np.uint8)

        def release(self):
            pass

    pytest.importorskip("cv2")
    import cv2  # noqa: F401  (imported before the clock starts: a first import takes a moment)
    cap = Cap()
    src = WebcamSource(0, capture=cap)
    try:
        time.sleep(0.3)
        f = src.read()
        assert f is not None and abs(int(f.image[0, 0, 0]) - cap.n % 255) <= 2  # the latest, not a queued one
    finally:
        src.close()


# ================================================================ 6. one bridge, one show
def test_launcher_reuses_a_patched_bridge_and_refuses_a_stock_one(tmp_path):
    from is_this_us.bridge_launcher import BridgeLauncher, LaunchRefused

    started = []
    mk = lambda st, port=False: BridgeLauncher("en6", runtime=tmp_path, popen=lambda *a, **k: started.append(a),  # noqa: E731
                                               status_fn=lambda h, p: st, port_fn=lambda p: port)
    h = mk({"sport_timeout_s": 10.0}).ensure()
    assert not h.started_by_us and not started
    with pytest.raises(LaunchRefused, match="WITHOUT our 10 s timeout"):
        mk({}).ensure()
    with pytest.raises(LaunchRefused, match="taken but nothing answers"):
        mk(None, port=True).ensure()
    assert not started


def test_launcher_starts_the_repo_script_and_stops_it_after_the_show(tmp_path, monkeypatch):
    from is_this_us import bridge_launcher as bl

    answers = iter([None, None, {"sport_timeout_s": 10.0}])
    signals = []

    class Proc:
        pid, returncode = 4242, None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0

    calls = []
    monkeypatch.setattr(bl.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(bl, "pid_alive", lambda pid: False)
    launcher = bl.BridgeLauncher("en6", runtime=tmp_path, popen=lambda argv, **k: calls.append((argv, k)) or Proc(),
                                 status_fn=lambda h, p: next(answers), port_fn=lambda p: False, sleep=lambda s: None)
    h = launcher.ensure()
    assert h.started_by_us
    argv, kw = calls[0]
    assert argv[0] == "bash" and argv[1].endswith("scripts/start_bridge.sh") and argv[2] == "en6"
    assert kw["start_new_session"] and kw["env"]["GO2_SPORT_TIMEOUT_S"] == "10"
    launcher.close()
    assert signals and signals[0] == (4242, bl.signal.SIGINT)  # Ctrl+C first: the bridge's own Move 0 → StopMove


def test_a_second_show_is_refused(tmp_path):
    import os

    from is_this_us.bridge_launcher import LaunchRefused, ShowLock

    (tmp_path / "present.pid").write_text(str(os.getppid()))  # another live process holds it
    with pytest.raises(LaunchRefused, match="already running"):
        with ShowLock(tmp_path):
            pass
    (tmp_path / "present.pid").write_text("999999")  # stale (dead pid): taken over
    with ShowLock(tmp_path):
        assert (tmp_path / "present.pid").read_text() == str(os.getpid())
    assert not (tmp_path / "present.pid").exists()
