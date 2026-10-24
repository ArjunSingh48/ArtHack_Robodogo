"""A simulated robot dog for presenting and rehearsing WITHOUT the Go2.

Everything the real robot would do still goes through the same paths — MotionCues → MotionGuard → adapter for
poses, the camera-guided Approacher for walking — but the adapter is the mock robot and the room is SimWorld.
Nothing physical moves. The interface draws the result ("SIMULATED DOG"): a top-down room with the dog, the
person, the path it walked, and what it is doing right now.
"""

from __future__ import annotations

import collections
import time

from ..domain import RobotBehavior
from ..motion.guard import MotionGuard
from ..motion.planner import DEFAULT_ALLOWED, SafeMotionPlanner
from ..robot.mock import MockRobotAdapter
from .approach import Approacher, ApproachSettings
from .motion import MotionCues
from .sim_world import SimWorld

# what the dog is doing, in plain words (SDK step → label)
DOING = {"StandUp": "standing up", "StandDown": "lying down", "BalanceStand": "standing", "Sit": "sitting down",
         "RiseSit": "getting up", "Hello": "giving a paw", "Stretch": "stretching", "Euler": "tilting"}
SIM_POSTURES = {"sitting", "standing", "kneeling", "crouching", "lying", "leaning_forward"}


class SimRobot:
    def __init__(self, approach: ApproachSettings | None = None, *, person=(2.2, 0.45), pose_time_scale: float = 1.0):
        self.world = SimWorld(person, "sitting", clock=time.monotonic)
        self.robot = MockRobotAdapter(time_scale=pose_time_scale)
        self.robot.connect()
        self.robot.world = self.world
        self.guard = MotionGuard(self.robot, hardware_gates_open=False, cooldown_s=0, timeout_s=60)
        self.planner = SafeMotionPlanner(DEFAULT_ALLOWED - {RobotBehavior.LOOK_ASIDE, RobotBehavior.PEER})
        self.motion = MotionCues(self.guard, self.planner, enabled=True)
        self.approacher = Approacher(self.robot, self.guard, self.world.camera, self.world,
                                     approach or ApproachSettings())
        self.approacher.world = self.world
        self.trail: collections.deque = collections.deque(maxlen=400)

    def set_person_posture(self, posture: str | None) -> None:
        p = "lying" if posture == "reclining" else (posture or "sitting")
        self.world.posture = p if p in SIM_POSTURES else "sitting"

    def snapshot(self) -> dict:
        self.world._advance()
        x, y, yaw = self.world.pose
        if not self.trail or abs(self.trail[-1][0] - x) + abs(self.trail[-1][1] - y) > 0.02:
            self.trail.append((round(x, 3), round(y, 3)))
        v = self.world._v
        walking = any(abs(c) > 1e-3 for c in v)
        doing = DOING.get(self.robot.doing, self.robot.doing.lower())
        body = str(getattr(self.robot.body, "value", self.robot.body))
        return {"robot": [round(x, 3), round(y, 3), round(yaw, 3)], "person": list(self.world.person),
                "posture": self.world.posture, "body": body,
                "doing": "walking" if walking else doing, "walking": walking, "trail": list(self.trail)}
