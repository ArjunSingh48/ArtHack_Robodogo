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
   estimate of where the robot and the person are. `go_home()` turns round, walks straight back to the
   start (dead reckoning) and turns to face the audience again — like a dog, never backwards.

Safety (in addition to the operator's physical remote):
* only with `--walk` (and `--motion --i-have-physical-estop`), SHOW_WALK_ENABLED=true, every motion
  gate open and the robot armed; re-checked every step (/disarm, /cancel, End stop it at once);
* velocities are clamped twice (here and in the adapter): ≤ 0.25 m/s forward, ≤ 0.5 rad/s turning;
  the robot never walks backwards;
* limits on time, distance and turning; losing the person for more than a second stops it;
* the bridge's own heartbeat zeroes velocity if commands stop arriving; its obstacle avoidance
  stays on (bridge default);
* every exit path ends the walk: Move(0,0,0) → stop → stop_move, confirmed (the adapter's end_walk).
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
    # a first sighting with head/feet cut off says nothing about the distance: look this long (standing still)
    # for a full-body view before giving up with "target_unreliable" — never walk on it, never call it "arrived"
    unreliable_seconds: float = 2.5
    frame_max_age_s: float = 0.6  # an older robot-camera frame is not steered on (as if no frame came)


@dataclass
class ApproachResult:
    # arrived (a validated geometric decision, person_at always set) | target_unreliable (only clipped views, no
    # position) | not_found | lost | camera_lost | timeout | too_far | stopped | refused | error
    status: str
    detail: str = ""
    travelled_m: float = 0.0
    path: list[tuple[float, float, float, float]] = field(default_factory=list)  # (vx, vy, vyaw, seconds)
    robot_pose: tuple[float, float, float] = (0.0, 0.0, 0.0)  # x, y, yaw from the start (dead reckoning)
    person_at: tuple[float, float] | None = None  # estimated, same frame
    diagnostics: dict = field(default_factory=dict)  # last look + why it stayed / searched / walked / stopped
    moves: int = 0  # velocity commands sent by this walk (the adapter's keep-alive adds its own in between)
    move_rate_hz: float = 0.0  # effective rate of those commands while walking

    @property
    def arrived(self) -> bool:
        return self.status == "arrived"


# The one high-level movement contract (Python simulation, the real Go2 via the bridge, and the browser show through
# `itu web-link`): what the approach is doing now. The browser only DISPLAYS these; it never decides where the
# physical robot is.
APPROACH_PHASES = ("searching", "target_acquired", "walking", "turning", "arrived", "target_unreliable",
                   "camera_lost", "stopped", "final_pose", "disarmed", "error")
FINAL_PHASE = {"arrived": "arrived", "target_unreliable": "target_unreliable", "camera_lost": "camera_lost",
               "not_found": "stopped", "lost": "stopped", "timeout": "stopped", "too_far": "stopped",
               "stopped": "stopped", "refused": "error", "error": "error"}


def validate_arrival(res: "ApproachResult") -> "ApproachResult":
    """Invariant of every approach: no valid person position → no arrival (never "already close — stayed")."""
    if res.status == "arrived" and res.person_at is None:
        res.status, res.detail = "target_unreliable", f"no valid position for the person ({res.detail})"
    return res


def phase_of(decision: str) -> str:
    head = decision.split(":", 1)[0]
    return {"search": "searching", "walk": "walking", "turn": "turning"}.get(head, "searching" if head == "stay"
                                                                              else "stopped")


def _clamp(v: float, m: float) -> float:
    return max(-m, min(m, v))


class Approacher:
    def __init__(self, robot, guard, camera_factory, estimator, settings: ApproachSettings | None = None,
                 *, clock=time.monotonic, sleep=time.sleep, on_state=None):
        self.robot = robot
        self.guard = guard
        self.camera_factory = camera_factory
        self.estimator = estimator
        self.s = settings or ApproachSettings()
        self.clock = clock
        self.sleep = sleep
        self.memory: ApproachResult | None = None  # the last approach: where we went, how to go back
        self._decision, self._diag_logged = "", 0.0
        self.on_state = on_state  # fn(dict) — live state for the browser / operator (APPROACH_PHASES)
        self._emitted = 0.0

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
        t0 = time.monotonic()
        self.robot.walk(vx, vy, vyaw)
        latency = time.monotonic() - t0
        if res.moves == 0:
            self._walk_t0 = self.clock()
            log.info("walk_start: vx=%.2f vy=%.2f vyaw=%.2f (bridge answered in %.0f ms)", vx, vy, vyaw, latency * 1e3)
        res.moves += 1
        span = self.clock() - getattr(self, "_walk_t0", self.clock())
        res.move_rate_hz = res.moves / span if span > 0 else 0.0
        res.diagnostics.update(cmd=(round(vx, 3), round(vy, 3), round(vyaw, 3)), bridge_ms=round(latency * 1e3, 1))
        self._last_cmd = (self.clock(), (vx, vy, vyaw), dt)
        res.path.append((vx, vy, vyaw, dt))
        self._integrate(res, vx, vy, vyaw, dt)

    def state(self, res: ApproachResult, phase: str, detail: str = "") -> dict:
        x, y, yaw = res.robot_pose
        return {"phase": phase, "detail": detail or res.diagnostics.get("decision", ""),
                "robot": [round(x, 3), round(y, 3), round(yaw, 3)],
                "person": [round(res.person_at[0], 3), round(res.person_at[1], 3)] if res.person_at else None,
                "travelled_m": round(res.travelled_m, 2), "moves": res.moves, "rate_hz": round(res.move_rate_hz, 1)}

    def _emit(self, res: ApproachResult, phase: str, detail: str = "", force: bool = False) -> None:
        if self.on_state is None:
            return
        now = self.clock()
        if not force and now - self._emitted < 0.2:
            return
        self._emitted = now
        try:
            self.on_state(self.state(res, phase, detail))
        except Exception as exc:  # a display must never stop (or keep going) a walk
            log.debug("approach state listener failed: %s", exc)

    def _decide(self, res: ApproachResult, decision: str) -> None:
        """Remember why this step stays / searches / walks / stops; log it on a change, else at most once a second."""
        changed = decision != res.diagnostics.get("decision")
        res.diagnostics["decision"] = decision
        self._emit(res, phase_of(decision), decision, force=changed)
        now = self.clock()
        if decision != self._decision or now - self._diag_logged >= 1.0:
            d = res.diagnostics
            log.info("approach: %s | bbox=%s clipped=%s clipped_x=%s h=%s w=%s estimates=%s dist=%s bearing=%s "
                     "cmd=%s moves=%d (%.1f Hz)", decision, d.get("bbox"), d.get("clipped"), d.get("clipped_x"),
                     d.get("height"), d.get("width"), d.get("estimates"), d.get("distance_m"), d.get("bearing_deg"),
                     d.get("cmd"), res.moves, res.move_rate_hz)
            self._decision, self._diag_logged = decision, now

    def _halt_if_moving(self, res: ApproachResult) -> None:
        if getattr(self, "_last_cmd", None) is not None:
            self._halt(res)

    def _halt(self, res: ApproachResult | None = None) -> None:
        if res is not None:
            self._catch_up(res)
        self._last_cmd = None
        self.robot.halt()

    def _end_walk(self, reason: str, res: ApproachResult | None = None) -> bool:
        """Every exit — arrived, timeout, lost camera, cancel, disarm, exception: leave walking for good."""
        if res is not None:
            self._catch_up(res)
        self._last_cmd = None
        end = getattr(self.robot, "end_walk", None)
        if end is None:
            self.robot.halt()
            return True
        if not end(reason):
            log.error("walk cleanup after %s was NOT verified — check the dog / use the remote", reason)
            return False
        return True

    def _standing(self) -> bool:
        body = getattr(self.robot, "body", None)
        return body is None or str(getattr(body, "value", body)) == "standing"

    def _look(self, camera):
        frame = camera.read()
        stamp = getattr(frame, "timestamp", None)
        if frame is None or (isinstance(stamp, (int, float)) and time.time() - stamp > self.s.frame_max_age_s):
            self._no_frame = True  # no frame — or one too old to steer on
            return None
        self._no_frame = False
        people = self.estimator.estimate(frame.image)
        person, _ = select_primary_person(people)
        if person is None:
            return None
        w, h = person.image_size
        x0, y0, x1, y1 = person.bbox()
        return {"cx": (x0 + x1) / 2 / w, "height": (y1 - y0) / h, "width": (x1 - x0) / w, "bottom": y1 / h,
                "bbox": (round(x0 / w, 3), round(y0 / h, 3), round(x1 / w, 3), round(y1 / h, 3)),
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

    def approach(self, *, side: str = "left", posture: str = "sitting", facing: str = "",
                 should_stop=lambda: False) -> ApproachResult:
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
        unreliable_since = None
        got_frame = False
        self._decision = ""
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
                    self._halt_if_moving(res)  # never keep walking blind, not even for one step
                    # before the first frame the stream may still be connecting (nothing has moved yet)
                    limit = self.s.camera_lost_seconds if got_frame else max(self.s.camera_lost_seconds, 2.0)
                    self._decide(res, "stop: no (fresh) camera frame")
                    if now - blind_since >= limit:
                        res.status, res.detail = "camera_lost", "no camera frame from the robot — stopped"
                        return res
                    self.sleep(dt)
                    continue
                blind_since = None
                got_frame = True
                if seen is not None:
                    last_seen = now
                    clipped = seen["clipped_x" if lying else "clipped"]
                    res.diagnostics.update(bbox=seen["bbox"], clipped=seen["clipped"], clipped_x=seen["clipped_x"],
                                           height=round(seen["height"], 3), width=round(seen["width"], 3))
                    if clipped:
                        # head or feet cut off: their size no longer tells the distance.
                        if person is None:
                            # no position yet: NOT "already close" — maybe badly framed, half in view, unstable
                            # box or a late frame. Stand still and look again; never walk on it.
                            unreliable_since = unreliable_since if unreliable_since is not None else now
                            if seen["clipped_x"] and abs(searched) < self.s.search_max_angle:
                                # cut off at the SIDE of the image: turn slowly on the spot towards that side until
                                # the whole person is in view (turning only, never forward)
                                turn = self.s.search_speed if seen["cx"] < 0.5 else -self.s.search_speed
                                self._decide(res, "search: person cut off at the image edge — turning towards them")
                                self._send(res, 0.0, 0.0, turn, dt)
                                searched += turn * dt
                                unreliable_since = now  # turning towards them: the 2.5 s start once it stops
                                self.sleep(dt)
                                continue
                            self._halt_if_moving(res)
                            self._decide(res, "stay: clipped first detection, no position memory — looking again")
                            if now - unreliable_since >= self.s.unreliable_seconds:
                                res.status = "target_unreliable"
                                res.detail = ("the robot camera never saw the participant's full body (head or feet "
                                              "cut off) — move the participant back or reframe them")
                                return res
                            self.sleep(dt)
                            continue
                        # a valid remembered position: steer on it
                        x, y, _ = res.robot_pose
                        if math.hypot(person[0] - x, person[1] - y) < min_gap:
                            self._halt(res)
                            res.person_at = person
                            self._decide(res, "stop: clipped, and the remembered position is within the minimum gap")
                            res.status, res.detail = "arrived", "close to the person (remembered position) — stopped"
                            return res
                    else:
                        unreliable_since = None
                        if not estimates:
                            res.person_at = self._person_in_start_frame(res, seen, k, lying)
                            self._emit(res, "target_acquired", "person located with the robot camera", force=True)
                        estimates = (estimates + [self._person_in_start_frame(res, seen, k, lying)])[-6:]
                        person = (sum(e[0] for e in estimates) / len(estimates),
                                  sum(e[1] for e in estimates) / len(estimates))
                        res.diagnostics.update(estimates=len(estimates))
                elif person is None:  # still searching: turn slowly, left first, then right
                    if search_dir > 0 and searched >= self.s.search_max_angle:
                        search_dir = -1.0
                    if search_dir < 0 and searched <= -self.s.search_max_angle:
                        res.status, res.detail = "not_found", "nobody in view after looking around"
                        return res
                    self._decide(res, "search: nobody in view, turning slowly")
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
                res.diagnostics.update(distance_m=round(math.hypot(person[0] - x, person[1] - y), 2),
                                       bearing_deg=round(math.degrees(math.atan2(math.sin(
                                           math.atan2(person[1] - y, person[0] - x) - yaw), math.cos(
                                           math.atan2(person[1] - y, person[0] - x) - yaw))), 1),
                                       goal_m=round(dist, 2))
                if dist <= self.s.arrive_tolerance_m:
                    self._decide(res, "stop: at the goal beside the person")
                    self._halt(res)
                    self._face(res, person, should_stop, facing)
                    res.status, res.detail = "arrived", "in front of the person (lying)" if lying else f"{side} of the person"
                    return res
                heading = math.atan2(gy - y, gx - x)
                err = math.atan2(math.sin(heading - yaw), math.cos(heading - yaw))
                vyaw = _clamp(1.8 * err, self.s.turn_speed)
                aligned = max(0.0, 1.0 - abs(err) / math.radians(35))  # walk only when roughly facing it
                vx = self.s.speed * aligned * min(1.0, max(0.3, dist / 0.6))
                self._decide(res, "walk: towards the goal" if vx > 0.01 else "turn: towards the goal")
                self._send(res, vx, 0.0, vyaw, dt)
                self.sleep(dt)
        except Exception as exc:
            log.exception("approach failed: %s", exc)
            res.status, res.detail = "error", str(exc)
            return res
        finally:
            validate_arrival(res)
            try:
                acked = self._end_walk(f"approach {res.status}", res)
            except Exception:
                acked = False
                log.exception("ending the walk failed")
            if camera is not None:
                try:
                    camera.close()
                except Exception:
                    pass
            self.memory = res
            final = "disarmed" if res.status == "stopped" and res.detail in ("disarmed", "software stop") else \
                FINAL_PHASE.get(res.status, "stopped")
            self._emit(res, final, res.detail, force=True)
            log.info("walk_stop: %s (%s) — %d velocity commands, %.1f Hz; cleanup Move 0 → stop → stop_move %s",
                     res.status, res.detail, res.moves, res.move_rate_hz, "acknowledged" if acked else "NOT verified")
            log.info("approach: %s (%s), %.1f m, pose %s, person %s", res.status, res.detail, res.travelled_m,
                     tuple(round(v, 2) for v in res.robot_pose), res.person_at)

    # the painted dog's look, as the audience (behind the start, looking along +x) sees it → heading
    FACING = {"away": 0.0, "viewer": math.pi, "left": math.pi / 2, "right": -math.pi / 2}

    def _face(self, res: ApproachResult, person, should_stop, facing: str = "") -> None:
        """Turn on the spot to look at the person (or back to the starting direction)."""
        x, y, yaw = res.robot_pose
        target = math.atan2(person[1] - y, person[0] - x) if self.s.face == "person" else 0.0
        if facing in self.FACING:  # like the dog in the picture
            target = self.FACING[facing]
        self._turn(res, math.atan2(math.sin(target - yaw), math.cos(target - yaw)), should_stop)

    def _turn(self, res: ApproachResult, angle: float, should_stop) -> None:
        dt = 1.0 / self.s.rate_hz
        steps = int(round(abs(angle) / (self.s.turn_speed * dt)))
        for _ in range(steps):
            if should_stop() or not self._allowed_now()[0]:
                break
            self._emit(res, "turning", "turning on the spot")
            self._send(res, 0.0, 0.0, math.copysign(self.s.turn_speed, angle), dt)
            self.sleep(dt)
        self._halt(res)

    def go_home(self, *, should_stop=lambda: False) -> str:
        """Turn towards the start, walk straight there, turn to face the audience again (open loop, dead
        reckoning from the remembered pose). → done | stopped | nothing"""
        mem = self.memory
        if mem is None or not mem.path:
            return "nothing"
        ok, why = self.guard.walk_allowed()
        if not ok or not self._standing():
            return "stopped"
        back = ApproachResult("home", robot_pose=mem.robot_pose)
        wrap = lambda a: math.atan2(math.sin(a), math.cos(a))  # noqa: E731
        try:
            x, y, yaw = back.robot_pose
            dt = 1.0 / self.s.rate_hz
            if math.hypot(x, y) > 0.05:
                self._turn(back, wrap(math.atan2(-y, -x) - yaw), should_stop)
                for _ in range(int(3 * math.hypot(x, y) / (self.s.speed * dt)) + 10):  # steer on the way
                    x, y, yaw = back.robot_pose
                    dist = math.hypot(x, y)
                    if dist < 0.01:
                        break
                    if should_stop() or not self._allowed_now()[0]:
                        return "stopped"
                    err = wrap(math.atan2(-y, -x) - yaw)
                    step = min(dt, dist / self.s.speed)  # the last step is shorter
                    self._emit(back, "walking", "walking back to the start")
                    self._send(back, self.s.speed, 0.0, _clamp(2 * err, self.s.turn_speed), step)
                    self.sleep(step)
                self._halt(back)
            self._turn(back, wrap(-back.robot_pose[2]), should_stop)  # face where it started facing
            if should_stop() or not self._allowed_now()[0]:
                return "stopped"
            self.memory = None
            back.status = "done"
            return "done"
        finally:
            try:
                self._end_walk("walk home ended")
            except Exception:
                log.exception("ending the walk failed")
            self._emit(back, "arrived" if back.status == "done" else "stopped",
                       "back at the start" if back.status == "done" else "walk home stopped", force=True)
