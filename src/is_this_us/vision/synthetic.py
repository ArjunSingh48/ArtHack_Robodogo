"""SYNTHETIC skeletons for simulation and tests — not real people, not real data.

Hand-placed 2-D landmark layouts for each broad posture, so the full pipeline can be
exercised without a camera, a photo, or MediaPipe.
"""

from __future__ import annotations

import numpy as np

from .pose import (
    L_ANKLE, L_EAR, L_ELBOW, L_HIP, L_KNEE, L_SHOULDER, L_WRIST, NOSE,
    R_ANKLE, R_EAR, R_ELBOW, R_HIP, R_KNEE, R_SHOULDER, R_WRIST,
    PersonLandmarks,
)

L_EYE, R_EYE = 2, 5

BASE_HEAD = {NOSE: (320, 110), L_EYE: (315, 105), R_EYE: (325, 105), L_EAR: (305, 110), R_EAR: (335, 110)}

LAYOUTS: dict[str, dict[int, tuple[float, float]]] = {
    "standing": {
        L_SHOULDER: (290, 150), R_SHOULDER: (350, 150), L_ELBOW: (280, 200), R_ELBOW: (360, 200),
        L_WRIST: (275, 250), R_WRIST: (365, 250), L_HIP: (300, 250), R_HIP: (340, 250),
        L_KNEE: (300, 340), R_KNEE: (340, 340), L_ANKLE: (300, 430), R_ANKLE: (340, 430),
    },
    "sitting": {
        L_SHOULDER: (290, 150), R_SHOULDER: (350, 150), L_ELBOW: (285, 200), R_ELBOW: (355, 200),
        L_WRIST: (300, 240), R_WRIST: (345, 240), L_HIP: (300, 250), R_HIP: (340, 250),
        L_KNEE: (380, 255), R_KNEE: (400, 255), L_ANKLE: (380, 345), R_ANKLE: (400, 345),
    },
    "crouching": {
        L_SHOULDER: (290, 290), R_SHOULDER: (350, 290), L_ELBOW: (300, 330), R_ELBOW: (345, 330),
        L_WRIST: (330, 350), R_WRIST: (360, 350), L_HIP: (300, 370), R_HIP: (340, 370),
        L_KNEE: (370, 350), R_KNEE: (390, 350), L_ANKLE: (335, 400), R_ANKLE: (355, 400),
    },
    "kneeling": {
        L_SHOULDER: (290, 200), R_SHOULDER: (350, 200), L_ELBOW: (285, 250), R_ELBOW: (355, 250),
        L_WRIST: (285, 295), R_WRIST: (355, 295), L_HIP: (300, 300), R_HIP: (340, 300),
        L_KNEE: (305, 390), R_KNEE: (345, 390), L_ANKLE: (240, 395), R_ANKLE: (280, 395),
    },
    "lying": {
        L_SHOULDER: (150, 300), R_SHOULDER: (150, 330), L_ELBOW: (200, 290), R_ELBOW: (200, 340),
        L_WRIST: (250, 290), R_WRIST: (250, 340), L_HIP: (250, 305), R_HIP: (250, 325),
        L_KNEE: (340, 308), R_KNEE: (340, 322), L_ANKLE: (430, 310), R_ANKLE: (430, 320),
    },
    "leaning_forward": {
        L_SHOULDER: (354, 173), R_SHOULDER: (414, 173), L_ELBOW: (380, 230), R_ELBOW: (420, 230),
        L_WRIST: (390, 280), R_WRIST: (425, 280), L_HIP: (300, 250), R_HIP: (340, 250),
        L_KNEE: (300, 340), R_KNEE: (340, 340), L_ANKLE: (300, 430), R_ANKLE: (340, 430),
    },
}

HEAD_OFFSET = {
    "crouching": (0, 140), "kneeling": (0, 50), "lying": (-200, 205), "leaning_forward": (64, 23),
}

GESTURE_OVERRIDES = {
    # forearms together in front of the chest (holding something small)
    "holding": {L_ELBOW: (290, 230), R_ELBOW: (350, 230), L_WRIST: (315, 215), R_WRIST: (325, 215)},
    # right arm straight out to the side/front
    "extending": {R_ELBOW: (400, 155), R_WRIST: (450, 160)},
}


def synthetic_person(posture: str, gesture: str | None = None, *, scale: float = 1.0,
                     image_size: tuple[int, int] = (640, 480)) -> PersonLandmarks:
    if posture not in LAYOUTS:
        raise ValueError(f"unknown synthetic posture {posture!r}; choose from {sorted(LAYOUTS)}")
    xy = np.zeros((33, 2), dtype=np.float32)
    vis = np.full(33, 0.05, dtype=np.float32)  # unplaced landmarks: invisible
    dx, dy = HEAD_OFFSET.get(posture, (0, 0))
    for i, (x, y) in BASE_HEAD.items():
        xy[i], vis[i] = (x + dx, y + dy), 0.95
    for i, p in LAYOUTS[posture].items():
        xy[i], vis[i] = p, 0.95
    if gesture:
        sh_off = np.array(LAYOUTS[posture][R_SHOULDER]) - np.array(LAYOUTS["standing"][R_SHOULDER])
        for i, p in GESTURE_OVERRIDES[gesture].items():
            xy[i] = np.array(p) + (sh_off if gesture == "extending" else (np.array(LAYOUTS[posture][L_SHOULDER]) - np.array(LAYOUTS["standing"][L_SHOULDER])))
            vis[i] = 0.95
    cx, cy = image_size[0] / 2, image_size[1] / 2
    xy = (xy - [cx, cy]) * scale + [cx, cy]
    return PersonLandmarks(xy=xy, visibility=vis, image_size=image_size)
