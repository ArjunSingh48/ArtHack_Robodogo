"""The browser show (web/, isthisus.vercel.app) → the real Go2: `itu web-link`.

    Chrome ──WebSocket ws://127.0.0.1:8770──► itu web-link ──ZeroMQ :5555──► go2_bridge ──DDS──► Go2

The page runs the show. Opened with `?robot=ws://127.0.0.1:8770`, it asks this relay for HIGH-LEVEL movement and
the Python backend is the only authority over the physical robot: poses go through the same MotionGuard and
ZhawBridgeAdapter as `itu show` (calm actions only, one at a time, confirmed, 10 s SDK timeout; an unknown outcome or
a failure disarms with a stated reason), and walking is the camera-guided `Approacher` — the Go2's own camera locates
the real participant, the adapter streams the velocity at 10 Hz, and every walk ends with Move(0,0,0) → stop →
stop_move. The page's simulated room (its fixed PERSON position) is NEVER used to move the real robot: with a robot
connected, the 3-D dog only draws the state sent back from here. With `--mock`, the same contract is served by the
Python simulation (SimRobot: mock adapter + SimWorld + the same Approacher).

One page drives at a time, and only from the allowed origins. A closed or reloaded page stops any walk, ends the
gait and disarms ("the page disconnected"); the page's Arm button re-arms.

Messages (JSON). Page → relay:
    {"cmd": "approach", "id": 3, "side": "left", "posture": "sitting", "facing": "person"}
                                → live {"type": "walk_state", "phase": …, "robot": [x, y, yaw], "person": [x, y]|null}
                                → {"type": "done", "id": 3, "ok": arrived, "status": "...", "msg": "...", ...}
    {"cmd": "go_home", "id": 4}  (walk back to the start)  → walk_state … → done
    {"cmd": "cancel_walk"}       (End / Pause / a jump on the page: the walk stops, cleanup runs)
    {"cmd": "pose", "id": 7, "name": "sit"}            → {"type": "done", "id": 7, "ok": true, "msg": "..."}
    {"cmd": "walk_end", "id": 8}                       → {"type": "done", "id": 8, "ok": true, "msg": "..."}
    {"cmd": "stop"}  (Esc: software stop, latched)    {"cmd": "arm"}  (the operator checked the dog)
    {"cmd": "walk", "vx", "vy", "vyaw"}  — open-loop velocity from the page: ONLY with --mock (refused with the
                                           real robot, where positions come from the robot's camera, not the page)
Relay → page: {"type": "hello", "mode": "bridge" | "mock", "walking": "on" | reason} and
{"type": "state", "armed": bool, "line": "..."} after every change of the arming state.
Phases (performance/approach.py APPROACH_PHASES): searching, target_acquired, walking, turning, arrived,
target_unreliable, camera_lost, stopped, final_pose, disarmed, error.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import threading

from .domain import ExecutionStatus, MotionPlan, Reproducibility, RobotBehavior

log = logging.getLogger(__name__)

POSES = {RobotBehavior.STAND, RobotBehavior.SIT, RobotBehavior.LIE_DOWN, RobotBehavior.GREET, RobotBehavior.STRETCH}
OK_STATUSES = {ExecutionStatus.SUCCESS, ExecutionStatus.SIMULATED}
ORIGINS = [
    None,  # not a browser (a local test client); browsers always send an Origin
    "https://isthisus.vercel.app",
    re.compile(r"http://(127\.0\.0\.1|localhost)(:\d+)?"),
]


def plan(b: RobotBehavior) -> MotionPlan:
    return MotionPlan(requested=b, behavior=b, fidelity=Reproducibility.YES, explanation="browser show")


class WebLink:
    """One driving page at a time; everything it asks for goes through the guard."""

    def __init__(self, robot, guard, mode: str, approacher=None, walk_off_reason: str = ""):
        self.robot, self.guard, self.mode = robot, guard, mode
        self.approacher = approacher
        self.walk_off_reason = walk_off_reason or ("" if approacher else "walking is off")
        self.move_task: asyncio.Task | None = None
        self.move_stop = threading.Event()
        if approacher is not None:
            approacher.on_state = self._on_walk_state
        self.driver = None
        self.walking = False  # the page is walking (walk_allowed was checked when it started)
        self.walk_refused = False
        self.pose_task: asyncio.Task | None = None
        # walk / walk_end / halt in the order the page sent them, never blocking the event loop
        self.walk_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-walk")
        self.loop: asyncio.AbstractEventLoop | None = None
        guard.listeners.append(self._on_guard)

    # ------------------------------------------------------------------ to the page
    def state(self) -> dict:
        return {"type": "state", "armed": self.guard.armed or not self.robot.is_real, "line": self.guard.state_line()
                if self.robot.is_real else "simulated robot (mock): no arming needed"}

    async def _send(self, msg: dict) -> None:
        ws = self.driver
        if ws is not None:
            try:
                await ws.send(json.dumps(msg))
            except Exception:
                pass

    def _on_guard(self, guard) -> None:  # any thread
        if self.loop is not None:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send(self.state())))

    def _on_walk_state(self, st: dict) -> None:  # the approach thread
        if self.loop is not None:
            msg = {"type": "walk_state", **st}
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send(msg)))

    # ------------------------------------------------------------------ camera-guided walking (the backend decides)
    def _moving(self) -> bool:
        return self.move_task is not None and not self.move_task.done()

    async def approach(self, m: dict) -> dict:
        if self.approacher is None:
            return {"ok": False, "status": "refused", "msg": f"walking: {self.walk_off_reason}"}
        side = str(m.get("side") or "left")
        side = side if side in ("left", "right", "front") else "left"
        posture, facing = str(m.get("posture") or "sitting"), str(m.get("facing") or "")
        if self.walking:
            await self.walk_end({})
        self.move_stop.clear()
        log.info("approach requested by the page: %s of the person (%s, facing %s)", side, posture, facing or "person")
        res = await self._walk_call(lambda: self.approacher.approach(side=side, posture=posture, facing=facing,
                                                                       should_stop=self.move_stop.is_set))
        return {"ok": res.arrived, "status": res.status, "msg": res.detail, **self.approacher.state(
            res, "arrived" if res.arrived else res.status)}

    async def go_home(self, m: dict) -> dict:
        if self.approacher is None:
            return {"ok": False, "status": "refused", "msg": f"walking: {self.walk_off_reason}"}
        self.move_stop.clear()
        r = await self._walk_call(lambda: self.approacher.go_home(should_stop=self.move_stop.is_set))
        return {"ok": r in ("done", "nothing"), "status": r, "msg": f"walk home: {r}"}

    async def cancel_walk(self) -> None:
        self.move_stop.set()
        if self._moving():
            try:
                await asyncio.wait_for(asyncio.shield(self.move_task), 15)
            except (asyncio.TimeoutError, Exception):
                pass

    # ------------------------------------------------------------------ from the page
    async def _walk_call(self, fn, *args):
        return await self.loop.run_in_executor(self.walk_pool, fn, *args)

    async def pose(self, m: dict) -> dict:
        try:
            b = RobotBehavior(str(m.get("name")))
        except ValueError:
            b = None
        if b not in POSES:
            return {"ok": False, "msg": f"{m.get('name')!r} is not a pose of this show"}
        if self._moving():
            return {"ok": False, "msg": "busy: the robot is walking"}
        if self.walking:
            await self.walk_end({})
        if b is not RobotBehavior.STAND:
            await self._send({"type": "walk_state", "phase": "final_pose", "detail": b.value})
        res = await asyncio.to_thread(self.guard.execute, plan(b))
        log.log(logging.INFO if res.status in OK_STATUSES else logging.WARNING, "pose %s: %s %s", b.value,
                res.status.value, res.detail)
        return {"ok": res.status in OK_STATUSES, "msg": f"{b.value}: {res.status.value} — {res.detail}"}

    async def walk(self, m: dict) -> None:
        if self.robot.is_real:
            # the page's simulated room is not the real room: velocities from it never reach the real robot
            if not self.walk_refused:
                self.walk_refused = True
                why = ("the real robot walks only by its own camera — the page asks for 'approach', not velocities")
                log.warning("page velocity refused: %s", why)
                await self._send({"type": "walk_refused", "msg": why})
            return
        if not self.walking:
            if self.walk_refused:
                return
            ok, why = await asyncio.to_thread(self.guard.walk_allowed)
            if not ok:
                self.walk_refused = True
                log.warning("walking refused: %s", why)
                await self._send({"type": "walk_refused", "msg": why})
                return
            self.walking = True
        if self.robot.is_real and not self.guard.armed:  # disarmed mid-walk: the guard already ended it
            return
        try:
            await self._walk_call(self.robot.walk, float(m.get("vx", 0)), float(m.get("vy", 0)),
                                  float(m.get("vyaw", 0)))
        except Exception as exc:
            log.warning("walk failed: %s", exc)
            await self.walk_end({}, reason=f"walk failed: {exc}")

    async def walk_end(self, m: dict, reason: str = "the page's walk finished") -> dict:
        self.walking, self.walk_refused = False, False
        end = getattr(self.robot, "end_walk", None)
        if end is None:
            await self._walk_call(self.robot.halt)
            return {"ok": True, "msg": "halted"}
        ok = await self._walk_call(end, reason)
        if not ok:
            self.guard.disarm(f"walk cleanup (Move 0 → stop → stop_move) not verified after: {reason}")
        return {"ok": ok, "msg": "walk ended (Move 0 → stop → stop_move)" if ok else "walk cleanup NOT verified"}

    async def handle(self, ws) -> None:
        if self.driver is not None:
            await ws.close(1013, "another page is already driving the dog")
            return
        self.driver, self.loop = ws, asyncio.get_running_loop()
        self.walking = self.walk_refused = False
        log.info("page connected: %s", ws.request.headers.get("Origin") or ws.remote_address)
        await self._send({"type": "hello", "mode": self.mode, "walking": "on" if self.approacher else
                          self.walk_off_reason, "detail": getattr(self.robot, "endpoint", "mock robot")})
        await self._send(self.state())
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                    cmd = m.get("cmd")
                    if cmd == "walk":
                        await self.walk(m)
                    elif cmd in ("approach", "go_home"):
                        if self._moving() or (self.pose_task is not None and not self.pose_task.done()):
                            await self._send({"type": "done", "id": m.get("id"), "ok": False, "status": "busy",
                                              "msg": "busy: one movement at a time"})
                        else:
                            fn = self.approach if cmd == "approach" else self.go_home
                            self.move_task = asyncio.create_task(self._reply(m, fn(m)))
                    elif cmd == "cancel_walk":
                        self.move_stop.set()
                    elif cmd == "pose":
                        if self.pose_task is not None and not self.pose_task.done():
                            await self._send({"type": "done", "id": m.get("id"), "ok": False,
                                              "msg": "busy: one pose at a time"})
                        else:
                            self.pose_task = asyncio.create_task(self._reply(m, self.pose(m)))
                    elif cmd == "walk_end":
                        await self._reply(m, self.walk_end(m))
                    elif cmd == "stop":
                        self.walking = False
                        self.move_stop.set()
                        await asyncio.to_thread(self.guard.software_stop, "Esc / Stop on the page")
                    elif cmd == "arm":
                        ok, why = await asyncio.to_thread(self.guard.arm, 0)
                        await self._send({**self.state(), "msg": why})
                    else:
                        log.warning("ignored %r", raw[:200])
                except (ValueError, TypeError, AttributeError) as exc:
                    log.warning("bad message %r: %s", raw[:200], exc)
        finally:
            self.driver = None
            log.warning("page disconnected")
            await self.cancel_walk()  # the approach stops and runs its own Move 0 → stop → stop_move
            if self.walking or getattr(self.robot, "gait", False):
                await self.walk_end({}, reason="the page disconnected")
            if self.robot.is_real and self.guard.armed:
                self.guard.disarm("the page disconnected (closed or reloaded) — press Arm on the page to go on")

    async def _reply(self, m: dict, coro) -> None:
        r = await coro
        await self._send({"type": "done", "id": m.get("id"), **r})


async def serve(link: WebLink, host: str, port: int, origins: list) -> None:
    from websockets.asyncio.server import serve as ws_serve

    async with ws_serve(link.handle, host, port, origins=origins):
        log.info("waiting for the page on ws://%s:%d — open https://isthisus.vercel.app/?robot=ws://%s:%d",
                 host, port, host, port)
        await asyncio.get_running_loop().create_future()


def add_arguments(p) -> None:
    p.add_argument("--mock", action="store_true", help="no robot: a simulated adapter (nothing is sent anywhere)")
    p.add_argument("--i-have-physical-estop", action="store_true",
                   help="required for the real robot: someone holds the Unitree remote")
    p.add_argument("--host", default="127.0.0.1", help="WebSocket address (keep it on localhost)")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--walk", action="store_true",
                   help="let the page ask the robot to walk to the person (camera-guided, needs calibration: "
                        "SHOW_WALK_CALIBRATED=true and the k values)")
    p.add_argument("--allow-origin", action="append", default=[],
                   help="another page origin allowed to drive, e.g. https://my-preview.vercel.app")


def build_real_approacher(settings, robot, guard, *, enabled: bool):
    """The camera-guided Approacher on the real robot — or (None, why not). Same gates as `itu present --walk`."""
    if not enabled:
        return None, "start itu web-link with --walk to let the robot walk to the person"
    if not settings.show_walk_calibrated:
        return None, ("distances are not calibrated — `itu show walk-test --calibrate 2 --posture sitting` (and "
                      "standing), then SHOW_WALK_CALIBRATED=true")
    if not getattr(robot, "can_walk", False):
        return None, "this robot connection cannot walk"
    from .bot.app import build_estimator
    from .performance.approach import Approacher
    from .performance.runtime import WALK_CAMERA_TIMEOUT_MS, approach_settings
    from .vision.frame_sources import make_frame_source

    est = build_estimator(settings)
    if est is None:
        return None, "pose estimation unavailable (`itu setup`)"
    return Approacher(robot, guard, lambda: make_frame_source(settings.show_walk_camera, timeout_ms=WALK_CAMERA_TIMEOUT_MS,
                                                              network_interface=settings.robot_network_interface),
                      est, approach_settings(settings)), ""


def cmd_web_link(args) -> int:
    from .config import get_settings
    from .motion.guard import MotionGuard

    if not args.mock and not args.i_have_physical_estop:
        raise SystemExit("Refusing: the real robot needs --i-have-physical-estop (keep the Unitree remote in your "
                         "hand), or use --mock.")
    approacher, walk_off = None, ""
    if args.mock:  # the same high-level contract, served by the Python simulation
        from .performance.runtime import sim_approach_settings
        from .performance.sim_robot import SimRobot

        s = get_settings()
        sim = SimRobot(sim_approach_settings(s), pose_time_scale=1.0)
        robot, approacher = sim.robot, sim.approacher
    else:
        from .robot.zhaw_bridge import ZhawBridgeAdapter

        s = get_settings()
        robot = ZhawBridgeAdapter(s.zhaw_bridge_host, s.zhaw_bridge_cmd_port, sdk_timeout_s=s.zhaw_bridge_sdk_timeout_s)
        try:
            robot.connect()
        except Exception as exc:
            raise SystemExit(f"cannot reach go2_bridge at {robot.endpoint}: {exc}\n"
                             f"Start it first: python/scripts/start_bridge.sh <interface>   (or use --mock)")
    guard = sim.guard if args.mock else MotionGuard(robot, hardware_gates_open=True, cooldown_s=0)
    if not args.mock:
        approacher, walk_off = build_real_approacher(s, robot, guard, enabled=args.walk)
        print("Walking: " + ("ON — camera-guided (the robot's camera finds the person)" if approacher else
                             f"off — {walk_off}"), flush=True)

    def announce(g) -> None:
        _, event, reason = g.history[-1]
        (log.info if event == "armed" else log.warning)("ROBOT %s: %s", event.upper(), reason)

    guard.listeners.append(announce)
    if robot.is_real:
        ok, why = guard.arm(operator_id=0)
        print(("ARMED: " if ok else "NOT armed: ") + why, flush=True)
    link = WebLink(robot, guard, "mock" if args.mock else "bridge", approacher, walk_off)
    try:
        asyncio.run(serve(link, args.host, args.port, ORIGINS + args.allow_origin))
    except KeyboardInterrupt:
        pass
    finally:
        log.warning("Ctrl+C — stopping the dog")
        guard.software_stop("web link ended (Ctrl+C)")
        guard.shutdown()
        robot.close()
    return 0
