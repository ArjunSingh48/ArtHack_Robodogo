"""Scene interpretation: landmarks -> coarse, honest `HumanObservation`.

Transparent geometric rules on landmarks normalised by torso length (shoulder-mid to
hip-mid), so body size and camera distance largely cancel out. Every rule produces a
soft score in [0, 1]; the posture with the highest score wins and confidence reflects
both the margin over the runner-up and landmark visibility.

What this deliberately does NOT do: infer emotion, intention, identity, or exact 3-D
relationships from a single image.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..domain import HumanGesture, HumanObservation, HumanPosture, Proximity
from .pose import (
    L_ANKLE, L_EAR, L_ELBOW, L_HIP, L_KNEE, L_SHOULDER, L_WRIST, NOSE,
    R_ANKLE, R_EAR, R_ELBOW, R_HIP, R_KNEE, R_SHOULDER, R_WRIST,
    PersonLandmarks, select_primary_person,
)

VIS = 0.5  # minimum visibility for a joint to be trusted


def _angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """Angle at b (degrees) between segments b->a and b->c."""
    v1, v2 = a - b, c - b
    n = np.linalg.norm(v1) * np.linalg.norm(v2)
    if n < 1e-6:
        return 180.0
    return math.degrees(math.acos(float(np.clip(np.dot(v1, v2) / n, -1.0, 1.0))))


def _from_vertical(v: np.ndarray) -> float:
    """Angle (degrees) between vector v and the vertical axis, 0 = vertical."""
    n = np.linalg.norm(v)
    if n < 1e-6:
        return 0.0
    return math.degrees(math.acos(min(1.0, abs(float(v[1])) / n)))


def _ramp(x: float, lo: float, hi: float) -> float:
    """0 below lo, 1 above hi, linear in between (works for lo > hi too)."""
    if lo == hi:
        return float(x >= hi)
    t = (x - lo) / (hi - lo)
    return float(min(1.0, max(0.0, t)))


@dataclass
class PostureFeatures:
    torso_len: float
    torso_tilt: float  # deg from vertical
    legs_visible: bool
    knee_angle: float | None = None  # hip-knee-ankle, 180 = straight
    hip_angle: float | None = None  # shoulder-hip-knee
    thigh_tilt: float | None = None  # hip->knee from vertical
    hip_above_ankle: float | None = None  # (ankle_y - hip_y) / torso
    knee_above_ankle: float | None = None  # (ankle_y - knee_y) / torso
    extras: dict[str, float] = field(default_factory=dict)


class GeometricSceneInterpreter:
    """Implements the SceneInterpreter role for the MVP."""

    def interpret(self, people: list[PersonLandmarks], source: str = "camera") -> HumanObservation:
        person, reason = select_primary_person(people)
        if person is None:
            return HumanObservation(
                posture=HumanPosture.UNKNOWN, posture_confidence=0.0, people_detected=0,
                notes=[reason], source=source,
            )
        obs = self.interpret_person(person, source=source)
        obs.people_detected = len(people)
        obs.notes.insert(0, reason)
        return obs

    # ------------------------------------------------------------------ features
    def features(self, p: PersonLandmarks) -> PostureFeatures | None:
        xy = p.xy
        if p.vis(L_SHOULDER, R_SHOULDER) < VIS or max(p.visibility[L_HIP], p.visibility[R_HIP]) < VIS:
            return None
        sh = (xy[L_SHOULDER] + xy[R_SHOULDER]) / 2
        hip = (xy[L_HIP] + xy[R_HIP]) / 2
        torso = sh - hip
        torso_len = float(np.linalg.norm(torso))
        if torso_len < 5:
            return None
        f = PostureFeatures(torso_len=torso_len, torso_tilt=_from_vertical(torso), legs_visible=False)

        legs = []
        for h, k, a in ((L_HIP, L_KNEE, L_ANKLE), (R_HIP, R_KNEE, R_ANKLE)):
            if p.vis(h, k) >= VIS:
                legs.append((h, k, a, p.visibility[a] >= VIS))
        if not legs:
            return f
        f.legs_visible = True
        # Use the most visible leg(s): average the features that are available.
        thigh, hipang, knee, hip_y, knee_y = [], [], [], [], []
        for h, k, a, ankle_ok in legs:
            thigh.append(_from_vertical(xy[k] - xy[h]))
            hipang.append(_angle(sh, xy[h], xy[k]))
            if ankle_ok:
                knee.append(_angle(xy[h], xy[k], xy[a]))
                hip_y.append((xy[a][1] - xy[h][1]) / torso_len)
                knee_y.append((xy[a][1] - xy[k][1]) / torso_len)
        f.thigh_tilt = float(np.mean(thigh))
        f.hip_angle = float(np.mean(hipang))
        if knee:
            f.knee_angle = float(np.mean(knee))
            f.hip_above_ankle = float(np.mean(hip_y))
            f.knee_above_ankle = float(np.mean(knee_y))
        return f

    # ------------------------------------------------------------------ posture
    def posture_scores(self, f: PostureFeatures) -> dict[HumanPosture, float]:
        s: dict[HumanPosture, float] = {}
        tilt = f.torso_tilt
        upright = _ramp(tilt, 40, 15)  # 1 when torso near vertical
        horizontal = _ramp(tilt, 50, 75)

        s[HumanPosture.LYING] = horizontal
        s[HumanPosture.RECLINING] = _ramp(tilt, 25, 40) * _ramp(tilt, 75, 55)

        if not f.legs_visible or f.knee_angle is None:
            # Lower body occluded: we can only say "upright" vs not. Stay honest.
            s[HumanPosture.UNKNOWN] = 0.6 * upright
            if f.legs_visible and f.thigh_tilt is not None:
                s[HumanPosture.SITTING] = upright * _ramp(f.thigh_tilt, 40, 65) * 0.8
                s[HumanPosture.STANDING] = upright * _ramp(f.thigh_tilt, 30, 12) * 0.7
            return s

        # Reclining means the legs go forward (hips bent), unlike leaning forward on straight legs.
        s[HumanPosture.RECLINING] *= _ramp(f.thigh_tilt, 30, 55)
        straight_legs = _ramp(f.knee_angle, 140, 165)
        bent_legs = _ramp(f.knee_angle, 140, 100)
        thigh_vertical = _ramp(f.thigh_tilt, 35, 15)
        thigh_horizontal = _ramp(f.thigh_tilt, 40, 65)
        tall = _ramp(f.hip_above_ankle, 1.0, 1.5)
        low = _ramp(f.hip_above_ankle, 0.9, 0.5)
        knee_on_floor = _ramp(f.knee_above_ankle, 0.45, 0.15)

        s[HumanPosture.STANDING] = upright * straight_legs * thigh_vertical * tall
        s[HumanPosture.LEANING_FORWARD] = (
            _ramp(tilt, 18, 35) * _ramp(tilt, 70, 55) * thigh_vertical * _ramp(f.knee_angle, 120, 150)
        )
        s[HumanPosture.SITTING] = _ramp(tilt, 50, 30) * thigh_horizontal * _ramp(f.knee_angle, 50, 75) * (1 - low * 0.5)
        s[HumanPosture.CROUCHING] = bent_legs * low * _ramp(f.knee_angle, 15, 35) * (1 - knee_on_floor * 0.7)
        s[HumanPosture.KNEELING] = knee_on_floor * _ramp(f.thigh_tilt, 70, 40) * _ramp(tilt, 55, 30) * _ramp(f.knee_angle, 150, 120)
        # Lying with legs extended along the floor
        s[HumanPosture.LYING] = max(s[HumanPosture.LYING], horizontal * _ramp(f.thigh_tilt, 50, 75))
        return s

    # ------------------------------------------------------------------ gestures
    def gestures(self, p: PersonLandmarks, f: PostureFeatures) -> dict[HumanGesture, float]:
        xy, g = p.xy, {}
        T = f.torso_len
        ext = 0.0
        for s_i, e_i, w_i in ((L_SHOULDER, L_ELBOW, L_WRIST), (R_SHOULDER, R_ELBOW, R_WRIST)):
            if p.vis(s_i, e_i, w_i) < VIS:
                continue
            straight = _ramp(_angle(xy[s_i], xy[e_i], xy[w_i]), 130, 160)
            reach = _ramp(float(np.linalg.norm(xy[w_i] - xy[s_i])) / T, 0.6, 0.9)
            # angle between the arm and the torso-down direction: 0 = arm resting along the body
            hip_mid = (xy[L_HIP] + xy[R_HIP]) / 2
            arm_vs_torso = _angle(xy[w_i], xy[s_i], xy[s_i] + (hip_mid - (xy[L_SHOULDER] + xy[R_SHOULDER]) / 2))
            away_from_body = _ramp(arm_vs_torso, 25, 50)
            # ...and against gravity: arms hanging down while leaning are not "extended"
            away_from_vertical = _ramp(_from_vertical(xy[w_i] - xy[s_i]), 25, 50)
            ext = max(ext, straight * reach * min(away_from_body, away_from_vertical))
        if ext > 0:
            g[HumanGesture.EXTENDING_ARM] = round(ext, 2)

        if p.vis(L_WRIST, R_WRIST, L_ELBOW, R_ELBOW) >= VIS:
            sh_mid = (xy[L_SHOULDER] + xy[R_SHOULDER]) / 2
            hip_mid = (xy[L_HIP] + xy[R_HIP]) / 2
            wrists_mid = (xy[L_WRIST] + xy[R_WRIST]) / 2
            together = _ramp(float(np.linalg.norm(xy[L_WRIST] - xy[R_WRIST])) / T, 0.7, 0.35)
            centered = _ramp(abs(float(wrists_mid[0] - hip_mid[0])) / T, 0.6, 0.3)
            in_torso = float(sh_mid[1] - 0.1 * T <= wrists_mid[1] <= hip_mid[1] + 0.2 * T)
            bent = _ramp((_angle(xy[L_SHOULDER], xy[L_ELBOW], xy[L_WRIST]) +
                          _angle(xy[R_SHOULDER], xy[R_ELBOW], xy[R_WRIST])) / 2, 130, 95)
            hold = together * centered * in_torso * bent
            if hold > 0:
                g[HumanGesture.ARMS_HOLDING] = round(hold, 2)

        # Looking down: nose clearly below the ear line. Capped at 0.6 — weak evidence.
        if p.vis(NOSE) >= VIS and max(p.visibility[L_EAR], p.visibility[R_EAR]) >= VIS:
            ears = [xy[i] for i in (L_EAR, R_EAR) if p.visibility[i] >= VIS]
            ear_y = float(np.mean([e[1] for e in ears]))
            down = _ramp((float(xy[NOSE][1]) - ear_y) / T, 0.12, 0.3) * 0.6
            if down > 0:
                g[HumanGesture.LOOKING_DOWN] = round(down, 2)

        # Facing the camera (= the robot, when the camera is on the robot).
        shoulder_w = float(np.linalg.norm(xy[L_SHOULDER] - xy[R_SHOULDER])) / T
        nose_v = float(p.visibility[NOSE])
        toward = _ramp(shoulder_w, 0.35, 0.6) * _ramp(nose_v, 0.4, 0.8)
        if toward > 0.3:
            g[HumanGesture.TURNED_TOWARD] = round(toward, 2)
        elif nose_v < 0.3 and shoulder_w > 0.3:
            g[HumanGesture.TURNED_AWAY] = round(1 - nose_v, 2)
        return g

    # ------------------------------------------------------------------ proximity
    @staticmethod
    def proximity(p: PersonLandmarks) -> Proximity:
        _, y0, _, y1 = p.bbox()
        frac = (y1 - y0) / max(p.image_size[1], 1)
        if frac > 0.75:
            return Proximity.CLOSE
        if frac > 0.4:
            return Proximity.MEDIUM
        return Proximity.FAR

    # ------------------------------------------------------------------ main
    def interpret_person(self, p: PersonLandmarks, source: str = "camera") -> HumanObservation:
        f = self.features(p)
        landmark_conf = round(p.mean_visibility, 2)
        if f is None:
            return HumanObservation(
                posture=HumanPosture.UNKNOWN, posture_confidence=0.0, landmark_confidence=landmark_conf,
                proximity=self.proximity(p), notes=["shoulders or hips not visible"], source=source,
            )
        scores = self.posture_scores(f)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, best_s = ranked[0] if ranked else (HumanPosture.UNKNOWN, 0.0)
        second_s = ranked[1][1] if len(ranked) > 1 else 0.0
        notes = []
        if best_s < 0.25:
            best, notes = HumanPosture.UNKNOWN, ["no posture rule clearly matched"]
            confidence = 0.2
        else:
            margin = (best_s - second_s) / max(best_s, 1e-6)
            confidence = best_s * (0.5 + 0.5 * margin) * (0.6 + 0.4 * landmark_conf)
        if not f.legs_visible:
            notes.append("legs not visible; posture inferred from upper body only")
        return HumanObservation(
            posture=best,
            posture_confidence=round(float(min(1.0, confidence)), 2),
            gestures=self.gestures(p, f),
            proximity=self.proximity(p),
            landmark_confidence=landmark_conf,
            notes=notes,
            source=source,
        )
