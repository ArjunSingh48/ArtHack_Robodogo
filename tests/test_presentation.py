"""The presentation programme: intro (@consent) → a pose round per era → rescue finale."""

from __future__ import annotations

import asyncio
import random
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from is_this_us.artworks.repository import YamlArtworkRepository
from is_this_us.memes.repository import MemeRepository
from is_this_us.performance.catalog import PerformanceCatalog
from is_this_us.performance.delivery import DeliveryResult, PhotoStore
from is_this_us.performance.director import Cmd, ShowState, StoryDirector
from is_this_us.performance.lines import Lines
from is_this_us.performance.pose_session import PoseReader
from is_this_us.performance.rounds import (behavior_for, candidates, human_pose, make_meme, meme_phrases,
                                           pick_for_round, side_for)
from is_this_us.performance.runtime import SyntheticParticipant
from is_this_us.performance.script import PRESENTATION_ORDER, ROUND_ERAS, ScriptError, load_script, parse_script
from is_this_us.performance.stage import RecordingStage
from is_this_us.performance.voice import ShowVoice, Speaker, all_utterances
from is_this_us.speech.synthesizer import NullSynthesizer

ROOT = Path(__file__).resolve().parents[1]
SHOW = ROOT / "data" / "performance"


@pytest.fixture(scope="module")
def pres():
    return load_script(SHOW, "en", "presentation")


@pytest.fixture(scope="module")
def catalog():
    return PerformanceCatalog(SHOW, YamlArtworkRepository(ROOT / "data" / "artworks"),
                              MemeRepository(ROOT / "data" / "memes"))


@pytest.fixture
def lines():
    return Lines.load(SHOW, "en", rng=random.Random(1))


class Delivery:
    name = "fake"

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, image, caption, session_id):
        self.sent.append((Path(image), caption))
        return DeliveryResult(self.ok, "sent" if self.ok else "boom")


class FakeMotion:
    enabled = True

    def __init__(self):
        self.done = []

    def perform(self, name):
        from is_this_us.domain import ExecutionStatus

        self.done.append(name)

        class R:
            status = ExecutionStatus.SIMULATED
            detail = ""
            diagnostic = ""
        return R()

    def request(self, name):
        self.done.append(name)

    def stop(self, reason):
        pass


def director(tmp_path, pres, catalog, lines, *, camera=True, delivery=None, **kw) -> StoryDirector:
    stand_in = SyntheticParticipant("sitting", head_tilt_deg=14)
    return StoryDirector(
        script=pres, catalog=catalog, lines=lines,
        voice=ShowVoice(NullSynthesizer(), NullSynthesizer(), Speaker(enabled=False), time_scale=0.0),
        stage=RecordingStage(), store=PhotoStore(tmp_path / "out", retention_minutes=60),
        pose_reader=PoseReader(stand_in) if camera else None,
        frame_source_factory=(lambda: stand_in) if camera else None,
        delivery=delivery if delivery is not None else Delivery(), mode="automatic", tick_s=0.005, hint_gap_s=0.0,
        rng=random.Random(0), moment_hold_s=0.0, stable_s=0.02, round_hold_s=0.03, rescue_wait_s=0.3,
        rescue_wake_s=0.05, flash_s=0.002, **{"photo_view_timeout_s": 0.02, **kw})


def answer(d, mapping, delay=0.01):
    def on_state(director, st):
        if st in mapping:
            sid = director.session_id
            asyncio.get_running_loop().call_later(delay, lambda: director.command(mapping[st], sid))
    d.listeners.append(on_state)


YES = {ShowState.CONSENT: Cmd.CONSENT_YES, ShowState.DELIVERY: Cmd.SEND_YES, ShowState.RESCUE: Cmd.READY}


# ---------------------------------------------------------------- script
def test_presentation_structure(pres):
    assert tuple(c.id for c in pres.chapters) == PRESENTATION_ORDER
    for era in ROUND_ERAS:
        rounds = [c for c in pres.chapter(era).cues if c.kind == "round"]
        assert len(rounds) == 1 and rounds[0].target == era and rounds[0].text
    assert [c.kind for c in pres.chapter("last_dog").cues].count("consent") == 1
    assert [c.kind for c in pres.chapter("rescue").cues].count("rescue") == 1
    assert pres.chapter("rescue").cues[-1].text == "Is this us?"


