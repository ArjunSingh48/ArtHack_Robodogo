import random

from is_this_us.domain import HumanGesture, HumanObservation, HumanPosture, Proximity
from is_this_us.matching.matcher import MatchSettings, WeightedRelationshipMatcher, posture_similarity
from is_this_us.vision.interpreter import GeometricSceneInterpreter
from is_this_us.vision.synthetic import synthetic_person

it = GeometricSceneInterpreter()
deterministic = WeightedRelationshipMatcher(MatchSettings(tie_margin=0.0))


def obs(posture, conf=0.9, gestures=None, proximity=Proximity.MEDIUM):
    return HumanObservation(posture=posture, posture_confidence=conf, gestures=gestures or {}, proximity=proximity)


def test_posture_similarity_is_graded():
    assert posture_similarity(HumanPosture.SITTING, HumanPosture.SITTING) == 1.0
    assert posture_similarity(HumanPosture.KNEELING, HumanPosture.CROUCHING) > posture_similarity(
        HumanPosture.KNEELING, HumanPosture.STANDING)


def test_sitting_finds_dog_at_feet(mock_repo):
    r = deterministic.match(obs(HumanPosture.SITTING), mock_repo.all(), rng=random.Random(0))
    assert r.chosen.artwork.id == "mock:feet"


def test_arms_holding_finds_dog_in_arms(mock_repo):
    o = obs(HumanPosture.STANDING, gestures={HumanGesture.ARMS_HOLDING: 1.0}, proximity=Proximity.CLOSE)
    assert deterministic.match(o, mock_repo.all()).chosen.artwork.id == "mock:held"


def test_kneel_and_reach_finds_offering(mock_repo):
    o = obs(HumanPosture.KNEELING, gestures={HumanGesture.EXTENDING_ARM: 1.0})
    assert deterministic.match(o, mock_repo.all()).chosen.artwork.id == "mock:offer"


def test_far_standing_finds_dog_apart(mock_repo):
    o = obs(HumanPosture.STANDING, proximity=Proximity.FAR)
    assert deterministic.match(o, mock_repo.all()).chosen.artwork.id == "mock:apart"


def test_relationship_not_visual_similarity(mock_repo):
    """Same seated posture, but holding → a held dog beats the seated-human painting."""
    o = obs(HumanPosture.SITTING, gestures={HumanGesture.ARMS_HOLDING: 1.0}, proximity=Proximity.CLOSE)
    ranked = deterministic.match(o, mock_repo.all()).ranked
    held = next(c for c in ranked if c.artwork.id == "mock:held")
    jump = next(c for c in ranked if c.artwork.id == "mock:jump")
    assert held.scores.relationship > jump.scores.relationship


def test_exclude_and_novelty(mock_repo):
    o = obs(HumanPosture.SITTING)
    r = deterministic.match(o, mock_repo.all(), exclude=["mock:feet"])
    assert r.chosen.artwork.id != "mock:feet"
    r2 = deterministic.match(o, mock_repo.all(), recent=["mock:feet"])
    feet = next(c for c in r2.ranked if c.artwork.id == "mock:feet")
    assert feet.scores.novelty == 0.0


def test_seeded_bounded_randomness_is_reproducible(repo):
    m = WeightedRelationshipMatcher()
    o = it.interpret([synthetic_person("sitting")])
    a = [m.match(o, repo.all(), rng=random.Random(42)).chosen.artwork.id for _ in range(3)]
    b = [m.match(o, repo.all(), rng=random.Random(42)).chosen.artwork.id for _ in range(3)]
    assert a == b
    picks = {m.match(o, repo.all(), rng=random.Random(s)).chosen.artwork.id for s in range(30)}
    assert 1 < len(picks) <= 3  # varies, but only among near-ties


def test_unknown_low_score_is_no_match(mock_repo):
    strict = WeightedRelationshipMatcher(MatchSettings(min_score=0.99))
    r = strict.match(obs(HumanPosture.SITTING), mock_repo.all())
    assert not r.found and "no painting" in r.reason


def test_nobody_is_no_match(repo):
    r = deterministic.match(HumanObservation(posture="unknown", posture_confidence=0, people_detected=0), repo.all())
    assert not r.found


def test_every_synthetic_posture_finds_a_real_painting(repo):
    m = WeightedRelationshipMatcher()
    for posture in ["standing", "sitting", "crouching", "kneeling", "lying", "leaning_forward"]:
        r = m.match(it.interpret([synthetic_person(posture)]), repo.all(), rng=random.Random(1))
        assert r.found, posture
        assert not r.chosen.artwork.is_fixture
