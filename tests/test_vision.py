from pathlib import Path

import numpy as np
import pytest

from is_this_us.domain import HumanGesture, HumanPosture, Proximity
from is_this_us.vision.interpreter import GeometricSceneInterpreter
from is_this_us.vision.pose import L_ANKLE, L_KNEE, R_ANKLE, R_KNEE, select_primary_person
from is_this_us.vision.synthetic import synthetic_person

it = GeometricSceneInterpreter()


@pytest.mark.parametrize("posture", ["standing", "sitting", "crouching", "kneeling", "lying", "leaning_forward"])
def test_each_synthetic_posture_is_recognised(posture):
    obs = it.interpret([synthetic_person(posture)])
    assert obs.posture == HumanPosture(posture)
    assert obs.posture_confidence >= 0.5


def test_scale_invariance():
    small = it.interpret([synthetic_person("kneeling", scale=0.5)])
    big = it.interpret([synthetic_person("kneeling", scale=1.3)])
    assert small.posture == big.posture == HumanPosture.KNEELING


def test_gestures():
    assert it.interpret([synthetic_person("standing", "holding")]).has(HumanGesture.ARMS_HOLDING)
    assert it.interpret([synthetic_person("kneeling", "extending")]).has(HumanGesture.EXTENDING_ARM)
    assert not it.interpret([synthetic_person("standing")]).has(HumanGesture.EXTENDING_ARM)
    # arms resting along the body while lying must not read as "extending"
    assert not it.interpret([synthetic_person("lying")]).has(HumanGesture.EXTENDING_ARM)


def test_proximity_from_body_size():
    assert it.interpret([synthetic_person("standing", scale=0.4)]).proximity is Proximity.FAR
    assert it.interpret([synthetic_person("standing", scale=1.4)]).proximity is Proximity.CLOSE


def test_nobody():
    obs = it.interpret([])
    assert obs.posture is HumanPosture.UNKNOWN and obs.people_detected == 0


def test_occluded_legs_are_honest():
    p = synthetic_person("standing")
    p.visibility[[L_KNEE, R_KNEE, L_ANKLE, R_ANKLE]] = 0.0
    obs = it.interpret([p])
    assert obs.posture is HumanPosture.UNKNOWN
    assert any("legs not visible" in n for n in obs.notes)


def test_multiple_people_selects_most_prominent():
    near = synthetic_person("kneeling", scale=1.2)
    far = synthetic_person("standing", scale=0.4)
    chosen, reason = select_primary_person([far, near])
    assert chosen is near and "2 people" in reason
    obs = it.interpret([far, near])
    assert obs.people_detected == 2 and obs.posture is HumanPosture.KNEELING


MODEL = Path(__file__).resolve().parents[1] / "models" / "pose_landmarker_lite.task"


@pytest.mark.skipif(not MODEL.exists(), reason="run `itu setup` to download the pose model")
def test_mediapipe_on_a_real_painting(repo):
    """Integration: MediaPipe + rules on Murillo's standing figure (cached museum image)."""
    from is_this_us.vision.frame_sources import decode_image_bytes
    from is_this_us.vision.pose import MediaPipePoseEstimator

    art = repo.get("met:437173")
    path = repo.local_image(art)
    if path is None:
        pytest.skip("images not cached; run `itu artworks cache`")
    est = MediaPipePoseEstimator(MODEL)
    try:
        obs = it.interpret(est.estimate(decode_image_bytes(path.read_bytes())))
    finally:
        est.close()
    assert obs.posture is HumanPosture.STANDING


def test_decode_image_bytes_is_in_memory(tmp_path):
    from io import BytesIO

    from PIL import Image

    from is_this_us.vision.frame_sources import decode_image_bytes

    buf = BytesIO()
    Image.new("RGB", (8, 6), (10, 20, 30)).save(buf, format="JPEG")
    arr = decode_image_bytes(buf.getvalue())
    assert arr.shape == (6, 8, 3) and arr.dtype == np.uint8
    assert list(tmp_path.iterdir()) == []