def test_presentation_validation_catches_a_missing_round(pres):
    text = (SHOW / "presentation.en.md").read_text().replace("@round: middle_ages\n", "")
    with pytest.raises(ScriptError, match="middle_ages"):
        parse_script(text, programme="presentation")
    with pytest.raises(ScriptError, match="presentation"):
        parse_script((SHOW / "script.en.md").read_text() + "\n@round: prehistory\nPose!\n")


def test_presentation_lines_are_pregenerated(pres, catalog, lines):
    texts = {u.text for u in all_utterances(pres, catalog, lines)}
    for era in ROUND_ERAS:
        assert next(c for c in pres.chapter(era).cues if c.kind == "round").text in texts
    assert lines.text("round", "found") in texts and lines.text("rescue", "wake") in texts
    spoken = {t for _, t in lines.fixed_lines()}
    assert not any(p in spoken for p in lines.variants("meme_phrases", "any"))  # written on the picture only
    assert not lines.validate()


# ---------------------------------------------------------------- picking
def test_every_era_has_pictures_and_a_robot_pose(catalog):
    from is_this_us.domain import RobotBehavior

    for era in ("prehistory", "middle_ages", "paintings"):
        cands = candidates(catalog, era)
        assert len(cands) >= 3, era
        for a in cands:
            assert behavior_for(a, catalog.art_repo) in {b.value for b in RobotBehavior}
            assert side_for(a, catalog.art_repo) in ("left", "right", "front")


def test_pick_prefers_the_same_human_pose(catalog):
    rng = random.Random(0)
    for posture in ("sitting", "standing"):
        pick = pick_for_round(catalog, "paintings", posture, rng=rng)
        assert human_pose(pick.artifact, catalog.art_repo) == posture
        assert posture in pick.why
    assert pick_for_round(catalog, "internet", "sitting") is None  # memes are never sent as archive images


# ---------------------------------------------------------------- the whole presentation
async def test_full_presentation_with_consent_and_send(tmp_path, pres, catalog, lines):
    delivery, motion = Delivery(), FakeMotion()
    d = director(tmp_path, pres, catalog, lines, delivery=delivery, motion=motion)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED and res.outcome == "together"
    assert [r["era"] for r in d.rounds] == ["prehistory", "middle_ages", "paintings", "internet", "rescue"]
    assert all(r["photo"] and r["settled"] for r in d.rounds)
    kinds = [r["sent"] for r in d.rounds]
    assert kinds[:3] == [["painting", "together"]] * 3 and kinds[3] == ["meme"] and kinds[4] == ["rescue"]
    assert len(delivery.sent) == 8
    # captions: short line + italic credit (HTML); the archive ones carry the real attribution
    for img, cap in delivery.sent:
        assert "\n\n<i>" in cap and cap.endswith("</i>")
    assert "Photo taken with consent" in delivery.sent[1][1]
    # the robot echoed each painted dog, then the rescue: a paw, then sitting
    assert motion.done[-2:] == ["greet", "sit"]
    # the remembered pictures flashed by before each reveal
    shows = d.stage.of("show")
    assert shows.count("composition") >= 5
    out = {p.name for p in (tmp_path / "out" / res.session_id).iterdir()}
    assert {"prehistory.jpg", "middle_ages.jpg", "paintings.jpg", "internet.jpg", "rescue.jpg"} <= out


async def test_no_consent_means_no_photo_at_all(tmp_path, pres, catalog, lines):
    delivery = Delivery()
    d = director(tmp_path, pres, catalog, lines, delivery=delivery)
    answer(d, {ShowState.CONSENT: Cmd.CONSENT_NO, ShowState.RESCUE: Cmd.READY})
    res = await d.run()
    assert res.final_state is ShowState.FINISHED and res.outcome == "alone"
    assert not any(r["photo"] for r in d.rounds)
    assert [p.parent.name for p, _ in delivery.sent] == ["images"] * len(delivery.sent)  # archive pictures only
    assert len(delivery.sent) == 3
    assert not (tmp_path / "out" / res.session_id).exists() or not any((tmp_path / "out" / res.session_id).iterdir())


