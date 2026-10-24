"""Human pose estimation (MediaPipe Pose Landmarker, IMAGE mode).

The estimator returns raw landmarks for every detected person; choosing *which*
person to respond to is an explicit, documented step (`select_primary_person`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
)

# MediaPipe BlazePose landmark indices
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 2, 5, 7, 8
L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW, L_WRIST, R_WRIST = 11, 12, 13, 14, 15, 16
L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE = 23, 24, 25, 26, 27, 28


@dataclass
class PersonLandmarks:
    """33 landmarks in *pixel* coordinates plus visibility (0..1)."""

    xy: np.ndarray  # (33, 2) pixels, y grows downward
    visibility: np.ndarray  # (33,)
    image_size: tuple[int, int]  # (width, height)

    def vis(self, *idx: int) -> float:
        return float(np.min(self.visibility[list(idx)]))

    def bbox(self, min_vis: float = 0.3) -> tuple[float, float, float, float]:
        pts = self.xy[self.visibility >= min_vis]
        if len(pts) == 0:
            pts = self.xy
        (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
        return float(x0), float(y0), float(x1), float(y1)

    @property
    def mean_visibility(self) -> float:
        return float(np.mean(self.visibility))


class PoseEstimator(Protocol):
    def estimate(self, rgb: np.ndarray) -> list[PersonLandmarks]: ...


def select_primary_person(people: list[PersonLandmarks]) -> tuple[PersonLandmarks | None, str]:
    """Choose the visitor the robot responds to.

    Rule: the most prominent person = largest bounding-box area weighted by mean
    landmark visibility. With the camera on the robot, this is usually the closest,
    most fully visible person. The reason string is logged for transparency.
    """
    if not people:
        return None, "no person detected"
    if len(people) == 1:
        return people[0], "single person"

    def score(p: PersonLandmarks) -> float:
        x0, y0, x1, y1 = p.bbox()
        return (x1 - x0) * (y1 - y0) * p.mean_visibility

    ranked = sorted(people, key=score, reverse=True)
    return ranked[0], f"{len(people)} people; chose largest & most visible"


class MediaPipePoseEstimator:
    def __init__(self, model_path: Path, num_poses: int = 3, min_confidence: float = 0.5):
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"Pose model not found at {model_path}. Run `itu setup` to download it."
            )
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision

        options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.IMAGE,
            num_poses=num_poses,
            min_pose_detection_confidence=min_confidence,
            min_pose_presence_confidence=min_confidence,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)

    def estimate(self, rgb: np.ndarray) -> list[PersonLandmarks]:
        import mediapipe as mp

        h, w = rgb.shape[:2]
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._landmarker.detect(image)
        people = []
        for pose in result.pose_landmarks or []:
            xy = np.array([[lm.x * w, lm.y * h] for lm in pose], dtype=np.float32)
            vis = np.array(
                [min(getattr(lm, "visibility", 1.0) or 0.0, getattr(lm, "presence", 1.0) or 1.0) for lm in pose],
                dtype=np.float32,
            )
            people.append(PersonLandmarks(xy=xy, visibility=np.clip(vis, 0, 1), image_size=(w, h)))
        return people

    def close(self) -> None:
        self._landmarker.close()


def download_pose_model(target: Path) -> Path:
    import httpx

    target.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", POSE_MODEL_URL, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return target
