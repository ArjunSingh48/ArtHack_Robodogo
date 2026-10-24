"""Eligibility is independent of artistic matching scores and image rights."""
from copy import deepcopy
from pathlib import Path
import json
import random

import pytest
from pydantic import ValidationError
import yaml

from is_this_us.artworks import curation
from is_this_us.artworks.repository import YamlArtworkRepository
from is_this_us.domain import Artwork, EligibilityReview, HumanObservation, scene_is_eligible
from is_this_us.matching.matcher import SceneMatcher, WeightedRelationshipMatcher
from is_this_us.memes.repository import MemeRepository
from is_this_us.scenes import SceneLibrary

ROOT = Path(__file__).resolve().parents[1]


def review(**overrides):
    return dict(status="accepted", human_count=1, dog_count=1, dog_on_ground=True,
                reason="Synthetic schema fixture: one person and one grounded dog",
                reviewer="test fixture", reviewed_on="2026-10-24",
                image_reference="https://example.invalid/fixture.jpg", **overrides)


def artwork(**overrides):
    return dict(id="met:1", museum="The Metropolitan Museum of Art", museum_object_id="1",
                title="Synthetic eligible artwork", artist="Test artist", date="1900",
                image_url="https://example.invalid/fixture.jpg", page_url="https://example.invalid/1",
                rights=dict(status="CC0", source_statement="Synthetic rights fixture", verified_on="2026-10-01"),
                human=dict(posture="sitting", gestures=[], confidence=0.9),
                dog=dict(posture="lying", relation="at_feet", interaction="resting_together",
                         gaze_at_human=False, reproducible="yes", robot_behavior="lie_down", confidence=0.9),
                review=review(), **overrides)


def write_archive(tmp_path, rows):
    (tmp_path / "annotations.yaml").write_text(yaml.safe_dump({"version": 1, "artworks": rows}))


def write_notes(tmp_path, rows):
    notes = {a["id"]: {k: a[k] for k in ("human", "dog", "review") if k in a} for a in rows}
    (tmp_path / "curation_notes.yaml").write_text(yaml.safe_dump({"notes": notes}))


@pytest.mark.parametrize("field,value", [("human_count", 0), ("human_count", 2), ("dog_count", 2),
                                         ("dog_on_ground", False), ("dog_on_ground", None),
                                         ("human_count", None), ("reviewer", " "),
                                         ("image_reference", ""), ("reason", ""),
                                         ("reviewed_on", "not-a-date"), ("human_count", True)])
def test_acceptance_requires_observations_and_provenance(field, value):
    data = review()
    data[field] = value
    with pytest.raises(ValidationError):
        EligibilityReview.model_validate(data)


@pytest.mark.parametrize("posture,relation", [("held", "beside"), ("sitting", "on_lap"), ("sitting", "in_arms")])
def test_ground_review_cannot_contradict_pose(posture, relation):
    data = artwork()
    data["dog"].update(posture=posture, relation=relation)
    with pytest.raises(ValidationError):
        Artwork.model_validate(data)


def test_repositories_keep_history_but_selection_fails_closed(tmp_path):
    rows = [artwork()]
    for i, status in enumerate(["rejected", "needs_review", None], 2):
        row = artwork()
        row["id"] = f"met:{i}"
        if status:
            row["review"].update(status=status, dog_on_ground=None)
        else:
            row.pop("review")
            row["is_fixture"] = True  # a flag cannot bypass production eligibility
        rows.append(row)
    write_archive(tmp_path, rows)
    repo = YamlArtworkRepository(tmp_path)
    assert [a.id for a in repo.all()] == ["met:1"]
    assert len(repo.all_records()) == 4 and repo.get("met:4") is not None
    obs = HumanObservation(posture="sitting", posture_confidence=0.9)
    assert [c.scene.id for c in WeightedRelationshipMatcher().rank(obs, repo.all_records())] == ["met:1"]


def test_offline_build_filters_and_preserves_museum_snapshot(tmp_path, monkeypatch):
    accepted, rejected, missing = artwork(), artwork(), artwork()
    rejected["id"] = "met:2"
    rejected["review"].update(status="rejected", human_count=2)
    missing["id"] = "met:3"
    missing.pop("review")
    rows = [accepted, rejected, missing]
    write_archive(tmp_path, rows)
    write_notes(tmp_path, rows)
    metadata = [{k: v for k, v in a.items() if k not in {"human", "dog", "review"}} for a in rows]
    snapshot = tmp_path / "museum_metadata.yaml"
    snapshot.write_text(yaml.safe_dump({"artworks": metadata}))
    before = snapshot.read_bytes()
    monkeypatch.setattr(curation, "MetClient", lambda: pytest.fail("offline build accessed network client"))
    built, problems = curation.build(tmp_path, offline=True)
    assert not problems and [a.id for a in built] == ["met:1"]
    assert built[0].rights.verified_on == "2026-10-01"
    assert built[0].title == accepted["title"] and built[0].image_url == Artwork(**accepted).image_url
    assert snapshot.read_bytes() == before
    assert [a.id for a in YamlArtworkRepository(tmp_path).all()] == ["met:1"]
    # Re-accepting a candidate can reuse the complete snapshot after a narrowed build.
    rejected["review"].update(status="accepted", human_count=1)
    write_notes(tmp_path, [accepted, rejected, missing])
    assert len(curation.build(tmp_path, offline=True)[0]) == 2


