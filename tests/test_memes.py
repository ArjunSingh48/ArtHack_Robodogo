"""Meme memory: repository, rights policy, matching, archives, motion approval, dialogue,
Telegram flows, consented imitation photo, Imgflip discovery. All with MockRobotAdapter."""

from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
import numpy as np
import pytest
from pydantic import ValidationError

from is_this_us.bot.flows import CB_ANOTHER, CB_CONSENT, CB_DECLINE, CB_KEEP, CB_PHOTO, VisitorFlows
from is_this_us.dialogue.engine import DialogueEngine
from is_this_us.domain import (
    ArchiveSource, ExecutionResult, ExecutionStatus, HumanGesture, HumanObservation, HumanPosture,
    Reproducibility, RobotBehavior,
)
from is_this_us.matching.matcher import MemeMatcher, SceneMatcher
from is_this_us.memes import curation
from is_this_us.memes.model import Meme
from is_this_us.memes.repository import MemeRepository
from is_this_us.motion.planner import SafeMotionPlanner
from is_this_us.robot.mock import MockRobotAdapter
from is_this_us.scenes import SceneLibrary
from is_this_us.speech.synthesizer import AudioCache, MockSynthesizer
from is_this_us.vision.frame_sources import Frame
from tests.conftest import FakeEstimator

ROOT = Path(__file__).resolve().parents[1]
MEMES = ROOT / "data" / "memes"


@pytest.fixture
def memes() -> MemeRepository:
    return MemeRepository(MEMES)


@pytest.fixture
def library(repo, memes) -> SceneLibrary:
    return SceneLibrary(repo, memes)


def obs(posture, gestures=None, conf=0.85):
    return HumanObservation(posture=posture, posture_confidence=conf, gestures=gestures or {})


# ------------------------------------------------------------------ repository & rights

def test_curated_memes_load_and_are_reference_only(memes):
    ids = {m.id for m in memes.all()}
    assert ids == {"meme:doge", "meme:cheems", "meme:this-is-fine", "meme:side-eye-dog", "meme:long-nose-borzoi"}
    for m in memes.all():
        assert m.kind == "meme" and m.curation_status == "approved"
        assert not m.displayable and memes.local_image(m) is None
        assert m.sources and m.attribution and m.reflection.get("en")
    assert memes.errors == {}
    assert curation.validate(MEMES) == []


def test_no_meme_image_files_are_shipped():
    images = [p for p in MEMES.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}]
    assert images == []


def _meme_dict(**over):
    base = json.loads((MEMES / "annotations" / "cheems.json").read_text())
    base.update(over)
    return base


def test_rights_policy_is_enforced_by_schema():
    with pytest.raises(ValidationError):  # reference-only must not ship a file
        Meme.model_validate(_meme_dict(image={"rights": "reference_only", "local_file": "x.jpg"}))
    with pytest.raises(ValidationError):  # licensed needs a licence note
        Meme.model_validate(_meme_dict(image={"rights": "licensed", "local_file": "x.jpg", "license_note": ""}))
    with pytest.raises(ValidationError):  # behaviors come only from the approved vocabulary
        Meme.model_validate(_meme_dict(dog={**_meme_dict()["dog"], "robot_behavior": "backflip"}))


def test_adding_a_meme_is_just_a_json_file(tmp_path):
    """A team recreation (original photo they own) — added without touching robot code."""
    (tmp_path / "annotations").mkdir()
    (tmp_path / "recreations").mkdir()
    from PIL import Image

    Image.new("RGB", (40, 30), (200, 180, 160)).save(tmp_path / "recreations" / "nap.jpg")
    meme = _meme_dict(id="meme:team-nap", name="Team Nap", curation_status="approved",
                      dog={**_meme_dict()["dog"], "posture": "lying", "robot_behavior": "lie_down"},
                      image={"rights": "recreation", "local_file": "recreations/nap.jpg",
                             "license_note": "Original photo by the installation team, CC BY 4.0"})
    (tmp_path / "annotations" / "team-nap.json").write_text(json.dumps(meme))
    (tmp_path / "annotations" / "broken.json").write_text("{not json")
    repo = MemeRepository(tmp_path)
    m = repo.get("meme:team-nap")
    assert m and m.displayable and repo.local_image(m).name == "nap.jpg"
    assert "broken.json" in repo.errors
    assert SafeMotionPlanner().plan(m).behavior is RobotBehavior.LIE_DOWN


# ------------------------------------------------------------------ matching

