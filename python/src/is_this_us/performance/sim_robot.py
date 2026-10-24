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
# MediaPipe left/right landmark pairs, for showing the participant like a mirror
SWAP_LR = list(range(33))
for _a, _b in [(1, 4), (2, 5), (3, 6), (7, 8), (9, 10)] + [(i, i + 1) for i in range(11, 33, 2)]:
    SWAP_LR[_a], SWAP_LR[_b] = _b, _a
SKELETON_MAX_AGE_S = 1.5


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
        self._seen = None  # (world landmarks, visibility, mirror, when) of the participant, for the 3-D avatar

    def see_person(self, landmarks, mirror: bool) -> None:
        """The participant as the photo camera last saw them; the 3-D view poses the avatar like this."""
        world = getattr(landmarks, "world", None)
        if world is not None:
            self._seen = (world, landmarks.visibility, mirror, time.monotonic())

    def skeleton(self) -> list | None:
        """33 × [x, y, z, visibility] in metres: x to the viewer's right, y up, z toward the viewer (the avatar
        faces the viewer). None when nobody was seen recently: the view then shows the posture preset."""
        if self._seen is None or time.monotonic() - self._seen[3] > SKELETON_MAX_AGE_S:
            return None
        world, vis, mirror, _ = self._seen
        p = world * [1, -1, -1]  # camera axes (y down, z away) → y up, z toward the viewer
        if mirror:  # like a mirror: flip left/right, and the avatar's left side follows the person's right
            p, vis = p[SWAP_LR] * [-1, 1, 1], vis[SWAP_LR]
        return [[round(float(x), 3), round(float(y), 3), round(float(z), 3), round(float(v), 2)]
                for (x, y, z), v in zip(p, vis)]

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
                "posture": self.world.posture, "body": body, "action": self.robot.doing, "skeleton": self.skeleton(),
                "doing": "walking" if walking else doing, "walking": walking, "trail": list(self.trail)}