async def test_consent_without_send_ok_keeps_photos_on_screen(tmp_path, pres, catalog, lines):
    delivery = Delivery()
    d = director(tmp_path, pres, catalog, lines, delivery=delivery)
    answer(d, {ShowState.CONSENT: Cmd.CONSENT_YES, ShowState.DELIVERY: Cmd.SEND_NO, ShowState.RESCUE: Cmd.READY})
    res = await d.run()
    assert all(r["photo"] for r in d.rounds)
    assert all(p.parent.name == "images" for p, _ in delivery.sent)  # visitor photos never left the computer
    assert any("big screen only" in n for n in d.encounter_log)
    assert res.outcome == "together"


async def test_without_camera_the_rounds_still_show_pictures(tmp_path, pres, catalog, lines):
    d = director(tmp_path, pres, catalog, lines, camera=False)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert [r["artifact"] is not None for r in d.rounds[:3]] == [True, True, True]
    assert not any(r["photo"] for r in d.rounds)


async def test_rounds_only_skips_narration(tmp_path, pres, catalog, lines):
    d = director(tmp_path, pres, catalog, lines)
    answer(d, YES)
    await d.run(start_at="invitation")
    subs = d.stage.of("subtitle")
    assert "I have four legs. I can walk.\nSometimes I fall over." not in subs
    assert len(d.rounds) == 5


async def test_cancel_during_a_round_stops_everything(tmp_path, pres, catalog, lines):
    motion = FakeMotion()
    d = director(tmp_path, pres, catalog, lines, motion=motion)
    answer(d, {**YES, ShowState.FINAL_POSE: Cmd.CANCEL})
    res = await d.run()
    assert res.final_state is ShowState.CANCELLED
    assert not (tmp_path / "out" / res.session_id).exists()


async def test_walking_round_goes_there_and_back(tmp_path, pres, catalog, lines):
    from is_this_us.config import Settings
    from is_this_us.performance.runtime import simulated_approacher

    approacher = simulated_approacher(Settings(_env_file=None), fast=0.0)
    d = director(tmp_path, pres, catalog, lines, motion=FakeMotion(), approacher=approacher)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert sum(w.startswith("approach: arrived") for w in d.walk_log) >= 4
    assert sum(w == "home: done" for w in d.walk_log) == 4  # back after every round; stays with the rescued person


def test_approach_keeps_distance_from_someone_lying(tmp_path):
    from is_this_us.config import Settings
    from is_this_us.performance.runtime import simulated_approacher

    a = simulated_approacher(Settings(_env_file=None), fast=0.0, person=(2.6, 0.2))
    a.world.posture = "lying"
    res = a.approach(side="front", posture="lying")
    assert res.arrived
    assert a.world.min_distance >= a.s.lying_gap_m - 0.25


# ---------------------------------------------------------------- pictures the robot makes
def test_meme_is_drawn_on_the_photo(lines):
    buf = BytesIO()
    Image.new("RGB", (640, 480), (90, 90, 120)).save(buf, format="JPEG")
    phrases = meme_phrases(lines, "sitting", random.Random(2))
    assert phrases[-1] == "wow" and len(phrases) == 5
    img = Image.open(BytesIO(make_meme(buf.getvalue(), phrases, seed=3)))
    assert img.width == 1600 and img.height == 1200
    assert len(img.convert("RGB").getcolors(maxcolors=1 << 20)) > 10  # text was drawn


async def test_go_to_interactive_in_the_renaissance_chapter(tmp_path, pres, catalog, lines):
    """Regression: pressing "Go to interactive" (pose) during the paintings narration raised StopIteration
    (the presentation has no @interactive chapter). It must skip to the paintings round and finish."""
    d = director(tmp_path, pres, catalog, lines)
    answer(d, YES)
    pressed = []

    def on_state(director_, st):
        if director_.chapter_id == "paintings" and st is ShowState.NARRATION and not pressed:
            pressed.append(True)
            sid = director_.session_id
            asyncio.get_running_loop().call_soon(lambda: director_.command(Cmd.POSE, sid))
    d.listeners.append(on_state)
    res = await d.run()
    assert pressed and res.final_state is ShowState.FINISHED
    assert [r["era"] for r in d.rounds] == ["prehistory", "middle_ages", "paintings", "internet", "rescue"]
    subs = d.stage.of("subtitle")
    assert "Not famous dogs. Just dogs who were there." not in subs  # the rest of that narration was skipped
    assert "Humans and dogs, running together.\nHunting." in subs  # other chapters keep theirs