@pytest.mark.parametrize("posture,gestures,expected", [
    (HumanPosture.SITTING, {HumanGesture.ARMS_HOLDING: 1.0}, "meme:this-is-fine"),
    (HumanPosture.LEANING_FORWARD, {HumanGesture.LOOKING_DOWN: 0.6}, "meme:long-nose-borzoi"),
    (HumanPosture.STANDING, {HumanGesture.TURNED_AWAY: 0.9}, "meme:side-eye-dog"),
    (HumanPosture.SITTING, {}, "meme:cheems"),
    (HumanPosture.STANDING, {HumanGesture.TURNED_TOWARD: 1.0}, "meme:doge"),
])
def test_meme_matcher_reads_gestures(memes, posture, gestures, expected):
    ranked = MemeMatcher().rank(obs(posture, gestures), memes.all())
    assert ranked[0].scene.id == expected


def test_scene_matcher_archives(library):
    m = SceneMatcher(library)
    o = obs(HumanPosture.SITTING, {HumanGesture.ARMS_HOLDING: 1.0})
    art = m.match(o, "art", rng=random.Random(1))
    meme = m.match(o, "meme", rng=random.Random(1))
    assert art.chosen.scene.kind == "art" and all(c.scene.kind == "art" for c in art.ranked)
    assert meme.chosen.scene.kind == "meme" and all(c.scene.kind == "meme" for c in meme.ranked)
    both = m.match(o, "both", rng=random.Random(1))
    assert {c.scene.kind for c in both.ranked} == {"art", "meme"}
    assert both.ranked == sorted(both.ranked, key=lambda c: c.scores.total, reverse=True)
    picked = {m.match(o, "surprise", rng=random.Random(s)).reason.split(":")[0] for s in range(20)}
    assert picked == {"surprise→art", "surprise→meme"}  # both memories, equal chance


def test_meme_archive_without_memes_falls_back_to_art(repo):
    r = SceneMatcher(SceneLibrary(repo, None)).match(obs(HumanPosture.SITTING), "meme", rng=random.Random(0))
    assert r.chosen.scene.kind == "art" and "no memes loaded" in r.reason


# ------------------------------------------------------------------ motion approval

def test_body_poses_need_operator_approval(memes):
    side_eye = memes.get("meme:side-eye-dog")
    planner = SafeMotionPlanner()
    p = planner.plan(side_eye)
    assert p.behavior is RobotBehavior.STAND and p.fidelity is Reproducibility.APPROXIMATE
    planner.approve(RobotBehavior.LOOK_ASIDE)
    p = planner.plan(side_eye)
    assert p.behavior is RobotBehavior.LOOK_ASIDE
    robot = MockRobotAdapter(time_scale=0.0)
    robot.connect()
    robot.execute(p.behavior)
    assert robot.calls[-2:] == ["Euler(0.0, 0.0, 0.25)", "Euler(0.0, 0.0, 0.0)"]
    planner.revoke(RobotBehavior.LOOK_ASIDE)
    assert planner.plan(side_eye).behavior is RobotBehavior.STAND


# ------------------------------------------------------------------ dialogue

def test_meme_dialogue_is_sincere(memes):
    eng = DialogueEngine("en")
    plan = SafeMotionPlanner().plan(memes.get("meme:this-is-fine"))
    res = ExecutionResult(behavior=plan.behavior, status=ExecutionStatus.SIMULATED)
    first = eng.compose(seed="a", observation=obs(HumanPosture.SITTING), artwork=memes.get("meme:this-is-fine"),
                        plan=plan, result=res, first_meme=True)
    assert first.lines[0] == "I found another kind of painting. Humans call it a meme."
    assert "I have been practicing this expression." in first.lines or "Let me try the face… with my whole body." in first.lines
    assert first.lines[-2:] == ["Did I do the meme correctly?", "Is this us?"]
    later = eng.compose(seed="b", observation=obs(HumanPosture.SITTING), artwork=memes.get("meme:this-is-fine"),
                        plan=plan, result=res, first_meme=False)
    assert later.lines[0] != first.lines[0]
    ru = DialogueEngine("ru").compose(seed="a", observation=obs(HumanPosture.SITTING),
                                      artwork=memes.get("meme:cheems"), first_meme=True)
    assert "мемом" in ru.lines[0] and "Это мы?" == ru.lines[-1]


# ------------------------------------------------------------------ Telegram flows

class Collect:
    def __init__(self):
        self.flows, self.sent = None, []

    async def deliver(self, chat_id, outcome):
        r = self.flows.replies_for_outcome(chat_id, outcome)
        self.sent.append((chat_id, r))
        return "ok"