@pytest.mark.parametrize("failure", ["contradictory_review", "unknown_id", "uncleared_rights"])
def test_failed_build_does_not_replace_previous_collection(tmp_path, failure):
    row = artwork()
    write_archive(tmp_path, [row])
    previous = (tmp_path / "annotations.yaml").read_bytes()
    note = deepcopy(row)
    if failure == "contradictory_review":
        note["review"]["human_count"] = 2
    elif failure == "unknown_id":
        note["id"] = "met:99"
    else:
        row["rights"]["status"] = "Copyrighted"
        (tmp_path / "museum_metadata.yaml").write_text(yaml.safe_dump({"artworks": [row]}))
    write_notes(tmp_path, [note])
    built, problems = curation.build(tmp_path, offline=True)
    assert built == [] and problems
    assert (tmp_path / "annotations.yaml").read_bytes() == previous


def test_live_fetch_failure_does_not_replace_collection(tmp_path, monkeypatch):
    row = artwork()
    write_archive(tmp_path, [row])
    write_notes(tmp_path, [row])
    before = (tmp_path / "annotations.yaml").read_bytes()
    def fail(*args, **kwargs):
        raise curation.RightsError("Museum rights changed")
    monkeypatch.setattr(curation, "fetch_record", fail)
    assert curation.build(tmp_path)[1]
    assert (tmp_path / "annotations.yaml").read_bytes() == before


def test_empty_approved_collection_builds_and_matches_nothing(tmp_path):
    row = artwork()
    row["review"].update(status="needs_review", dog_on_ground=None)
    write_notes(tmp_path, [row])
    built, problems = curation.build(tmp_path, offline=True)
    assert not built and not problems
    repo = YamlArtworkRepository(tmp_path)
    matcher = SceneMatcher(SceneLibrary(repo))
    for source in ("art", "meme", "both", "surprise"):
        result = matcher.match(HumanObservation(posture="sitting", posture_confidence=0.9), source)
        assert not result.found and result.ranked == []


def test_all_archive_modes_exclude_unreviewed_memes(tmp_path):
    art_dir, meme_dir = tmp_path / "art", tmp_path / "memes"
    art_dir.mkdir()
    (meme_dir / "annotations").mkdir(parents=True)
    write_archive(art_dir, [artwork()])
    raw = json.loads((ROOT / "data/memes/annotations/cheems.json").read_text())
    (meme_dir / "annotations/unreviewed.json").write_text(json.dumps(raw))
    eligible = deepcopy(raw)
    eligible.update(id="meme:synthetic-grounded-pair", name="Synthetic grounded pair", is_fixture=True, review=review())
    (meme_dir / "annotations/eligible.json").write_text(json.dumps(eligible))
    memes = MemeRepository(meme_dir)
    assert len(memes.all_records()) == 2 and len(memes.all()) == 1
    assert memes.get(raw["id"]) is not None
    library = SceneLibrary(YamlArtworkRepository(art_dir), memes)
    matcher = SceneMatcher(library)
    obs = HumanObservation(posture="sitting", posture_confidence=0.9)
    for source in ("art", "meme", "both", "surprise"):
        result = matcher.match(obs, source, rng=random.Random(3))
        assert result.found
        assert all(scene_is_eligible(c.scene) and c.scene.id != raw["id"] for c in result.ranked)
    assert {c.scene.kind for c in matcher.match(obs, "both").ranked} == {"art", "meme"}


def test_meme_catalog_list_keeps_unreviewed_records_visible(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from is_this_us import cli

    (tmp_path / "annotations").mkdir()
    raw = json.loads((ROOT / "data/memes/annotations/cheems.json").read_text())
    (tmp_path / "annotations/unreviewed.json").write_text(json.dumps(raw))
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(meme_data_dir=tmp_path))
    cli.cmd_memes(SimpleNamespace(action="list", drafts=False))
    output = capsys.readouterr().out
    assert "meme:cheems" in output and "curation: approved" in output and "eligible: no" in output


def test_live_build_fetches_only_accepted_ids(tmp_path, monkeypatch):
    from is_this_us.artworks.museum_clients import MuseumRecord

    accepted, rejected, missing = artwork(), artwork(), artwork()
    rejected["id"] = "met:2"
    rejected["review"].update(status="rejected", human_count=2)
    missing["id"] = "met:3"
    missing.pop("review")
    write_notes(tmp_path, [accepted, rejected, missing])
    seen = []
    def fetch(key, **kwargs):
        seen.append(key)
        return MuseumRecord(museum=accepted["museum"], museum_object_id="1", title="Current title",
                            artist="Current artist", date="1901", image_url=accepted["image_url"],
                            page_url=accepted["page_url"], rights_status="CC0",
                            rights_statement="Live fixture evidence", verified_on="2026-10-24")
    monkeypatch.setattr(curation, "fetch_record", fetch)
    built, problems = curation.build(tmp_path)
    assert seen == ["met:1"] and not problems
    assert built[0].title == "Current title" and built[0].rights.verified_on == "2026-10-24"
