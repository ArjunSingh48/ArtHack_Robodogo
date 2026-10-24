"""A photography event selects cultural imitations, not human/dog pose matches."""
import json
import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from is_this_us.domain import HumanObservation, InteractionIntent, RobotBehavior
from is_this_us.matching.matcher import SceneMatcher
from is_this_us.memes import curation
from is_this_us.memes.model import Meme
from is_this_us.memes.repository import MemeRepository
from is_this_us.motion.planner import SafeMotionPlanner
from is_this_us.scenes import SceneLibrary

MEMES = Path(__file__).resolve().parents[1] / "data" / "memes"


@pytest.fixture
def photo_library(repo):
    return SceneLibrary(repo, MemeRepository(MEMES))


def event(**values):
    return HumanObservation(posture="unknown", posture_confidence=0,
                            intent=InteractionIntent.PHOTOGRAPHING_ROBOT, **values)


def test_folder_has_five_photo_poses_but_no_relationship_memes(photo_library):
    assert len(photo_library.photo_poses()) == 5
    assert photo_library.all("meme") == []
    assert curation.validate(MEMES) == []
    for meme in photo_library.photo_poses():
        assert meme.human.posture == "unknown" and meme.human.confidence == 0
        assert meme.photo_pose.review_basis == "existing_annotations"
        assert meme.dog.reproducible == "approximate"
        assert not meme.displayable and photo_library.local_image(meme) is None
    draft = Meme.model_validate_json((MEMES / "drafts/buff-doge-vs-cheems.json").read_text())
    assert draft.photo_pose.status == "rejected" and not draft.photo_pose_eligible


def test_random_selection_ignores_photographer_pose_and_is_seeded(photo_library):
    matcher = SceneMatcher(photo_library)
    a = matcher.match_photo_opportunity(event(), rng=random.Random(4))
    b = matcher.match_photo_opportunity(
        HumanObservation(posture="standing", posture_confidence=1), rng=random.Random(4))
    assert a.chosen.scene.id == b.chosen.scene.id
    assert "no posture score" in a.reason
    assert all(c.scores.total == c.scores.posture == c.scores.relationship == 0 for c in a.ranked)
    selected = {matcher.match_photo_opportunity(event(), rng=random.Random(seed)).chosen.scene.id
                for seed in range(80)}
    assert selected == {m.id for m in photo_library.photo_poses()}


def test_photo_pool_respects_exclusions_and_recent_history(photo_library):
    matcher = SceneMatcher(photo_library)
    ids = [m.id for m in photo_library.photo_poses()]
    result = matcher.match_photo_opportunity(event(), recent=ids[:-1], rng=random.Random(0))
    assert result.chosen.scene.id == ids[-1]
    assert matcher.match_photo_opportunity(event(), recent=ids).found
    assert not matcher.match_photo_opportunity(event(), exclude=ids).found
    assert not matcher.match_photo_opportunity(event(people_detected=0)).found


@pytest.mark.parametrize("change", ["missing_review", "pending", "draft", "unknown_rights", "not_reproducible"])
def test_unapproved_photo_pose_never_enters_pool(tmp_path, change):
    raw = json.loads((MEMES / "annotations/doge.json").read_text())
    if change == "missing_review":
        raw.pop("photo_pose")
    elif change == "pending":
        raw["photo_pose"]["status"] = "needs_review"
    elif change == "draft":
        raw["curation_status"] = "draft"
    elif change == "unknown_rights":
        raw["image"]["rights"] = "unknown"
    else:
        raw["dog"]["reproducible"] = "no"
    (tmp_path / "annotations").mkdir()
    (tmp_path / "annotations/example.json").write_text(json.dumps(raw))
    assert MemeRepository(tmp_path, include_drafts=True).photo_poses() == []


def test_photo_review_requires_auditable_mapping():
    raw = json.loads((MEMES / "annotations/doge.json").read_text())
    raw["photo_pose"]["approximation"] = " "
    with pytest.raises(ValidationError):
        Meme.model_validate(raw)


def test_photo_mapping_preserves_operator_approval(photo_library):
    planner = SafeMotionPlanner()
    for meme_id, behavior in [("meme:side-eye-dog", RobotBehavior.LOOK_ASIDE),
                              ("meme:long-nose-borzoi", RobotBehavior.PEER)]:
        meme = photo_library.get(meme_id)
        assert planner.plan(meme).behavior is RobotBehavior.STAND
        planner.approve(behavior)
        assert planner.plan(meme).behavior is behavior


async def test_installation_routes_event_and_retry_to_memes(make_orchestrator, photo_library):
    orch = make_orchestrator()
    orch.repo, orch.matcher = photo_library, SceneMatcher(photo_library)
    orch.storage.touch_visitor(1)
    first = await orch.observe_given(1, event(), origin="installation", archive="art")
    assert first.artwork.kind == "meme" and first.archive == "meme"
    assert "uniform random" in first.match.reason
    row = orch.storage.get_interaction(first.interaction_id, 1)
    assert row.observation["intent"] == "photographing_robot"
    another = await orch.another(1, first.interaction_id)
    assert another.artwork.kind == "meme" and another.artwork.id != first.artwork.id


async def test_uploaded_photo_does_not_trigger_photo_opportunity(make_orchestrator, photo_library):
    orch = make_orchestrator()
    orch.repo, orch.matcher = photo_library, SceneMatcher(photo_library)
    orch.storage.touch_visitor(1)
    obs = HumanObservation(posture="standing", posture_confidence=1,
                           intent=InteractionIntent.PHOTOGRAPHING_ROBOT)
    result = await orch.observe_given(1, obs, origin="photo", archive="art")
    assert result.artwork.kind == "art" and "photo opportunity" not in result.match.reason


async def test_empty_photo_pool_stays_still_without_art_fallback(make_orchestrator):
    orch = make_orchestrator()  # art-only library
    orch.storage.touch_visitor(1)
    result = await orch.observe_given(1, event(), origin="installation")
    assert result.artwork is None and result.plan.behavior is RobotBehavior.STILLNESS
    assert result.match.reason == "photo opportunity: no eligible meme pose"


def test_geometry_never_invents_photography_intent():
    from is_this_us.vision.interpreter import GeometricSceneInterpreter
    from is_this_us.vision.synthetic import synthetic_person

    obs = GeometricSceneInterpreter().interpret([synthetic_person("standing", "holding")])
    assert obs.intent is InteractionIntent.POSE


def test_simulation_rejects_photo_event_with_real_image(capsys):
    from is_this_us.cli import main

    with pytest.raises(SystemExit, match="synthetic event"):
        main(["simulate", "--photographing", "--image", "unused.jpg"])