class CountingCamera:
    """Imitation camera stand-in: a plain grey frame. Counts how often it is opened."""
    opened = 0

    def __init__(self):
        CountingCamera.opened += 1
        self.name = "test:imitation"

    def read(self):
        return Frame(np.full((300, 400, 3), 128, dtype=np.uint8), self.name)

    def close(self):
        pass


class StaticSource:
    name = "test:static"

    def read(self):
        return Frame(np.zeros((48, 64, 3), dtype=np.uint8), self.name)

    def close(self):
        pass


@pytest.fixture
def meme_flows(make_orchestrator, storage, library, tmp_path, monkeypatch):
    monkeypatch.setattr("is_this_us.orchestrator.decode_image_bytes", lambda b: np.zeros((4, 4, 3), np.uint8))
    monkeypatch.chdir(tmp_path)

    def _make(posture="sitting", gesture="holding", camera=True):
        CountingCamera.opened = 0
        notifier = Collect()
        orch = make_orchestrator(estimator=FakeEstimator(posture, gesture), notifier=notifier,
                                 synth=MockSynthesizer(AudioCache(tmp_path / "audio")))
        orch.repo = library
        orch.matcher = SceneMatcher(library)
        flows = VisitorFlows(orchestrator=orch, storage=storage, repo=library, operator_ids=[42],
                             frame_source_factory=StaticSource,
                             imitation_camera_factory=CountingCamera if camera else None)
        notifier.flows = flows
        return flows, orch, notifier
    return _make


def _iid(replies):
    return replies[1].buttons[0][0][1].split(":")[1]


async def test_meme_command_and_reference_only_delivery(meme_flows, storage):
    flows, orch, notifier = meme_flows()
    r = flows.set_archive(5, "en", "meme")
    assert "Memes" in r[0].text and storage.visitor_archive(5) == "meme"
    await flows.photo(5, b"x")
    (_, sent), = notifier.sent
    main = sent[1]
    assert main.kind == "text" and main.photo is None  # never an unlicensed image
    assert main.preview_url.startswith("https://")
    assert "This Is Fine" in main.text and "KC Green" in main.text and "image not reproduced" in main.text
    assert "Did I do the meme correctly?" in main.text
    # no imitation-photo button for a photo sent from anywhere (robot not there)
    assert not any(d.startswith(CB_PHOTO) for row in main.buttons for _, d in row)

    iid = _iid(sent)
    keep = await flows.callback(5, f"{CB_KEEP}:{iid}")
    assert keep[0].kind == "photo" and keep[0].photo[:2] == b"\xff\xd8"  # typographic card
    mem = await flows.memory(5)
    assert mem[1].kind == "text" and mem[1].preview_url

    await flows.callback(5, f"{CB_ANOTHER}:{iid}")
    row = storage.get_interaction(iid, 5)
    assert row.archive == "meme" and all(x.startswith("meme:") for x in row.shown_ids)


async def test_art_and_surprise_commands(meme_flows, storage):
    flows, orch, notifier = meme_flows()
    flows.set_archive(6, "en", "art")
    await flows.photo(6, b"x")
    assert storage.get_interaction(_iid(notifier.sent[0][1]), 6).artwork_id.startswith(("met:", "cma:"))
    assert "Memes" not in flows.set_archive(6, "en", "surprise")[0].text
    await flows.photo(6, b"x")
    assert storage.get_interaction(_iid(notifier.sent[1][1]), 6).archive == "surprise"


async def test_consented_imitation_photo(meme_flows, storage, tmp_path):
    flows, orch, notifier = meme_flows()
    flows.set_archive(7, "en", "meme")
    storage.pair(7, storage.create_pairing_token())
    await flows.observe(7)
    (_, sent), = notifier.sent
    buttons = [d for row in sent[1].buttons for _, d in row]
    photo_btn = next(d for d in buttons if d.startswith(CB_PHOTO))
    iid = photo_btn.split(":")[1]

    ask = await flows.callback(7, photo_btn)
    assert "only to you" in ask[0].text and CountingCamera.opened == 0  # nothing captured before consent
    assert (await flows.callback(7, f"{CB_DECLINE}:{iid}"))[0].kind == "text" and CountingCamera.opened == 0

    before = set(tmp_path.rglob("*"))
    got = await flows.callback(7, f"{CB_CONSENT}:{iid}")
    assert CountingCamera.opened == 1
    assert got[0].kind == "photo" and got[0].photo[:2] == b"\xff\xd8"
    new_files = {p for p in set(tmp_path.rglob("*")) - before if "audio" not in p.parts}
    assert new_files == set()  # diptych made in memory only

    stranger = await flows.callback(8, f"{CB_CONSENT}:{iid}")
    assert "passed" in stranger[0].text and CountingCamera.opened == 1


