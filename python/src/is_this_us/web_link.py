"""The browser show (web/, isthisus.vercel.app) → the real Go2: `itu web-link`.

    Chrome ──WebSocket ws://127.0.0.1:8770──► itu web-link ──ZeroMQ :5555──► go2_bridge ──DDS──► Go2

The page runs the whole show and its simulated dog. Opened with `?robot=ws://127.0.0.1:8770`, it also sends here
what its dog does; this relay passes it through the same MotionGuard and ZhawBridgeAdapter as `itu show`, so the
same rules hold: calm actions only, one at a time and confirmed (10 s SDK timeout), an unknown outcome or a failure
disarms with a stated reason, walking is clamped and kept alive at 10 Hz while the page renews it, and every walk
ends with Move(0,0,0) → stop → stop_move. One page drives at a time, and only from the allowed origins. A closed or
reloaded page ends any walk and disarms ("the page disconnected"); the page's Arm button re-arms.

Messages (JSON). Page → relay:
    {"cmd": "pose", "id": 7, "name": "sit"}            → {"type": "done", "id": 7, "ok": true, "msg": "..."}
    {"cmd": "walk", "vx": 0.25, "vy": 0, "vyaw": 0}    (no answer; renew at ≥ 2 Hz while walking)
    {"cmd": "walk_end", "id": 8}                       → {"type": "done", "id": 8, "ok": true, "msg": "..."}
    {"cmd": "stop"}  (Esc: software stop, latched)    {"cmd": "arm"}  (the operator checked the dog)
Relay → page: {"type": "hello", "mode": "bridge" | "mock", ...} and {"type": "state", "armed": bool, "line": "..."}
after every change of the arming state.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re

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

    def __init__(self, robot, guard, mode: str):
        self.robot, self.guard, self.mode = robot, guard, mode
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
        if self.walking:
            await self.walk_end({})
        res = await asyncio.to_thread(self.guard.execute, plan(b))
        log.log(logging.INFO if res.status in OK_STATUSES else logging.WARNING, "pose %s: %s %s", b.value,
                res.status.value, res.detail)
        return {"ok": res.status in OK_STATUSES, "msg": f"{b.value}: {res.status.value} — {res.detail}"}

    async def walk(self, m: dict) -> None:
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
        await self._send({"type": "hello", "mode": self.mode,
                          "detail": getattr(self.robot, "endpoint", "mock robot")})
        await self._send(self.state())
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                    cmd = m.get("cmd")
                    if cmd == "walk":
                        await self.walk(m)
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
    p.add_argument("--allow-origin", action="append", default=[],
                   help="another page origin allowed to drive, e.g. https://my-preview.vercel.app")


def cmd_web_link(args) -> int:
    from .config import get_settings
    from .motion.guard import MotionGuard

    if not args.mock and not args.i_have_physical_estop:
        raise SystemExit("Refusing: the real robot needs --i-have-physical-estop (keep the Unitree remote in your "
                         "hand), or use --mock.")
    if args.mock:
        from .robot.mock import MockRobotAdapter

        robot = MockRobotAdapter(time_scale=1.0)
        robot.connect()
    else:
        from .robot.zhaw_bridge import ZhawBridgeAdapter

        s = get_settings()
        robot = ZhawBridgeAdapter(s.zhaw_bridge_host, s.zhaw_bridge_cmd_port, sdk_timeout_s=s.zhaw_bridge_sdk_timeout_s)
        try:
            robot.connect()
        except Exception as exc:
            raise SystemExit(f"cannot reach go2_bridge at {robot.endpoint}: {exc}\n"
                             f"Start it first: python/scripts/start_bridge.sh <interface>   (or use --mock)")
    guard = MotionGuard(robot, hardware_gates_open=True, cooldown_s=0)

    def announce(g) -> None:
        _, event, reason = g.history[-1]
        (log.info if event == "armed" else log.warning)("ROBOT %s: %s", event.upper(), reason)

    guard.listeners.append(announce)
    if robot.is_real:
        ok, why = guard.arm(operator_id=0)
        print(("ARMED: " if ok else "NOT armed: ") + why, flush=True)
    link = WebLink(robot, guard, "mock" if args.mock else "bridge")
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