# ---------------------------------------------------------------- no Telegram, camera failures, live view
async def test_without_telegram_photos_stay_local_and_nobody_is_told_otherwise(tmp_path, pres, catalog, lines):
    d = director(tmp_path, pres, catalog, lines)
    d.delivery = None  # nothing configured
    states = []
    d.listeners.append(lambda _d, st: states.append(st))
    answer(d, {ShowState.CONSENT: Cmd.CONSENT_YES, ShowState.RESCUE: Cmd.READY})
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert ShowState.DELIVERY not in states and ShowState.SENDING not in states  # no "send to Telegram?" step
    subs = " ".join(d.stage.of("subtitle"))
    assert "here, on the big screen." in subs and "Telegram" not in subs
    assert all(r["photo"] for r in d.rounds)  # consented local photos and THEN/NOW still happen
    assert any("local display only" in n for n in d.encounter_log)
    assert not any("error" in n.lower() or "fail" in n.lower() for n in d.encounter_log)


def _stand_in():
    return SyntheticParticipant("sitting", head_tilt_deg=14)


async def test_denied_laptop_camera_falls_back_to_the_robot_camera_without_joint_photos(tmp_path, pres, catalog,
                                                                                       lines):
    from is_this_us.performance.runtime import CameraOpener

    calls = []

    def make(spec):
        calls.append(spec)
        if spec.startswith("webcam"):
            raise OSError("OpenCV: not authorized to capture video. Cannot open webcam 0")
        return _stand_in()

    d = director(tmp_path, pres, catalog, lines)
    d.frame_source_factory = CameraOpener("webcam:0", "zhaw_bridge", make)
    states = []
    d.listeners.append(lambda _d, st: states.append(st))
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert calls == ["webcam:0", "zhaw_bridge"]  # tried once each, never retried
    alerts = [a for a in d.stage.of("alert") if a]
    assert alerts and "ROBOT camera" in alerts[0] and "Privacy & Security" in alerts[0]
    assert ShowState.CONSENT not in states  # no photo can be taken, so none is asked for
    assert not any(r["photo"] for r in d.rounds)  # never claims a joint photo
    assert [r["posture"] for r in d.rounds[:3]] == ["sitting"] * 3  # poses are still read (robot camera)


async def test_denied_camera_without_fallback_is_reported_once(tmp_path, pres, catalog, lines):
    from is_this_us.performance.runtime import CameraOpener

    calls = []

    def make(spec):
        calls.append(spec)
        raise OSError("OpenCV: not authorized to capture video")

    d = director(tmp_path, pres, catalog, lines)
    d.frame_source_factory = CameraOpener("webcam:0", "", make)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED and calls == ["webcam:0"]
    assert any(a and "CAMERA FAILED" in a for a in d.stage.of("alert"))
    assert not any(r["photo"] for r in d.rounds)


async def test_live_view_stays_up_through_the_whole_presentation(tmp_path, pres, catalog, lines):
    d = director(tmp_path, pres, catalog, lines, live_always=True)
    d.voice = ShowVoice(NullSynthesizer(), NullSynthesizer(), Speaker(enabled=False), time_scale=0.003)
    answer(d, YES)
    await d.run()
    ev = d.stage.events
    marks = [i for i, (k, _) in enumerate(ev) if k == "chapter"] + [len(ev)]
    for a, b in zip(marks, marks[1:]):
        seg = ev[a:b]
        assert any(k == "live" and v for k, v in seg), seg[0]
        # never hidden in the middle of the show: only at the very end
        assert not any(k == "live" and v is False for k, v in seg) or b == len(ev)
    remembering = [i for i, (k, v) in enumerate(ev) if k == "status" and v == "remembering"]
    assert remembering and any(k == "live" and v for k, v in ev[remembering[0]:remembering[0] + 200])