async def test_no_imitation_camera_means_no_button(meme_flows, storage):
    flows, orch, notifier = meme_flows(camera=False)
    storage.pair(9, storage.create_pairing_token())
    await flows.observe(9)
    buttons = [d for row in notifier.sent[0][1][1].buttons for _, d in row]
    assert not any(d.startswith(CB_PHOTO) for d in buttons)


def test_operator_approves_and_revokes_actions(meme_flows):
    flows, orch, _ = meme_flows()
    assert "operator" in flows.operator(1, "approve", "look_aside")[0].text
    assert "look_aside" in flows.operator(42, "approve", "look_aside")[0].text.split("\n")[0]
    assert RobotBehavior.LOOK_ASIDE in orch.planner.allowed
    flows.operator(42, "revoke", "look_aside")
    assert RobotBehavior.LOOK_ASIDE not in orch.planner.allowed
    assert "usage" in flows.operator(42, "approve", "backflip")[0].text


# ------------------------------------------------------------------ Telegram delivery reliability

async def test_send_replies_falls_back_and_retries():
    from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

    from is_this_us.bot.app import send_replies
    from is_this_us.bot.flows import Reply

    class FakeBot:
        def __init__(self):
            self.messages, self.rate_limited = [], True

        async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
            raise TelegramBadRequest(method=None, message="wrong file identifier/HTTP URL specified")

        async def send_message(self, chat_id, text, reply_markup=None, link_preview_options=None):
            if self.rate_limited:
                self.rate_limited = False
                raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
            self.messages.append((text, link_preview_options))

    bot = FakeBot()
    status = await send_replies(bot, 1, [Reply("photo", text="caption", photo="https://example.invalid/x.jpg"),
                                         Reply("text", text="meme", preview_url="https://knowyourmeme.com/memes/cheems")])
    assert status == "sent 2/2 message(s)"
    assert bot.messages[0][0] == "caption"  # photo failed → text fallback
    assert bot.messages[1][1].url == "https://knowyourmeme.com/memes/cheems"


# ------------------------------------------------------------------ Imgflip discovery

def test_imgflip_discovery_writes_unloaded_drafts(tmp_path):
    (tmp_path / "annotations").mkdir()
    (tmp_path / "annotations" / "this-is-fine.json").write_text((MEMES / "annotations" / "this-is-fine.json").read_text())

    def handler(request):
        assert request.url.host == "api.imgflip.com"
        return httpx.Response(200, json={"success": True, "data": {"memes": [
            {"id": "55311130", "name": "This Is Fine", "url": "https://i.imgflip.com/wxica.jpg"},
            {"id": "1", "name": "Sad Shiba Dog", "url": "https://i.imgflip.com/a.jpg"},
            {"id": "2", "name": "Distracted Boyfriend", "url": "https://i.imgflip.com/b.jpg"},
        ]}})

    written = curation.discover(tmp_path, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [p.name for p in written] == ["sad-shiba-dog.json"]  # curated one skipped, non-dog ignored
    draft = json.loads(written[0].read_text())
    assert draft["image"]["rights"] == "unknown" and draft["curation_status"] == "draft"
    assert "not permission" in draft["notes"] and "url" not in json.dumps(draft["image"])
    assert MemeRepository(tmp_path).get("meme:sad-shiba-dog") is None  # drafts never load


async def test_full_mock_robot_meme_interaction(make_orchestrator, library):
    from is_this_us.notifier import ConsoleNotifier

    robot = MockRobotAdapter(time_scale=0.0)
    orch = make_orchestrator(estimator=FakeEstimator("leaning_forward"), robot=robot, notifier=ConsoleNotifier())
    orch.repo, orch.matcher = library, SceneMatcher(library)
    orch.planner.approve(RobotBehavior.PEER)
    orch.storage.touch_visitor(1)
    out = await orch.observe_given(1, orch.interpreter.interpret(
        [__import__("is_this_us.vision.synthetic", fromlist=["x"]).synthetic_person("leaning_forward")]),
        origin="installation", archive=ArchiveSource.MEME)
    assert out.artwork.id == "meme:long-nose-borzoi"
    assert out.plan.behavior is RobotBehavior.PEER
    assert "Euler(0.0, 0.15, 0.0)" in robot.calls and robot.calls[-1] == "Euler(0.0, 0.0, 0.0)"
    assert out.result.status is ExecutionStatus.SIMULATED
