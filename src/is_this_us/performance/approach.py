"""Camera-guided approach: the robot finds the participant with ITS OWN camera and walks to their side.

How it works (one loop, ~8 Hz):
1. Look: the robot's front camera → MediaPipe (existing estimator) → the most prominent person.
2. Search: nobody in view → turn slowly on the spot (left, then back right), up to a limit.
3. Locate: bearing from where the person is in the image, distance from how tall they look
   (distance ≈ k / height-fraction; k is calibrated on site). Estimates are averaged and kept in
   the robot's starting frame — the robot "remembers" where the person is even when they leave the view.
4. Go: walk (dead reckoning) to a spot beside the person — LEFT or RIGHT as seen from where the robot
   started (the audience side), or in FRONT — never closer than `min_gap_m`; then turn to face them.
   If the person suddenly looks very large in the camera, it stops at once.
5. Remember: every velocity command actually sent is kept (`path`) together with a dead-reckoned
   estimate of where the robot and the person are. `go_home()` replays the path backwards.

Safety (in addition to the operator's physical remote):
* only with `--walk` (and `--motion --i-have-physical-estop`), SHOW_WALK_ENABLED=true, every motion
  gate open and the robot armed; re-checked every step (/disarm, /cancel, End stop it at once);
* velocities are clamped twice (here and in the adapter): ≤ 0.25 m/s forward, ≤ 0.5 rad/s turning;
  the robot never walks backwards toward people except when retracing its own path home;
* limits on time, distance and turning; losing the person for more than a second stops it;
* the bridge's own heartbeat zeroes velocity if commands stop arriving; its obstacle avoidance
  stays on (bridge default);
* every exit path sends a halt.
Distances are estimates from a single camera — the stop margin is deliberately generous.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from ..vision.pose import select_primary_person

log = logging.getLogger(__name__)


@dataclass
class ApproachSettings:
    speed: float = 0.22  # m/s forward, at most
    turn_speed: float = 0.45  # rad/s, at most
    search_speed: float = 0.3  # rad/s while looking for someone
    search_max_angle: float = math.radians(100)  # look up to this far left, then the same to the right
    hfov: float = math.radians(90)  # robot camera's horizontal field of view (Go2 front camera ≈ 90–120°)
    # distance ≈ k / (person's height in the image as a fraction of the image height).
    # Calibrate on site with `itu show walk-test --calibrate 2.0` (person at 2 m).
    k_standing: float = 1.1
    k_sitting: float = 0.8
    # someone lying on the floor (the rescue): distance ≈ k_lying / their WIDTH in the image, and the
    # robot stops further away, because their head or feet may be much closer than their middle
    k_lying: float = 0.85
    lying_gap_m: float = 1.2
    beside_m: float = 0.65  # how far to the person's side the robot stops (centre to centre)
    front_m: float = 0.9  # for "front": how far in front of the person
    min_gap_m: float = 0.55  # never plan a goal closer than this to the person
    arrive_tolerance_m: float = 0.12
    face: str = "person"  # at the end turn to face: person | start (the direction it started in)
    max_seconds: float = 40.0
    max_distance_m: float = 3.5
    lost_seconds: float = 6.0  # without seeing the person again, give up
    camera_lost_seconds: float = 0.6  # no camera FRAME at all for this long: stop at once (blind)
    rate_hz: float = 8.0


@dataclass
class ApproachResult:
    status: str  # arrived | not_found | lost | camera_lost | timeout | too_far | stopped | refused | error
    detail: str = ""
    travelled_m: float = 0.0
    path: list[tuple[float, float, float, float]] = field(default_factory=list)  # (vx, vy, vyaw, seconds)
    robot_pose: tuple[float, float, float] = (0.0, 0.0, 0.0)  # x, y, yaw from the start (dead reckoning)
    person_at: tuple[float, float] | None = None  # estimated, same frame

    @property
    def arrived(self) -> bool:
        return self.status == "arrived"


def _clamp(v: float, m: float) -> float:
    return max(-m, min(m, v))


class Approacher:
    def __init__(self, robot, guard, camera_factory, estimator, settings: ApproachSettings | None = None,
                 *, clock=time.monotonic, sleep=time.sleep):
        self.robot = robot
        self.guard = guard
        self.camera_factory = camera_factory
        self.estimator = estimator
        self.s = settings or ApproachSettings()
        self.clock = clock
        self.sleep = sleep
        self.memory: ApproachResult | None = None  # the last approach: where we went, how to go back

    # ------------------------------------------------------------------ helpers
    def _allowed_now(self) -> tuple[bool, str]:
        if getattr(self.guard, "stop_latched", False):
            return False, "software stop"
        if getattr(self.robot, "is_real", False) and not self.guard.armed:
            return False, "disarmed"
        return True, ""

    def _integrate(self, res: ApproachResult, vx: float, vy: float, vyaw: float, t: float) -> None:
        x, y, yaw = res.robot_pose
        x += (vx * math.cos(yaw) - vy * math.sin(yaw)) * t
        y += (vx * math.sin(yaw) + vy * math.cos(yaw)) * t
        res.robot_pose = (x, y, yaw + vyaw * t)
        res.travelled_m += abs(vx) * t + abs(vy) * t

    def _catch_up(self, res: ApproachResult) -> None:
        """The previous velocity kept running until now (camera + pose estimate can take longer than one tick;
        the bridge keeps a velocity alive for at most `coast_s`). Count that real time in the dead reckoning and
        in the remembered path, so the robot never believes it is further from the person than it is."""
        last = getattr(self, "_last_cmd", None)
        if last is None:
            return
        t0, (vx, vy, vyaw), assumed = last
        coast = float(getattr(self.robot, "coast_s", 0.3))
        extra = min(self.clock() - t0, max(assumed, coast)) - assumed
        if extra > 1e-3 and (vx or vy or vyaw):
            self._integrate(res, vx, vy, vyaw, extra)
            if res.path:
                pvx, pvy, pvyaw, pdt = res.path[-1]
                res.path[-1] = (pvx, pvy, pvyaw, pdt + extra)
        self._last_cmd = (t0, (vx, vy, vyaw), assumed + max(0.0, extra))

    def _send(self, res: ApproachResult, vx: float, vy: float, vyaw: float, dt: float) -> None:
        vx = _clamp(vx, self.s.speed)
        vy = _clamp(vy, self.s.speed)
        vyaw = _clamp(vyaw, max(self.s.turn_speed, self.s.search_speed))
        self._catch_up(res)
        self.robot.walk(vx, vy, vyaw)
        self._last_cmd = (self.clock(), (vx, vy, vyaw), dt)
        res.path.append((vx, vy, vyaw, dt))
        self._integrate(res, vx, vy, vyaw, dt)

    def _halt(self, res: ApproachResult | None = None) -> None:
        if res is not None:
            self._catch_up(res)
        self._last_cmd = None
        self.robot.halt()

    def _standing(self) -> bool:
        body = getattr(self.robot, "body", None)
        return body is None or str(getattr(body, "value", body)) == "standing"

    def _look(self, camera):
        frame = camera.read()
        if frame is None:
            self._no_frame = True
            return None
        self._no_frame = False
        people = self.estimator.estimate(frame.image)
        person, _ = select_primary_person(people)
        if person is None:
            return None
        w, h = person.image_size
        x0, y0, x1, y1 = person.bbox()
        return {"cx": (x0 + x1) / 2 / w, "height": (y1 - y0) / h, "width": (x1 - x0) / w, "bottom": y1 / h,
                "clipped": y0 / h <= 0.02 or y1 / h >= 0.98,
                "clipped_x": x0 / w <= 0.02 or x1 / w >= 0.98}

    # ------------------------------------------------------------------ main
    def _person_in_start_frame(self, res: ApproachResult, seen: dict, k: float, lying: bool = False) -> tuple[float, float]:
        dist = k / max(seen["width"] if lying else seen["height"], 0.05)
        bearing = (0.5 - seen["cx"]) * self.s.hfov  # + = to the robot's left
        x, y, yaw = res.robot_pose
        return x + dist * math.cos(yaw + bearing), y + dist * math.sin(yaw + bearing)

    def goal_for(self, person: tuple[float, float], side: str, lying: bool = False) -> tuple[float, float]:
        """A spot beside the person, as seen from where the robot started (the audience side)."""
        px, py = person
        d = math.hypot(px, py) or 1.0
        ux, uy = px / d, py / d  # from the start towards the person
        lx, ly = -uy, ux  # the start's left
        if side == "left":
            gx, gy = px + self.s.beside_m * lx - 0.15 * ux, py + self.s.beside_m * ly - 0.15 * uy
        elif side == "right":
            gx, gy = px - self.s.beside_m * lx - 0.15 * ux, py - self.s.beside_m * ly - 0.15 * uy
        else:
            gx, gy = px - self.s.front_m * ux, py - self.s.front_m * uy
        if lying:  # never step onto someone on the floor: stop in front, further away
            gx, gy = px - self.s.lying_gap_m * ux, py - self.s.lying_gap_m * uy
        min_gap = self.s.lying_gap_m if lying else self.s.min_gap_m
        gap = math.hypot(gx - px, gy - py)
        if gap < min_gap:  # safety: push the goal away from the person
            f = min_gap / max(gap, 1e-6)
            gx, gy = px + (gx - px) * f, py + (gy - py) * f
        return gx, gy

    def approach(self, *, side: str = "left", posture: str = "sitting", should_stop=lambda: False) -> ApproachResult:
        res = ApproachResult("error")
        ok, why = self.guard.walk_allowed()
        if not ok:
            res.status, res.detail = "refused", why
            return res
        if not self._standing():
            res.status, res.detail = "refused", "the robot is not standing"
            return res
        lying = posture in ("lying", "reclining")
        k = self.s.k_lying if lying else self.s.k_standing if posture == "standing" else self.s.k_sitting
        min_gap = self.s.lying_gap_m if lying else self.s.min_gap_m
        dt = 1.0 / self.s.rate_hz
        camera = None
        start = self.clock()
        searched, search_dir = 0.0, 1.0
        person: tuple[float, float] | None = None
        estimates: list[tuple[float, float]] = []
        last_seen = None
        blind_since = None
        try:
            camera = self.camera_factory()
            while True:
                if should_stop():
                    res.status, res.detail = "stopped", "operator"
                    return res
                ok, why = self._allowed_now()
                if not ok:
                    res.status, res.detail = "stopped", why
                    return res
                if self.clock() - start > self.s.max_seconds:
                    res.status, res.detail = "timeout", f"{self.s.max_seconds:.0f}s"
                    return res
                if res.travelled_m > self.s.max_distance_m:
                    res.status, res.detail = "too_far", f"{res.travelled_m:.1f} m"
                    return res
                seen = self._look(camera)
                now = self.clock()
                if getattr(self, "_no_frame", False):
                    blind_since = blind_since if blind_since is not None else now
                    self._halt(res)  # never keep walking blind, not even for one step
                    if now - blind_since >= self.s.camera_lost_seconds:
                        res.status, res.detail = "camera_lost", "no camera frame from the robot — stopped"
                        return res
                    self.sleep(dt)
                    continue
                blind_since = None
                if seen is not None:
                    last_seen = now
                    if seen["clipped_x" if lying else "clipped"]:
                        # head or feet cut off: their size no longer tells the distance. Rely on memory —
                        # and if memory says we are already close (or we have none), stop right here.
                        x, y, _ = res.robot_pose
                        if person is None:
                            self._halt(res)
                            res.status, res.detail = "arrived", "already close to the person — stayed"
                            return res
                        if math.hypot(person[0] - x, person[1] - y) < min_gap:
                            self._halt(res)
                            res.person_at = person
                            res.status, res.detail = "arrived", "close to the person — stopped"
                            return res
                    else:
                        estimates = (estimates + [self._person_in_start_frame(res, seen, k, lying)])[-6:]
                        person = (sum(e[0] for e in estimates) / len(estimates),
                                  sum(e[1] for e in estimates) / len(estimates))
                elif person is None:  # still searching: turn slowly, left first, then right
                    if search_dir > 0 and searched >= self.s.search_max_angle:
                        search_dir = -1.0
                    if search_dir < 0 and searched <= -self.s.search_max_angle:
                        res.status, res.detail = "not_found", "nobody in view after looking around"
                        return res
                    self._send(res, 0.0, 0.0, search_dir * self.s.search_speed, dt)
                    searched += search_dir * self.s.search_speed * dt
                    self.sleep(dt)
                    continue
                elif now - last_seen > self.s.lost_seconds:
                    res.status, res.detail = "lost", "the person has not been seen for a while"
                    return res
                res.person_at = person
                gx, gy = self.goal_for(person, side, lying)
                x, y, yaw = res.robot_pose
                dist = math.hypot(gx - x, gy - y)
                if dist <= self.s.arrive_tolerance_m:
                    self._halt(res)
                    self._face(res, person, should_stop)
                    res.status, res.detail = "arrived", "in front of the person (lying)" if lying else f"{side} of the person"
                    return res
                heading = math.atan2(gy - y, gx - x)
                err = math.atan2(math.sin(heading - yaw), math.cos(heading - yaw))
                vyaw = _clamp(1.8 * err, self.s.turn_speed)
                aligned = max(0.0, 1.0 - abs(err) / math.radians(35))  # walk only when roughly facing it
                vx = self.s.speed * aligned * min(1.0, max(0.3, dist / 0.6))
                self._send(res, vx, 0.0, vyaw, dt)
                self.sleep(dt)
        except Exception as exc:
            log.exception("approach failed: %s", exc)
            res.status, res.detail = "error", str(exc)
            return res
        finally:
            try:
                self._halt(res)
            except Exception:
                pass
            if camera is not None:
                try:
                    camera.close()
                except Exception:
                    pass
            self.memory = res
            log.info("approach: %s (%s), %.1f m, pose %s, person %s", res.status, res.detail, res.travelled_m,
                     tuple(round(v, 2) for v in res.robot_pose), res.person_at)

    def _face(self, res: ApproachResult, person, should_stop) -> None:
        """Turn on the spot to look at the person (or back to the starting direction)."""
        x, y, yaw = res.robot_pose
        target = math.atan2(person[1] - y, person[0] - x) if self.s.face == "person" else 0.0
        self._turn(res, math.atan2(math.sin(target - yaw), math.cos(target - yaw)), should_stop)

    def _turn(self, res: ApproachResult, angle: float, should_stop) -> None:
        dt = 1.0 / self.s.rate_hz
        steps = int(round(abs(angle) / (self.s.turn_speed * dt)))
        for _ in range(steps):
            if should_stop() or not self._allowed_now()[0]:
                break
            self._send(res, 0.0, 0.0, math.copysign(self.s.turn_speed, angle), dt)
            self.sleep(dt)
        self._halt(res)

    def go_home(self, *, should_stop=lambda: False) -> str:
        """Retrace the remembered path backwards (open loop). → done | stopped | nothing"""
        mem = self.memory
        if mem is None or not mem.path:
            return "nothing"
        ok, why = self.guard.walk_allowed()
        if not ok or not self._standing():
            return "stopped"
        back = ApproachResult("home")
        try:
            for vx, vy, vyaw, dt in reversed(mem.path):
                if should_stop() or not self._allowed_now()[0]:
                    return "stopped"
                self._send(back, -vx, -vy, -vyaw, dt)
                self.sleep(dt)
            self.memory = None
            return "done"
        finally:
            try:
                self._halt()
            except Exception:
                pass