# ---------------------------------------------------------------- two cameras: the dog's own + the laptop's
async def test_dog_camera_reads_the_pose_and_laptop_camera_takes_the_photo(tmp_path, pres, catalog, lines):
    from is_this_us.vision.frame_sources import Frame

    robot_reads, laptop_reads = [], []

    class Labelled:
        def __init__(self, label, log):
            self.inner, self.label, self.log = _stand_in(), label, log

        def read(self):
            self.log.append(1)
            f = self.inner.read()
            return Frame(f.image, self.label)

        def close(self):
            pass

    d = director(tmp_path, pres, catalog, lines, live_always=True, pose_from="robot",
                 robot_frame_source_factory=lambda: Labelled("zhaw_bridge:test", robot_reads))
    d.frame_source_factory = lambda: Labelled("webcam:0", laptop_reads)
    d.voice = ShowVoice(NullSynthesizer(), NullSynthesizer(), Speaker(enabled=False), time_scale=0.002)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    kinds = {k for k, v in d.stage.events if v is True}
    assert {"live", "live_robot"} <= kinds  # both feeds on the projector
    assert robot_reads and laptop_reads
    assert [r["posture"] for r in d.rounds[:3]] == ["sitting"] * 3 and all(r["photo"] for r in d.rounds)
    assert any(p.name.startswith("now-") for p in (tmp_path / "out" / res.session_id).iterdir())


async def test_missing_dog_camera_falls_back_to_laptop_for_poses(tmp_path, pres, catalog, lines):
    def broken():
        raise OSError("no camera frames from the bridge")

    d = director(tmp_path, pres, catalog, lines, pose_from="robot", robot_frame_source_factory=broken)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert any(a and "ROBOT CAMERA" in a for a in d.stage.of("alert"))
    assert [r["posture"] for r in d.rounds[:3]] == ["sitting"] * 3 and all(r["photo"] for r in d.rounds)


# ---------------------------------------------------------------- photo stays; calm remembering; simulated dog
async def test_photo_stays_until_the_operator_continues(tmp_path, pres, catalog, lines):
    d = director(tmp_path, pres, catalog, lines, photo_view_timeout_s=None)
    shown_at = []

    def on_state(director_, st):
        if st is ShowState.ARTWORK:
            shown_at.append(time.monotonic())
            sid = director_.session_id
            asyncio.get_running_loop().call_later(0.15, lambda: director_.command(Cmd.NEXT, sid))
    d.listeners.append(on_state)
    answer(d, YES)
    t0 = time.monotonic()
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert len(shown_at) == 5  # every photo waited for "Next": 4 rounds + the rescue
    subs = d.stage.of("subtitle")
    assert sum(any(a in s_ for a in lines.variants("round", "admire")) for s_ in subs) == 5
    assert time.monotonic() - t0 >= 5 * 0.15


async def test_remembering_shows_a_calm_grid_with_moving_focus(tmp_path, pres, catalog, lines):
    d = director(tmp_path, pres, catalog, lines)
    answer(d, YES)
    await d.run()
    shows = d.stage.of("show")
    assert "consider" in shows  # one grid per round, not a picture per flash
    focus = d.stage.of("focus")
    assert focus and all(catalog.get(f) is not None for f in focus)


async def test_simulated_dog_walks_and_poses_without_a_robot(tmp_path, pres, catalog, lines):
    from is_this_us.performance.sim_robot import SimRobot
    from is_this_us.performance.approach import ApproachSettings

    sim = SimRobot(ApproachSettings(speed=0.3, max_seconds=30), person=(1.2, 0.3), pose_time_scale=0.01)
    snaps = []
    d = director(tmp_path, pres, catalog, lines, motion=sim.motion, approacher=sim.approacher, sim_robot=sim)
    d.stage.sim = lambda snap: snaps.append(snap)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert all(r["settled"] for r in d.rounds)  # the simulated dog "did" every pose
    assert any(s["walking"] for s in snaps) and any(s["body"] == "sitting" for s in snaps)
    assert sum(w.startswith("approach: arrived") for w in d.walk_log) >= 4
    assert not sim.robot.is_real and "Hello" in sim.robot.calls


def test_simulated_person_follows_the_camera_like_a_mirror(monkeypatch):
    import numpy as np

    from is_this_us.performance import sim_robot as sr
    from is_this_us.vision.pose import PersonLandmarks

    sim = sr.SimRobot()
    assert sim.snapshot()["skeleton"] is None  # nobody seen: the view uses the posture preset
    world = np.zeros((33, 3), dtype=np.float32)
    world[15] = (0.3, -0.4, -0.1)  # left wrist: image right, raised, toward the camera
    seen = PersonLandmarks(xy=np.zeros((33, 2)), visibility=np.ones(33), image_size=(640, 480), world=world)
    sim.see_person(seen, mirror=False)
    assert sim.skeleton()[15][:3] == [0.3, 0.4, 0.1]  # y up, z toward the viewer
    sim.see_person(seen, mirror=True)
    assert sim.skeleton()[16][:3] == [-0.3, 0.4, 0.1]  # mirrored: shown as the avatar's right hand
    now = sr.time.monotonic()
    monkeypatch.setattr(sr.time, "monotonic", lambda: now + sr.SKELETON_MAX_AGE_S + 1)
    assert sim.skeleton() is None  # stale readings fall back to the preset


