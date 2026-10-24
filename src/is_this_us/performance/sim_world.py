"""A tiny simulated room for rehearsing and testing the camera-guided approach without a robot.

The robot starts at (0, 0) facing +x. A person stands or sits somewhere in front. Walking commands
move the simulated robot; its "camera" sees the person as a synthetic skeleton whose size and
position in the image follow from distance and bearing (90° field of view).
"""

from __future__ import annotations

import math
import time

import numpy as np

from ..vision.frame_sources import Frame
from ..vision.synthetic import synthetic_person

HFOV = math.radians(90)


def _base_height(posture: str) -> float:
    """Synthetic skeleton height / image height at scale 1."""
    p = synthetic_person(posture)
    x0, y0, x1, y1 = p.bbox()
    return (y1 - y0) / p.image_size[1]


def _base_width(posture: str) -> float:
    p = synthetic_person(posture)
    x0, y0, x1, y1 = p.bbox()
    return (x1 - x0) / p.image_size[0]


class SimWorld:
    def __init__(self, person=(2.2, 0.4), posture: str = "sitting", clock=time.monotonic):
        self.person = person
        self.posture = posture
        self.clock = clock
        self.pose = [0.0, 0.0, 0.0]  # x, y, yaw
        self._v = (0.0, 0.0, 0.0)
        self._t = clock()
        self.min_distance = math.inf

    def _advance(self) -> None:
        now = self.clock()
        dt, self._t = now - self._t, now
        vx, vy, vyaw = self._v
        x, y, yaw = self.pose
        x += (vx * math.cos(yaw) - vy * math.sin(yaw)) * dt
        y += (vx * math.sin(yaw) + vy * math.cos(yaw)) * dt
        self.pose = [x, y, yaw + vyaw * dt]
        self.min_distance = min(self.min_distance, self.distance())

    def command(self, vx: float, vy: float, vyaw: float) -> None:
        self._advance()
        self._v = (vx, vy, vyaw)

    def distance(self) -> float:
        return math.hypot(self.person[0] - self.pose[0], self.person[1] - self.pose[1])

    def bearing(self) -> float:
        """Angle of the person relative to where the robot faces (+ = to the robot's left)."""
        ang = math.atan2(self.person[1] - self.pose[1], self.person[0] - self.pose[0]) - self.pose[2]
        return math.atan2(math.sin(ang), math.cos(ang))

    def side_of_person(self) -> str:
        """Where the robot is relative to the person, seen from the robot's starting point (the audience)."""
        ux, uy = self.person
        rx, ry = self.pose[0] - self.person[0], self.pose[1] - self.person[1]
        cross = ux * ry - uy * rx
        if abs(cross) < 0.25 * math.hypot(ux, uy):
            return "front"
        return "left" if cross > 0 else "right"

    # camera + estimator facades
    def camera(self):
        world = self

        class Cam:
            name = "sim robot camera"

            def read(self):
                world._advance()
                return Frame(image=np.zeros((480, 640, 3), dtype=np.uint8), source="sim")

            def close(self):
                pass

        return Cam()

    def estimate(self, rgb):
        self._advance()
        b = self.bearing()
        if abs(b) > HFOV / 2:
            return []
        x_img = 0.5 - b / HFOV
        if self.posture == "lying":  # seen lying across the view: its width tells the distance
            frac = min(1.4, 0.85 / max(self.distance(), 0.2))
            return [synthetic_person("lying", scale=frac / _base_width("lying"), shift_x=(x_img - 0.5) * 640)]
        frac = min(1.4, (1.1 if self.posture == "standing" else 0.8) / max(self.distance(), 0.2))
        return [synthetic_person(self.posture, scale=frac / _base_height(self.posture), shift_x=(x_img - 0.5) * 640)]