async def test_lobby_waits_for_start_and_opens_the_camera_only_after_consent(tmp_path, pres, catalog, lines):
    opened = []
    d = director(tmp_path, pres, catalog, lines, wait_for_start=True)
    inner = d.frame_source_factory
    d.frame_source_factory = lambda: opened.append(d.state) or inner()
    answer(d, {ShowState.LOBBY: Cmd.CONSENT_YES, **YES})
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert d.transitions[0] == ("idle", "lobby")  # nothing before the Start
    assert opened and ShowState.LOBBY not in opened and d.photo_consent  # the camera opened only after Start
    assert "consent" not in [b for _, b in d.transitions]  # nobody is asked again later


async def test_lobby_can_start_with_a_picture_instead_of_the_camera(tmp_path, pres, catalog, lines):
    from is_this_us.performance.runtime import SyntheticParticipant

    pic = SyntheticParticipant("standing")
    d = director(tmp_path, pres, catalog, lines, camera=False, wait_for_start=True, photo_factory=lambda data: pic)
    answer(d, {ShowState.LOBBY: Cmd.SAMPLE, **YES})
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert d.photo_consent and d._open_camera() is pic and d.pose_reader is not None


async def test_operator_can_switch_from_a_sample_picture_to_the_camera(tmp_path, pres, catalog, lines):
    from is_this_us.performance.runtime import SyntheticParticipant

    pic, cam = SyntheticParticipant("standing"), SyntheticParticipant("sitting")
    d = director(tmp_path, pres, catalog, lines, wait_for_start=True, photo_factory=lambda data: pic)
    d.frame_source_factory = d._real_camera = None  # set below: the "real" camera
    d.frame_source_factory, d._real_camera = (lambda: cam), ((lambda: cam), d.pose_reader)
    switched = []

    def on_state(director, st):
        if st is ShowState.LOBBY:
            asyncio.get_running_loop().call_later(0.01, lambda: director.command(Cmd.SAMPLE))
        elif st is ShowState.INTRODUCTION and not switched:
            switched.append(director.command(Cmd.CAMERA))
    d.listeners.append(on_state)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert switched == [(True, "ok: camera")] and d._open_camera() is cam and d.photo_consent


async def test_operator_can_replay_a_step_by_its_hash(tmp_path, pres, catalog, lines):
    d = director(tmp_path, pres, catalog, lines)
    jumped = []
    step = next(c.id for c in pres.chapter("prehistory").cues if c.kind == "round")

    def on_state(director, st):
        if st is ShowState.READINESS and director.chapter_id == "middle_ages" and not jumped:
            jumped.append(director.goto("prehistory"))
        elif st is ShowState.READINESS and director.chapter_id == "paintings" and len(jumped) == 1:
            jumped.append(director.goto(step))  # one picture step: straight back to that round
    d.listeners.append(on_state)
    answer(d, YES)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert jumped == [(True, "ok: going to prehistory"), (True, f"ok: going to {step}")]
    played = d.chapters_played
    assert played.index("middle_ages") < len(played) - 1 - played[::-1].index("prehistory")  # replayed after
    assert d.goto("nowhere")[0] is False


async def test_operator_can_switch_from_the_camera_to_the_sample_photos(tmp_path, pres, catalog, lines):
    from is_this_us.performance.runtime import SyntheticParticipant

    pic = SyntheticParticipant("standing")
    pic.is_still = True
    d = director(tmp_path, pres, catalog, lines, photo_factory=lambda data: pic)
    switched = []

    def on_state(director, st):
        if st is ShowState.INTRODUCTION and not switched:
            switched.append(director.command(Cmd.SAMPLES))
    d.listeners.append(on_state)
    answer(d, {**YES, ShowState.READINESS: Cmd.READY})
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert switched == [(True, "ok: sample photos")] and d._open_camera() is pic
