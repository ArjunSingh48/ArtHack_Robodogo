"""Performance mode: script, catalog, director state machine, pose, composition, delivery, voice."""

from __future__ import annotations

import asyncio
import math
import random
import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from is_this_us.artworks.repository import YamlArtworkRepository
from is_this_us.memes.repository import MemeRepository
from is_this_us.performance.catalog import PerformanceCatalog
from is_this_us.performance.delivery import DeliveryResult, PhotoStore, PreviewDelivery
from is_this_us.performance.director import Cmd, ShowState, StoryDirector
from is_this_us.performance.lines import Lines
from is_this_us.performance.pose_session import PoseCoach, PoseReader, PoseReading, compare
from is_this_us.performance.runtime import ParticipantScript, SyntheticParticipant, keyboard_command
from is_this_us.performance.script import CHAPTER_ORDER, ScriptError, load_script, parse_script
from is_this_us.performance.stage import RecordingStage
from is_this_us.performance.voice import ShowVoice, Speaker, all_utterances, cache_status, prune
from is_this_us.speech.synthesizer import AudioCache, MockSynthesizer, NullSynthesizer

ROOT = Path(__file__).resolve().parents[1]
SHOW = ROOT / "data" / "performance"


@pytest.fixture(scope="module")
def script():
    return load_script(SHOW, "en")


@pytest.fixture(scope="module")
def catalog():
    return PerformanceCatalog(SHOW, YamlArtworkRepository(ROOT / "data" / "artworks"),
                              MemeRepository(ROOT / "data" / "memes"))


@pytest.fixture
def lines():
    return Lines.load(SHOW, "en", rng=random.Random(1))


def fast_voice(narration=None, short=None) -> ShowVoice:
    return ShowVoice(narration or NullSynthesizer(), short or NullSynthesizer(), Speaker(enabled=False),
                     time_scale=0.0)


class CountingDelivery:
    name = "fake"

    def __init__(self, ok=True):
        self.ok = ok
        self.sent: list[tuple[Path, str, str]] = []

    async def send(self, image, caption, session_id):
        self.sent.append((image, caption, session_id))
        return DeliveryResult(self.ok, "sent" if self.ok else "boom")


class BrokenCamera:
    name = "broken"

    def read(self):
        raise OSError("camera unplugged")

    def close(self):
        pass


def make_director(tmp_path, script, catalog, lines, *, mode="automatic", posture="sitting", camera="synthetic",
                  delivery=None, voice=None, **kw) -> StoryDirector:
    stand_in = SyntheticParticipant(posture, head_tilt_deg=14)
    factory, reader = None, None
    if camera == "synthetic":
        factory, reader = (lambda: stand_in), PoseReader(stand_in)
    elif camera == "broken":
        factory, reader = (lambda: BrokenCamera()), PoseReader(stand_in)
    d = StoryDirector(script=script, catalog=catalog, lines=lines, voice=voice or fast_voice(), stage=RecordingStage(),
                      store=PhotoStore(tmp_path / "out", retention_minutes=60), pose_reader=reader,
                      frame_source_factory=factory, delivery=delivery if delivery is not None else CountingDelivery(),
                      mode=mode, tick_s=0.005, hint_gap_s=0.0, rng=random.Random(0),
                      **{"try_seconds": 0.05, "moment_hold_s": 0.0, "live_linger_s": 0.0, "finale": "imitate", **kw})
    return d


def answer(d: StoryDirector, mapping: dict[ShowState, Cmd], delay: float = 0.01) -> None:
    def on_state(director, st):
        if st in mapping:
            sid = director.session_id
            asyncio.get_running_loop().call_later(delay, lambda: director.command(mapping[st], sid))
    d.listeners.append(on_state)


HAPPY = {ShowState.READINESS: Cmd.READY, ShowState.CONSENT: Cmd.CONSENT_YES, ShowState.DELIVERY: Cmd.SEND_YES}


# ---------------------------------------------------------------- script
def test_script_has_all_chapters_in_order(script):
    assert tuple(c.id for c in script.chapters) == CHAPTER_ORDER
    assert [c.id for c in script.chapters if c.interactive] == ["invitation"]
    first = script.speech_cues()[0]
    assert first.text == "Hello."
    assert script.chapter("present").cues_for("together")[-1].text == "Is this us?"
    assert any("We made it." in c.text for c in script.chapter("present").cues_for("together"))
    assert not any("We made it." in c.text for c in script.chapter("present").cues_for("alone"))


def test_narration_text_is_not_in_python():
    src = "\n".join(p.read_text() for p in (ROOT / "src" / "is_this_us" / "performance").glob("*.py"))
    for line in ("Perhaps ancient dogs had better batteries.", "There are SO MANY DOGS.", "I would like to be your dog."):
        assert line not in src


def test_script_rejects_wrong_order_and_unknown_directive(script):
    text = (SHOW / "script.en.md").read_text()
    swapped = text.replace("## prehistory | Prehistory", "## tmp | X").replace("## middle_ages |", "## prehistory |")
    with pytest.raises(ScriptError):
        parse_script(swapped)
    with pytest.raises(ScriptError):
        parse_script(re.sub(r"(?m)^@hold$", "@jump", text, count=1))
    with pytest.raises(ScriptError):
        parse_script(text.replace("{pose_request}", "Please sit."))


def test_pause_hold_and_show_directives_parsed(script):
    pre = script.chapter("prehistory")
    assert any(c.kind == "pause" and c.seconds == 2 for c in pre.cues)
    assert pre.cues[-1].hold_after
    assert pre.cues[0].show == "met:327508"


# ---------------------------------------------------------------- catalog
def test_catalog_validates_against_script(script, catalog, lines):
    assert catalog.validate(script, pose_lines=lines.keys("pose")) == []
    assert lines.validate() == []
    for ch in ("prehistory", "middle_ages", "paintings", "photography", "internet"):
        assert catalog.for_chapter(ch), ch
    for a in catalog.items.values():
        if a.kind == "art":
            assert a.rights_status in ("Public Domain", "CC0") and a.displayable


def test_metadata_comes_from_verified_records_not_hand_typing(catalog):
    a = catalog.get("met:545395")
    assert a.title == "Inscribed block with bowman and dog" and a.date == "ca. 2051–2000 B.C."
    assert catalog.get("cma:290412").artist.startswith("Raja Deen Dayal")
    assert catalog.get("meme:doge").rights_status.startswith("reference only")
    assert not catalog.get("meme:doge").displayable


def test_catalog_flags_bad_entries(tmp_path, catalog):
    import shutil

    import yaml

    d = tmp_path / "perf"
    shutil.copytree(SHOW, d)
    raw = yaml.safe_load((d / "catalog.yaml").read_text())
    raw["artifacts"]["met:327508"]["interactive"] = {"pose_subject": "human", "safe_to_imitate": True,
                                                    "request": "x", "posture": "sitting"}
    raw["artifacts"]["met:999999"] = {"fetch": True, "chapter": "prehistory", "subject": "dog_only",
                                      "description": "x"}
    (d / "catalog.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    bad = PerformanceCatalog(d, catalog.art_repo, catalog.meme_repo).validate()
    assert any("dog-only picture cannot have a human target pose" in p for p in bad)
    assert any("met:999999" in p and "itu show catalog build" in p for p in bad)


def test_interactive_choice_prefers_human_and_posture(catalog):
    a = catalog.choose_interactive("sitting", rng=random.Random(0))
    assert a.interactive.posture == "sitting" and a.interactive.pose_subject == "human"
    b = catalog.choose_interactive("standing", rng=random.Random(0))
    assert b.interactive.posture == "standing"
    c = catalog.choose_interactive("sitting", exclude={a.id}, rng=random.Random(0))
    assert c.id != a.id
    dog = catalog.get("met:285425")
    assert dog.subject == "dog_only" and dog.interactive.semantic_only


# ---------------------------------------------------------------- director: full runs
async def test_full_rehearsal_together(tmp_path, script, catalog, lines):
    delivery = CountingDelivery()
    d = make_director(tmp_path, script, catalog, lines, delivery=delivery)
    answer(d, HAPPY)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert res.chapters == list(CHAPTER_ORDER)
    assert res.outcome == "together" and res.composition and res.composition.exists()
    assert len(delivery.sent) == 1 and "— Your robot dog 🐾" in delivery.sent[0][1]
    subs = d.stage.of("subtitle")
    assert subs[0] == "Hello." and subs[-1] == "Is this us?"
    assert "We made it." in subs
    states = [b for _, b in res.transitions]
    for s in ("introduction", "narration", "invitation", "participant_readiness", "consent", "capture",
              "composition", "delivery", "conclusion", "finished"):
        assert s in states
    assert states.index("consent") < states.index("capture") < states.index("delivery")


async def test_artworks_shown_in_sync_with_cues(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    answer(d, HAPPY)
    seen = []
    d.stage.subtitle = lambda t, _s=d.stage: seen.append(("sub", t)) if t else None
    orig = d.stage.show
    d.stage.show = lambda v: (seen.append(("show", v.artifact.id if v.artifact else v.kind)), orig(v))
    await d.run()
    i = seen.index(("show", "met:437890"))
    assert seen[i + 1] == ("sub", "Some are enormous.")
    j = seen.index(("show", "cma:169746"))
    assert seen[j + 1] == ("sub", "Then I found these.\nMedieval manuscripts.")


async def test_participant_declines_story_still_finishes(tmp_path, script, catalog, lines):
    delivery = CountingDelivery()
    d = make_director(tmp_path, script, catalog, lines, delivery=delivery)
    answer(d, {ShowState.READINESS: Cmd.DECLINE})
    res = await d.run()
    assert res.final_state is ShowState.FINISHED and res.outcome == "alone"
    assert not delivery.sent and res.composition is None
    subs = d.stage.of("subtitle")
    assert "We did not make a picture today." in subs and "We made it." not in subs
    assert subs[-1] == "Is this us?"
    assert not any((tmp_path / "out").glob("*/now.jpg"))


async def test_no_consent_means_no_photo(tmp_path, script, catalog, lines):
    delivery = CountingDelivery()
    d = make_director(tmp_path, script, catalog, lines, delivery=delivery)
    answer(d, {ShowState.READINESS: Cmd.READY, ShowState.CONSENT: Cmd.CONSENT_NO})
    res = await d.run()
    assert res.outcome == "alone" and not delivery.sent
    assert "capture" not in [b for _, b in res.transitions]
    assert not any((tmp_path / "out").rglob("*.jpg"))


async def test_consent_timeout_is_a_no(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, consent_timeout_s=0.05)
    answer(d, {ShowState.READINESS: Cmd.READY})
    res = await d.run()
    assert res.outcome == "alone" and res.composition is None


async def test_send_needs_separate_confirmation(tmp_path, script, catalog, lines):
    delivery = CountingDelivery()
    d = make_director(tmp_path, script, catalog, lines, delivery=delivery, send_timeout_s=0.05)
    answer(d, {ShowState.READINESS: Cmd.READY, ShowState.CONSENT: Cmd.CONSENT_YES})
    res = await d.run()
    assert res.outcome == "together" and res.composition.exists()
    assert delivery.sent == [] and res.delivery_detail.startswith("not sent")


async def test_delivery_failure_does_not_stop_the_ending(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, delivery=CountingDelivery(ok=False))
    answer(d, HAPPY)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED and not res.delivered and res.delivery_detail == "boom"
    assert d.stage.of("subtitle")[-1] == "Is this us?"


async def test_readiness_never_captures_without_confirmation(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, readiness_timeout_s=0.1)
    res = await d.run()  # nobody says "ready": no forced capture, the story ends alone
    assert res.outcome == "alone"
    assert "consent" not in [b for _, b in res.transitions]


# ---------------------------------------------------------------- director: controls
async def test_pause_resume_replays_the_cue(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    answer(d, HAPPY)
    done = {"paused": False}

    def on_state(director, st):
        if st is ShowState.NARRATION and not done["paused"]:
            done["paused"] = True
            loop = asyncio.get_running_loop()
            loop.call_soon(lambda: director.command(Cmd.PAUSE))
            loop.call_later(0.05, lambda: director.command(Cmd.RESUME))
    d.listeners.append(on_state)
    res = await d.run()
    states = [b for _, b in res.transitions]
    assert "paused" in states and res.final_state is ShowState.FINISHED
    subs = d.stage.of("subtitle")
    assert subs.count("I started at the beginning.\nA very, very long time ago.") == 2  # replayed after resume


async def test_skip_jumps_to_next_chapter(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    answer(d, HAPPY)
    orig = d.stage.chapter

    def chapter(cid, title):
        orig(cid, title)
        if cid == "paintings":
            asyncio.get_running_loop().call_soon(lambda: d.command(Cmd.SKIP))
    d.stage.chapter = chapter
    res = await d.run()
    subs = d.stage.of("subtitle")
    assert "Some are enormous." not in subs
    assert "And then humans invented cameras." in subs
    assert res.final_state is ShowState.FINISHED


async def test_cancel_purges_photos_and_stops(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)

    def on_state(director, st):
        if st is ShowState.READINESS:
            asyncio.get_running_loop().call_soon(lambda: director.command(Cmd.READY))
        if st is ShowState.CONSENT:
            asyncio.get_running_loop().call_soon(lambda: director.command(Cmd.CONSENT_YES))
        if st is ShowState.DELIVERY:
            asyncio.get_running_loop().call_soon(lambda: director.command(Cmd.CANCEL))
    d.listeners.append(on_state)
    res = await d.run()
    assert res.final_state is ShowState.CANCELLED and res.composition is None
    assert not (tmp_path / "out" / res.session_id).exists()
    assert "present" not in res.chapters


async def test_guided_mode_waits_for_operator(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, mode="guided")
    answer(d, HAPPY)

    def on_state(director, st):
        if st is ShowState.ARTWORK:
            asyncio.get_running_loop().call_later(0.01, lambda: director.command(Cmd.NEXT))
    d.listeners.append(on_state)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    holds = [b for _, b in res.transitions].count("artwork_display")
    assert holds == 6  # one per chapter before the invitation


async def test_repeat_at_hold_replays_last_segment(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, mode="guided")
    answer(d, HAPPY)
    count = {"n": 0}

    def on_state(director, st):
        if st is ShowState.ARTWORK:
            count["n"] += 1
            cmd = Cmd.REPEAT if count["n"] == 1 else Cmd.NEXT
            asyncio.get_running_loop().call_later(0.01, lambda: director.command(cmd))
    d.listeners.append(on_state)
    await d.run()
    # the first hold follows the "Is someone there?" moment: repeat asks again
    assert d.stage.of("subtitle").count("Oh.\nIs someone there?") == 2


async def test_pose_command_jumps_to_interactive(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    answer(d, HAPPY)
    jumped = {"done": False}

    def on_state(director, st):
        if st is ShowState.INTRODUCTION and not jumped["done"]:
            jumped["done"] = True
            asyncio.get_running_loop().call_soon(lambda: director.command(Cmd.POSE))
    d.listeners.append(on_state)
    res = await d.run()
    assert res.chapters[:2] == ["last_dog", "invitation"] and res.outcome == "together"


async def test_stale_and_invalid_commands_are_refused(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    answer(d, HAPPY)
    res = await d.run()
    old = res.session_id
    assert d.command(Cmd.SEND_YES, old) == (False, "'send_yes' does nothing while finished")
    d2 = make_director(tmp_path, script, catalog, lines)
    d2.session_id, d2.state = "abc123", ShowState.DELIVERY
    ok, msg = d2.command(Cmd.SEND_YES, "zzz999")
    assert not ok and "earlier performance" in msg
    d2.state = ShowState.NARRATION
    assert d2.command(Cmd.SEND_YES)[0] is False
    assert d2.command("dance")[0] is False


async def test_stale_command_cleared_on_state_change(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    d.state = ShowState.READINESS
    d._inbox.append(Cmd.READY)
    d._set_state(ShowState.CONSENT)
    assert not d._inbox


# ---------------------------------------------------------------- recovery
async def test_voice_unavailable_is_text_only(tmp_path, script, catalog, lines):
    synth = MockSynthesizer(AudioCache(tmp_path / "a"), fail=True)
    d = make_director(tmp_path, script, catalog, lines, voice=fast_voice(synth, synth))
    answer(d, HAPPY)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED
    assert all(s.audio is None for s in d.voice.log)


async def test_slow_voice_never_blocks(tmp_path, script, catalog, lines):
    class Slow:
        available = True

        def cached(self, *a):
            return None

        def synthesize(self, *a):
            import time

            time.sleep(0.3)
            return None

    v = fast_voice(Slow(), Slow())
    v.generation_timeout_s = 0.01
    spoken = await v.speak("Hello.")
    assert spoken.audio is None


async def test_camera_failure_falls_back(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, camera="broken")
    answer(d, HAPPY)
    res = await d.run()
    assert res.final_state is ShowState.FINISHED and res.outcome == "alone"
    assert d.lines.text("consent", "failed") in d.stage.of("subtitle")


async def test_camera_failure_telegram_photo_with_consent(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, camera="broken", photo_upload_allowed=True)
    answer(d, HAPPY)
    buf = BytesIO()
    Image.new("RGB", (300, 400), (180, 170, 160)).save(buf, format="JPEG")

    def on_state(director, st):
        if st is ShowState.AWAITING_PHOTO:
            sid = director.session_id
            asyncio.get_running_loop().call_later(0.01, lambda: director.supply_photo(buf.getvalue(), sid))
    d.listeners.append(on_state)
    assert d.supply_photo(b"x")[0] is False  # not waiting for a photo
    res = await d.run()
    assert res.outcome == "together" and res.composition.exists()


async def test_no_camera_manual_composition(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, camera=None)
    answer(d, HAPPY)
    res = await d.run()
    # no camera and no upload: the story ends without a photo, and says why
    assert res.outcome == "alone"
    assert d.lines.text("pose", "manual") in d.stage.of("subtitle")


# ---------------------------------------------------------------- pose + comparison
def test_coach_hints_in_order_and_quietly(catalog):
    spec = catalog.get("met:437841").interactive  # sitting, head tilted
    t = {"now": 0.0}
    coach = PoseCoach(spec, min_gap_s=5, clock=lambda: t["now"])
    assert coach.next_hint(PoseReading(False)) == "not_visible"
    assert coach.next_hint(PoseReading(False)) is None  # rate-limited
    t["now"] += 6
    assert coach.next_hint(PoseReading(True, "ok", "move_left", "sitting", True)) == "move_left"
    t["now"] += 6
    assert coach.next_hint(PoseReading(True, "ok", "center", "standing", False)) == "sit"
    t["now"] += 6
    assert coach.next_hint(PoseReading(True, "ok", "center", "sitting", False)) == "tilt_head"
    t["now"] += 6
    assert coach.next_hint(PoseReading(True, "ok", "center", "sitting", True)) == "got_it"
    t["now"] += 6
    assert coach.next_hint(PoseReading(True, "ok", "center", "sitting", True)) is None  # no chatter


def test_reader_framing_side_and_tilt():
    from is_this_us.vision.synthetic import synthetic_person

    class Est:
        def __init__(self, p):
            self.p = p

        def estimate(self, rgb):
            return [self.p]

    r = PoseReader(Est(synthetic_person("sitting", head_tilt_deg=15))).read(None)
    assert r.person_visible and r.framing == "ok" and r.posture == "sitting" and r.head_tilt
    r = PoseReader(Est(synthetic_person("standing", shift_x=-220))).read(None)
    assert r.side == "move_left" and r.head_tilt is False
    r = PoseReader(Est(synthetic_person("standing", shift_x=-220)), mirrored=True).read(None)
    assert r.side == "move_right"
    class Empty:
        def estimate(self, rgb):
            return []
    assert PoseReader(Empty()).read(None).person_visible is False


def test_comparison_is_qualitative(catalog):
    human = catalog.get("met:437841")
    good = compare(human, PoseReading(True, "ok", "center", "sitting", True))
    assert good.shared == ["seated", "heads tilted"] and good.line_keys == ["shared", "differ", "okay"]
    assert not re.search(r"\d", good.label() + good.note)
    other = compare(human, PoseReading(True, "ok", "center", "standing", False))
    assert other.line_keys == ["understand"] and other.different
    dog = compare(catalog.get("met:285425"), PoseReading(True, "ok", "center", "sitting", None))
    assert dog.method == "meaning" and "never measured" in dog.note
    none = compare(human, None)
    assert none.method == "none" and "no comparison" in none.note


# ---------------------------------------------------------------- composition
def test_composition_is_deterministic_and_keeps_proportions(catalog):
    from datetime import datetime

    from is_this_us.performance.composer import SIZE, compose

    art = catalog.get("cma:290412")
    then = Image.open(art.image_path)
    now = Image.new("RGB", (480, 640), (200, 190, 180))
    comp = compare(art, PoseReading(True, "ok", "center", "sitting", None))
    a = compose(then, now, art, comp, when=datetime(2026, 10, 24), session_id="abc123")
    b = compose(then, now, art, comp, when=datetime(2026, 10, 24), session_id="abc123")
    assert a == b
    img = Image.open(BytesIO(a))
    assert img.size == SIZE and img.format == "JPEG"
    dark = compose(then, now, art, comp, when=datetime(2026, 10, 24), theme="charcoal")
    assert Image.open(BytesIO(dark)).getpixel((5, 5))[0] < 60


# ---------------------------------------------------------------- storage + delivery
async def test_photo_store_retention(tmp_path):
    import os
    import time

    store = PhotoStore(tmp_path, retention_minutes=1)
    p = store.save("s1", "now.jpg", b"x")
    assert p.exists()
    old = time.time() - 3600
    os.utime(p.parent, (old, old))
    assert store.cleanup() == 1 and not p.exists()
    store.save("s2", "now.jpg", b"x")
    assert store.purge_all() == 1


async def test_preview_delivery_sends_nothing(tmp_path):
    img = tmp_path / "s" / "is-this-us.jpg"
    img.parent.mkdir()
    img.write_bytes(b"jpg")
    res = await PreviewDelivery().send(img, "caption", "s")
    assert res.ok and "not sent" in res.detail
    assert "WOULD SEND" in (img.parent / "telegram_preview.txt").read_text()


def test_private_images_are_git_ignored():
    ignore = (ROOT / ".gitignore").read_text()
    assert "data/runtime/" in ignore


# ---------------------------------------------------------------- telegram controls
def test_callback_parsing_and_prompts(tmp_path, script, catalog, lines):
    from is_this_us.bot.show_controls import callback_data, parse_callback, prompt_for

    assert parse_callback(callback_data(Cmd.SEND_YES, "ab12cd")) == (Cmd.SEND_YES, "ab12cd")
    assert parse_callback("show:cancel:ab12cd") is None  # only answer buttons, not arbitrary commands
    assert parse_callback("show:send_yes") is None
    d = make_director(tmp_path, script, catalog, lines)
    d.session_id = "ab12cd"
    text, kb = prompt_for(ShowState.CONSENT, d, "the exhibition chat")
    assert "never public" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["show:consent_yes:ab12cd", "show:consent_no:ab12cd"]
    assert prompt_for(ShowState.NARRATION, d, "x") is None


async def test_show_controller_operator_only_and_stale(tmp_path, script, catalog, lines):
    from is_this_us.bot.show_controls import ShowController

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.messages.append((chat_id, text, reply_markup))

    bot = FakeBot()
    ctrl = ShowController(lambda: make_director(tmp_path, script, catalog, lines), [42])
    assert ctrl.is_operator(42) and not ctrl.is_operator(7)
    assert "No performance" in ctrl.command(Cmd.NEXT)
    msg = ctrl.start(chat_id=42, bot=bot)
    assert "starting" in msg and "already running" in ctrl.start(42, bot)
    d = ctrl.director
    while d.state is not ShowState.READINESS:
        await asyncio.sleep(0.005)
    assert "earlier performance" in ctrl.command(Cmd.READY, "old000")
    assert ctrl.command(Cmd.READY, d.session_id).startswith("ok")
    while d.state is not ShowState.CONSENT:
        await asyncio.sleep(0.005)
    ctrl.command(Cmd.CONSENT_NO, d.session_id)
    await ctrl.task
    await asyncio.sleep(0.01)
    texts = [t for _, t, _ in bot.messages]
    assert any("Pose session" in t for t in texts) and any("photograph" in t for t in texts)
    assert any("finished" in t for t in texts)
    assert "finished" in ctrl.status_line()


async def test_telegram_delivery_requires_chat_and_handles_errors(tmp_path):
    from aiogram.exceptions import TelegramNetworkError

    from is_this_us.performance.delivery import TelegramDelivery

    img = tmp_path / "x.jpg"
    img.write_bytes(b"jpg")

    class Bot:
        def __init__(self, fail=False):
            self.fail, self.calls = fail, []

        async def send_photo(self, chat_id, photo, caption=None, **kw):
            self.calls.append(chat_id)
            if self.fail:
                raise TelegramNetworkError(method=None, message="down")

    assert not (await TelegramDelivery(Bot(), None).send(img, "c", "s")).ok
    ok_bot = Bot()
    assert (await TelegramDelivery(ok_bot, -100123).send(img, "c", "s")).ok and ok_bot.calls == [-100123]
    res = await TelegramDelivery(Bot(fail=True), -100123).send(img, "c", "s")
    assert not res.ok and "TelegramNetworkError" in res.detail


# ---------------------------------------------------------------- voice cache
def test_voice_cache_status_regenerate_prune(tmp_path, script, catalog, lines):
    narr = MockSynthesizer(AudioCache(tmp_path / "narration"))
    short = MockSynthesizer(AudioCache(tmp_path / "lines"))
    voice = fast_voice(narr, short)
    items = all_utterances(script, catalog, lines)
    assert len({i.cue_id for i in items}) == len(items)
    reqs = {a.interactive.request for a in catalog.interactive("any")}
    assert sum(1 for i in items if i.cue_id.startswith("invitation.") and "~" in i.cue_id) == len(reqs)
    assert not any(i.cached for i in cache_status(items, voice))
    for i in items[:5]:
        voice._synth(i.kind).synthesize(i.text, "en", i.direction)
    assert sum(1 for i in cache_status(items, voice) if i.cached) == 5
    (tmp_path / "narration" / "stale.wav").write_bytes(b"x")
    assert prune(items, voice) == 1
    # changing the delivery direction is a different clip (cache invalidation)
    assert narr.key_for("Hello.", "en", "shy") != narr.key_for("Hello.", "en", "warm")


def test_realtime_synth_cache_key_covers_voice_model_direction(tmp_path):
    from is_this_us.speech.synthesizer import OpenAIRealtimeSynthesizer

    a = OpenAIRealtimeSynthesizer("k", "gpt-realtime-2.1-mini", "marin", AudioCache(tmp_path))
    b = OpenAIRealtimeSynthesizer("k", "gpt-realtime-2.1-mini", "cedar", AudioCache(tmp_path))
    assert a.key_for("Hello.") != b.key_for("Hello.")
    assert a.key_for("Hello.", "en", "shy") != a.key_for("Hello.", "en", "")
    assert "Delivery for this passage: shy" in a.instructions_for("en", "shy")
    # offline: a cached clip plays even without credentials
    wav, _ = a.cache.paths(a.key_for("Hello."))
    wav.write_bytes(b"RIFF")
    offline = OpenAIRealtimeSynthesizer("", "gpt-realtime-2.1-mini", "marin", AudioCache(tmp_path), voice_notes=False)
    assert offline.synthesize("Hello.").audio_path == wav
    assert offline.synthesize("Not cached.") is None


# ---------------------------------------------------------------- rehearsal helpers
async def test_keyboard_maps_yes_no_by_state(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    d.state = ShowState.CONSENT
    assert keyboard_command(d, "y") == (True, "ok: consent_yes")
    d.state = ShowState.DELIVERY
    assert keyboard_command(d, "n") == (True, "ok: send_no")
    d.state = ShowState.NARRATION
    assert keyboard_command(d, "")[1] == "ok: next"
    assert keyboard_command(d, "launch")[0] is False


async def test_participant_script_answers(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    sim = ParticipantScript(consent=True, send=False, delay_s=0.01)
    sim.attach(d)
    res = await d.run()
    assert sim.answered == ["ready", "consent_yes", "send_no"]
    assert res.outcome == "together" and res.delivery_detail.startswith("not sent")


def test_cli_dry_run_end_to_end(tmp_path, monkeypatch):
    """`itu show --dry-run --auto` in a clean environment: no network, no hardware, no keys."""
    from is_this_us import cli
    from is_this_us.config import get_settings

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("SHOW_OUTPUT_DIR", str(tmp_path / "perf"))
    monkeypatch.setenv("SHOW_TELEGRAM_CHAT_ID", "-100123")  # rehearse a configured chat: preview, never sent
    get_settings.cache_clear()
    try:
        cli.main(["show", "--programme", "show", "--dry-run", "--auto", "--fast", "0.002", "--no-voice",
                  "--out", str(tmp_path / "r")])
    finally:
        get_settings.cache_clear()
    outs = list((tmp_path / "r").glob("*/is-this-us.jpg"))
    assert len(outs) == 1
    assert "not sent" in (outs[0].parent / "telegram_preview.txt").read_text()


def test_web_stage_serves_only_what_is_on_screen(catalog):
    import json
    import urllib.request

    from is_this_us.performance.stage import View, WebStage

    web = WebStage("127.0.0.1", 0)
    try:
        web.chapter("paintings", "Paintings and Portraits")
        web.show(View("artifact", artifact=catalog.get("met:437890")))
        web.subtitle("Some are enormous.")
        state = json.loads(urllib.request.urlopen(web.url + "state").read())
        assert state["subtitle"] == "Some are enormous." and "AI-generated" in state["notice"]
        token = re.search(r"/img/([0-9]+\.jpg)", state["view_html"]).group(1)
        assert urllib.request.urlopen(web.url + "img/" + token).status == 200
        web.show(View("title"))
        with pytest.raises(Exception):
            urllib.request.urlopen(web.url + "img/" + token)
        web.show(View("artifact", artifact=catalog.get("meme:doge")))
        state = json.loads(urllib.request.urlopen(web.url + "state").read())
        assert "reference only" in state["view_html"] and "<img" not in state["view_html"]
    finally:
        web.close()


# ---------------------------------------------------------------- moments inside the story
def test_script_weaves_moments_into_every_chapter(script):
    tries = script.try_cues()
    assert {c.chapter for c in tries} == {"last_dog", "prehistory", "middle_ages", "paintings", "photography",
                                          "internet"}
    for c in tries:
        assert c.text and c.yes and c.no
    with pytest.raises(ScriptError):
        parse_script((SHOW / "script.en.md").read_text().replace("@yes: Oh. Yes. I think a dog would follow you.\n", ""))


async def test_moments_react_to_what_the_camera_sees(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, posture="sitting")  # stand-in sits, head tilted
    answer(d, HAPPY)
    res = await d.run()
    seen = dict(d.tries)
    assert seen["middle_ages.19"] is True  # "sit down" — seen
    assert seen["prehistory.16"] is False  # "stand like the hunter" — not seen
    subs = d.stage.of("subtitle")
    assert "Oh. I think I would stay too." in subs
    assert "That's all right. Hunters were probably very busy." in subs
    assert "We have practised a little already." in subs  # the finale remembers
    assert "participation" in [b for _, b in res.transitions]
    assert not any((tmp_path / "out").glob("*/moment*"))  # moments never store anything


async def test_operator_can_confirm_or_skip_a_moment(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, posture="sitting", try_seconds=5)
    answer(d, HAPPY)

    def on_state(director, st):
        if st is ShowState.PARTICIPATION:
            cmd = Cmd.READY if director.cue_id == "prehistory.16" else Cmd.NEXT
            asyncio.get_running_loop().call_soon(lambda: director.command(cmd))
    d.listeners.append(on_state)
    await d.run()
    assert dict(d.tries)["prehistory.16"] is True  # operator said "they did it" although the camera saw sitting
    assert dict(d.tries)["middle_ages.19"] is False  # operator moved on
    assert "Oh. I think I would stay too." not in d.stage.of("subtitle")


async def test_no_camera_leaves_moments_out(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, camera=None)
    answer(d, HAPPY)
    res = await d.run()
    assert d.tries == [] and "participation" not in [b for _, b in res.transitions]
    assert "Is someone there?" not in " ".join(d.stage.of("subtitle"))
    assert "We have practised a little already." not in d.stage.of("subtitle")


async def test_start_directly_at_the_interactive_part(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    answer(d, HAPPY)
    res = await d.run(start_at="invitation")
    assert res.chapters == ["invitation", "present"] and res.outcome == "together"
    assert d.stage.of("subtitle")[0] == "I have studied dogs for a very long time."
    with pytest.raises(ValueError):
        await make_director(tmp_path, script, catalog, lines).run(start_at="dinosaurs")


def test_web_buttons_need_the_operator_key(catalog):
    import json
    import threading
    import urllib.error
    import urllib.request

    from is_this_us.performance.stage import WebStage

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    pressed = []
    web = WebStage("127.0.0.1", 0)
    try:
        def post(url):
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, method="POST")).read())
        with pytest.raises(urllib.error.HTTPError):  # controls not enabled yet
            post(web.url + "control?key=" + web.key + "&cmd=pose")
        web.enable_controls(lambda c: (pressed.append(c) or (True, f"ok: {c}")), loop)
        assert post(web.control_url.replace("?", "control?") + "&cmd=pose") == {"ok": True, "msg": "ok: pose"}
        with pytest.raises(urllib.error.HTTPError):
            post(web.url + "control?key=wrong&cmd=pose")
        with pytest.raises(urllib.error.HTTPError):
            post(web.control_url.replace("?", "control?") + "&cmd=rm_rf")
        assert pressed == ["pose"]
        page = urllib.request.urlopen(web.url).read().decode()
        assert "Go to interactive" in page  # buttons are drawn only when the page URL has the key
    finally:
        web.close()
        loop.call_soon_threadsafe(loop.stop)


async def test_telegram_show_interactive(tmp_path, script, catalog, lines):
    from is_this_us.bot.show_controls import ShowController

    class Bot:
        async def send_message(self, *a, **k):
            pass

    ctrl = ShowController(lambda: make_director(tmp_path, script, catalog, lines), [1])
    assert "Unknown part" in ctrl.start(1, Bot(), start_at="dinosaurs")
    assert "straight to the interactive part" in ctrl.start(1, Bot(), start_at="invitation")
    while ctrl.director.state is not ShowState.READINESS:
        await asyncio.sleep(0.005)
    assert ctrl.director.chapters_played == ["invitation"]
    ctrl.command(Cmd.CANCEL)
    await ctrl.task


# ---------------------------------------------------------------- live camera view
async def test_live_view_only_while_the_robot_is_looking(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, live_always=False)
    answer(d, HAPPY)
    await d.run()
    live = [v for k, v in d.stage.events if k == "live"]
    assert True in live and live[-1] is False  # shown, and hidden again at the end
    # it is on during the pose session and hidden before the composition appears
    ev = d.stage.events
    first_comp = next(i for i, (k, v) in enumerate(ev) if k == "show" and v == "composition")
    last_live = max(i for i, (k, v) in enumerate(ev) if k == "live" and v is True)
    assert last_live < first_comp
    assert any(k == "live" and v is False for k, v in ev[last_live:first_comp])


async def test_live_view_can_stay_on_for_the_whole_show(tmp_path, script, catalog, lines):
    from is_this_us.performance.voice import ShowVoice, Speaker
    from is_this_us.speech.synthesizer import NullSynthesizer

    voice = ShowVoice(NullSynthesizer(), NullSynthesizer(), Speaker(enabled=False), time_scale=0.004)
    d = make_director(tmp_path, script, catalog, lines, live_always=True, voice=voice)
    answer(d, HAPPY)
    await d.run()
    ev = d.stage.events
    live_on = [i for i, (k, v) in enumerate(ev) if k == "live" and v]
    first_chapter_change = [i for i, (k, v) in enumerate(ev) if k == "chapter"][1]
    assert live_on and live_on[0] < first_chapter_change  # already up during the first chapter's narration
    assert ev[[i for i, (k, _) in enumerate(ev) if k == "live"][-1]][1] is False  # hidden after the show


def test_live_view_render_and_checks():
    from is_this_us.performance.live_view import checks_for, render
    from is_this_us.performance.runtime import SyntheticParticipant

    stand_in = SyntheticParticipant("sitting", head_tilt_deg=14)
    frame = stand_in.read()
    reading = PoseReader(stand_in).read(frame.image)
    jpeg = render(frame.image, reading)
    assert Image.open(BytesIO(jpeg)).format == "JPEG"
    checks = {c.label: c.ok for c in checks_for(reading, posture="sitting", head_tilt=True)}
    assert checks == {"I can see you": True, "whole body in the picture": True, "in the middle": True,
                      "sitting": True, "head tilted": True}
    nobody = checks_for(None, posture="standing")
    assert not any(c.ok for c in nobody)
    assert [c.label for c in checks_for(reading, any_person=True)] == ["I can see you"]


def test_web_live_frame_is_memory_only_and_hidden_when_off():
    import json
    import urllib.error
    import urllib.request

    from is_this_us.performance.stage import WebStage

    web = WebStage("127.0.0.1", 0)
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(web.url + "live.jpg")
        web.live(b"\xff\xd8jpeg", [("sitting", True)])
        assert urllib.request.urlopen(web.url + "live.jpg").read() == b"\xff\xd8jpeg"
        state = json.loads(urllib.request.urlopen(web.url + "state").read())
        assert state["live"]["laptop"]["on"] and state["live"]["laptop"]["checks"] == [["sitting", True]]
        web.live(b"\xff\xd8robot", [], feed="robot")  # the dog's own camera is a second, separate feed
        assert urllib.request.urlopen(web.url + "live.jpg?feed=robot").read() == b"\xff\xd8robot"
        assert urllib.request.urlopen(web.url + "live.jpg").read() == b"\xff\xd8jpeg"
        web.live(None, [])
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(web.url + "live.jpg")
    finally:
        web.close()


# ---------------------------------------------------------------- robot placement + mirrored photo
def test_interactive_pictures_say_where_the_robot_goes(catalog):
    for a in catalog.interactive("any"):
        if a.interactive.pose_subject == "human":
            assert a.interactive.robot_side in ("left", "right", "front"), a.id
            assert a.interactive.robot_behavior in ("sit", "stand", "lie_down"), a.id
    zorn = catalog.get("met:437966").interactive
    assert "LEFT" in zorn.placement() and zorn.robot_behavior == "sit"
    assert catalog.get("met:437841").interactive.robot_behavior == "lie_down"


class SpyMotion:
    enabled = True

    def __init__(self):
        self.requested, self.stopped = [], []

    def request(self, name):
        self.requested.append(name)

    def stop(self, reason):
        self.stopped.append(reason)


async def test_robot_takes_the_dog_pose_before_the_person_poses(tmp_path, script, catalog, lines):
    motion = SpyMotion()
    d = make_director(tmp_path, script, catalog, lines, motion=motion)
    answer(d, HAPPY)
    res = await d.run(start_at="invitation")
    assert res.artifact == "met:437966" and "sit" in motion.requested
    subs = d.stage.of("subtitle")
    assert subs.index("I will try to be the dog in the picture. You can be the person.") < subs.index(
        "Hold still… just for a moment.")
    readiness = [b for _, b in res.transitions]
    assert "participant_readiness" in readiness


async def test_no_motion_no_robot_pose_line(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines)
    answer(d, HAPPY)
    await d.run(start_at="invitation")
    assert "I will try to be the dog in the picture. You can be the person." not in d.stage.of("subtitle")


async def test_photo_is_mirrored_like_the_live_view(tmp_path, script, catalog, lines):
    import numpy as np

    from is_this_us.vision.frame_sources import Frame

    stand_in = SyntheticParticipant("sitting")
    left_red = np.zeros((60, 80, 3), dtype=np.uint8)
    left_red[:, :40] = (255, 0, 0)

    class Cam:
        def read(self):
            return Frame(image=left_red, source="webcam")

        def close(self):
            pass

    d = make_director(tmp_path, script, catalog, lines, camera=None)
    d.pose_reader = PoseReader(stand_in)
    d.frame_source_factory = Cam
    answer(d, HAPPY)
    await d.run(start_at="invitation")
    photo = Image.open(next((tmp_path / "out").glob("*/now.jpg"))).convert("RGB")
    assert photo.getpixel((70, 30))[0] > 200 and photo.getpixel((10, 30))[0] < 60  # red moved to the right
    d.pose_reader = PoseReader(stand_in, mirrored=True)  # a camera that already mirrors is left alone
    assert d._mirror(Frame(image=left_red, source="webcam")) is False


def test_telegram_readiness_prompt_names_the_robot_spot(tmp_path, script, catalog, lines):
    from is_this_us.bot.show_controls import prompt_for

    d = make_director(tmp_path, script, catalog, lines)
    d.session_id, d.artifact = "ab12cd", catalog.get("met:437966")
    text, _ = prompt_for(ShowState.READINESS, d, "the chat")
    assert "Place the robot on the LEFT of the person" in text


# ---------------------------------------------------------------- camera-guided walking
def _sim(side, person, posture):
    from is_this_us.motion.guard import MotionGuard
    from is_this_us.performance.approach import Approacher
    from is_this_us.performance.sim_world import SimWorld
    from is_this_us.robot.mock import MockRobotAdapter

    t = [0.0]

    def clock():
        return t[0]

    def sleep(d):
        t[0] += d

    world = SimWorld(person, posture, clock=clock)
    robot = MockRobotAdapter(time_scale=0)
    robot.connect()
    robot.world = world
    guard = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0)
    return Approacher(robot, guard, world.camera, world, clock=clock, sleep=sleep), world, robot, guard


@pytest.mark.parametrize("side,person,posture", [
    ("left", (2.2, 0.4), "sitting"), ("right", (2.0, -0.6), "standing"), ("left", (2.5, -0.8), "sitting"),
    ("right", (3.0, 0.0), "standing"), ("left", (0.5, 3.0), "sitting"), ("front", (1.5, 1.2), "sitting")])
def test_robot_walks_to_the_painted_side_and_keeps_its_distance(side, person, posture):
    a, world, robot, _ = _sim(side, person, posture)
    res = a.approach(side=side, posture=posture)
    assert res.arrived, res.detail
    assert world.side_of_person() == side
    assert world.min_distance >= 0.55  # never closer than the safety gap
    assert all(abs(vx) <= 0.3 and abs(vyaw) <= 0.6 for vx, vy, vyaw in robot.velocities)
    assert robot.velocities[-1] == (0.0, 0.0, 0.0)  # halted
    out = len(robot.velocities)
    assert a.go_home() == "done"
    assert all(vx >= 0 for vx, vy, vyaw in robot.velocities[out:])  # turns round: never walks backwards
    assert abs(math.atan2(math.sin(world.pose[2]), math.cos(world.pose[2]))) < 0.1  # faces the audience again
    assert abs(world.pose[0]) < 0.05 and abs(world.pose[1]) < 0.05  # back where it started


def test_walk_gives_up_safely():
    a, world, robot, _ = _sim("left", (-2.0, 0.0), "sitting")  # behind the robot: never seen
    res = a.approach(side="left")
    assert res.status == "not_found" and robot.velocities[-1] == (0.0, 0.0, 0.0)
    a, world, robot, _ = _sim("left", (2.2, 0.4), "sitting")
    n = {"i": 0}

    def stop_after_a_few():
        n["i"] += 1
        return n["i"] > 5
    res = a.approach(side="left", should_stop=stop_after_a_few)
    assert res.status == "stopped" and robot.velocities[-1] == (0.0, 0.0, 0.0)


def test_walk_refused_without_permission_and_not_standing():
    from is_this_us.motion.guard import MotionGuard
    from is_this_us.performance.approach import Approacher
    from is_this_us.robot.base import BodyState

    a, world, robot, guard = _sim("left", (2.2, 0.4), "sitting")
    robot.body = BodyState.SITTING
    assert a.approach().status == "refused"

    class RealNoArm:
        is_real, can_walk, body = True, True, BodyState.STANDING

        def status(self):
            from is_this_us.robot.base import RobotStatus
            return RobotStatus(connected=True, is_real=True, model="x", body=self.body, battery_percent=None,
                               error_code=0, hardware_verified=False, detail="")
    g = MotionGuard(RealNoArm(), hardware_gates_open=True)
    res = Approacher(RealNoArm(), g, world.camera, world).approach()
    assert res.status == "refused" and "armed" in res.detail

    class NoWalk(RealNoArm):
        can_walk = False
    assert "cannot walk" in MotionGuard(NoWalk(), hardware_gates_open=True).walk_allowed()[1]


def test_bridge_walk_is_clamped_and_move_is_not_free():
    from is_this_us.robot.zhaw_bridge import ZhawBridgeAdapter, clamp_velocity

    assert clamp_velocity(2.0, -1.0, 5.0) == (0.3, -0.15, 0.6)
    bridge = ZhawBridgeAdapter()
    bridge._sock = object()
    with pytest.raises(PermissionError):
        bridge.request("move", {"vx": 1.0})  # only through walk()


async def test_show_walks_to_the_person_then_home(tmp_path, script, catalog, lines):
    from is_this_us.performance.runtime import simulated_approacher
    from is_this_us.config import Settings

    approacher = simulated_approacher(Settings(_env_file=None), fast=0.0)
    d = make_director(tmp_path, script, catalog, lines, approacher=approacher)
    answer(d, HAPPY)
    res = await d.run(start_at="invitation")
    assert res.outcome == "together"
    assert [w.split(":")[0] for w in d.walk_log] == ["approach", "home"]
    subs = d.stage.of("subtitle")
    assert subs.index("May I come a little closer?") < subs.index("Here I am.") < subs.index(
        "Hold still… just for a moment.")
    assert "I will go back to my place now." in subs
    assert "left of the person" in d.walk_log[0]  # Zorn: the collie is on the left
    assert abs(approacher.world.pose[0]) < 0.05  # and it walked back to where it started


async def test_cancel_during_walk_stops_the_robot(tmp_path, script, catalog, lines):
    from is_this_us.config import Settings
    from is_this_us.performance.runtime import simulated_approacher

    approacher = simulated_approacher(Settings(_env_file=None), fast=0.02)
    d = make_director(tmp_path, script, catalog, lines, approacher=approacher)
    answer(d, {ShowState.READINESS: Cmd.READY})

    def on_state(director, st):
        if st is ShowState.APPROACH:
            asyncio.get_running_loop().call_later(0.3, lambda: director.command(Cmd.CANCEL))
    d.listeners.append(on_state)
    res = await d.run(start_at="invitation")
    assert res.final_state is ShowState.CANCELLED
    assert approacher.robot.velocities[-1] == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------- regressions from the first real run
@pytest.mark.parametrize("delay", [0.0, 0.003, 0.01, 0.02, 0.04, 0.07, 0.1])
async def test_go_to_interactive_works_whenever_it_is_pressed(tmp_path, script, catalog, lines, delay):
    """Pressing "Go to interactive" was lost if it landed during a short line before a screen change."""
    d = make_director(tmp_path, script, catalog, lines, mode="guided")
    answer(d, HAPPY)
    pressed = {"ok": False}

    def on_state(director, st):
        if not pressed["ok"] and st in (ShowState.INTRODUCTION, ShowState.PARTICIPATION, ShowState.ARTWORK):
            def press():
                pressed["ok"] = director.command(Cmd.POSE)[0] or pressed["ok"]
            asyncio.get_running_loop().call_later(delay, press)
    d.listeners.append(on_state)

    def hold_next(director, st):  # guided holds after the jump would otherwise wait forever
        if st is ShowState.ARTWORK and director.chapter_id not in ("last_dog",):
            asyncio.get_running_loop().call_later(0.01, lambda: director.command(Cmd.NEXT))
    d.listeners.append(hold_next)
    res = await asyncio.wait_for(d.run(), 20)
    assert pressed["ok"]
    assert res.chapters[:2] == ["last_dog", "invitation"], res.chapters
    assert res.outcome == "together"


async def test_camera_view_stays_up_through_the_moment(tmp_path, script, catalog, lines):
    """The camera view vanished ~1 s after the robot saw someone; it must last through the reaction."""
    d = make_director(tmp_path, script, catalog, lines, moment_hold_s=0.05, live_linger_s=0.05, try_seconds=1.0)
    answer(d, HAPPY)
    await d.run()
    ev = d.stage.events
    yes = next(i for i, (k, v) in enumerate(ev) if k == "subtitle" and v.startswith("Hello. You can look"))
    before = [v for k, v in ev[:yes] if k == "live"]
    after = [v for k, v in ev[yes:] if k == "live"]
    assert before and before[-1] is True  # on while the robot reacts…
    first_off = next(i for i, (k, v) in enumerate(ev) if i > yes and k == "live" and v is False)
    assert any(k == "live" and v is True for k, v in ev[yes:first_off])  # …still updating after it…
    assert after[-1] is False  # …and only then hidden


async def test_next_pressed_during_a_line_is_not_lost(tmp_path, script, catalog, lines):
    d = make_director(tmp_path, script, catalog, lines, mode="guided")
    d.state = ShowState.PARTICIPATION
    d._inbox.append(Cmd.NEXT)
    d._set_state(ShowState.ARTWORK)
    assert list(d._inbox) == [Cmd.NEXT]  # still meaningful at the hold
    d._inbox.append(Cmd.SEND_YES)
    d._set_state(ShowState.NARRATION)
    assert Cmd.SEND_YES not in d._inbox  # meaningless now: dropped


# ---------------------------------------------------------------- the good boy encounter (finale, docs/UX.md)
def enc_director(tmp_path, script, catalog, lines, *, motion=None, delivery=None, **kw):
    kw.setdefault("finale", "encounter")
    kw.setdefault("stable_s", 0.0)
    kw.setdefault("final_hold_s", 0.2)
    return make_director(tmp_path, script, catalog, lines, motion=motion, delivery=delivery, **kw)


def mock_motion():
    from is_this_us.motion.guard import MotionGuard
    from is_this_us.motion.planner import SafeMotionPlanner
    from is_this_us.performance.motion import MotionCues
    from is_this_us.robot.mock import MockRobotAdapter

    robot = MockRobotAdapter(time_scale=0.0)
    robot.connect()
    return MotionCues(MotionGuard(robot, hardware_gates_open=False, cooldown_s=0, timeout_s=5), SafeMotionPlanner(),
                      enabled=True), robot


async def test_encounter_is_the_default_finale_and_follows_the_contract(tmp_path, script, catalog, lines):
    from is_this_us.performance.encounter import attribution

    motion, robot = mock_motion()
    delivery = CountingDelivery()
    d = enc_director(tmp_path, script, catalog, lines, motion=motion, delivery=delivery)
    answer(d, {ShowState.CONSENT: Cmd.CONSENT_YES})  # nobody presses Ready: it starts by itself
    t0 = {}
    d.listeners.append(lambda dd, st: t0.setdefault(st.value, dd.clock()))
    res = await d.run(start_at="invitation")
    assert res.final_state is ShowState.FINISHED and res.outcome == "together"
    # one Telegram message: the ARCHIVE image (never the visitor's photo), short caption, italic credit
    assert len(delivery.sent) == 1
    image, text, _ = delivery.sent[0]
    art = d.artifact
    assert image == art.image_path and "now.jpg" not in str(image)
    first, _, credit = text.partition("\n\n")
    assert first in ("me and who?🥺", "us?👉👈")
    assert credit.startswith("<i>") and credit.endswith("</i>") and art.title.split("(")[0].strip()[:20] in credit
    assert attribution(art).startswith(art.artist)
    # speech: short good-boy lines, the caption is never spoken
    subs = d.stage.of("subtitle")
    assert "Am I a good boy already?" in subs and "A really good boy now?" in subs
    assert not any(c in s for s in subs for c in ("us?👉👈", "me and who?🥺"))
    # the physical finale: the image's pose, then a hold measured after settling
    assert robot.calls, "the robot took a pose"
    assert any("final pose" in n and ("simulated" in n or "success" in n) for n in d.encounter_log)
    held = [n for n in d.encounter_log if n.startswith("held the pose")]
    assert held and float(held[0].split("for ")[1].split(" s")[0]) >= 0.2 - 0.01
    states = [b for _, b in res.transitions]
    assert states.index("sending") < states.index("final_pose") < states.index("composition")
    assert "delivery" not in states  # no send-confirmation step for the archive image


async def test_encounter_skip_ends_the_hold_and_cancel_stops(tmp_path, script, catalog, lines):
    motion, robot = mock_motion()
    d = enc_director(tmp_path, script, catalog, lines, motion=motion, final_hold_s=30.0, photo_in_encounter=False)

    def on_state(director, st):
        if st is ShowState.FINAL_POSE:
            asyncio.get_running_loop().call_later(0.05, lambda: director.command(Cmd.SKIP))
    d.listeners.append(on_state)
    res = await asyncio.wait_for(d.run(start_at="invitation"), 10)
    assert res.final_state is ShowState.FINISHED
    assert "hold ended early by the operator" in d.encounter_log

    motion2, robot2 = mock_motion()
    d2 = enc_director(tmp_path, script, catalog, lines, motion=motion2, final_hold_s=30.0, photo_in_encounter=False)
    d2.listeners.append(lambda dd, st: st is ShowState.FINAL_POSE and asyncio.get_running_loop().call_later(
        0.05, lambda: dd.command(Cmd.CANCEL)))
    res2 = await asyncio.wait_for(d2.run(start_at="invitation"), 10)
    assert res2.final_state is ShowState.CANCELLED and "StopMove" in robot2.calls


async def test_encounter_without_motion_does_not_claim_a_pose(tmp_path, script, catalog, lines):
    d = enc_director(tmp_path, script, catalog, lines, photo_in_encounter=False)
    res = await d.run(start_at="invitation")
    assert res.final_state is ShowState.FINISHED
    assert any("not performed" in n for n in d.encounter_log)
    assert not any(n.startswith("held the pose") for n in d.encounter_log)


async def test_encounter_without_camera_sends_nothing(tmp_path, script, catalog, lines):
    delivery = CountingDelivery()
    d = enc_director(tmp_path, script, catalog, lines, camera=None, delivery=delivery)
    res = await d.run(start_at="invitation")
    assert res.outcome == "alone" and delivery.sent == []
    assert any("no camera" in n for n in d.encounter_log)


async def test_encounter_delivery_failure_does_not_replay_motion(tmp_path, script, catalog, lines):
    motion, robot = mock_motion()
    d = enc_director(tmp_path, script, catalog, lines, motion=motion, delivery=CountingDelivery(ok=False),
                     photo_in_encounter=False)
    await d.run(start_at="invitation")
    assert any(n.startswith("Telegram (archive image): boom") for n in d.encounter_log)
    finals = [n for n in d.encounter_log if n.startswith("final pose")]
    assert len(finals) == 1  # the pose happened once; delivery failure changes nothing


async def test_encounter_only_sends_images_we_may_send(tmp_path, script, catalog, lines):
    from is_this_us.performance.encounter import deliverable

    assert not deliverable(catalog.get("meme:doge"))  # reference-only memes are never sent
    for _ in range(3):
        d = enc_director(tmp_path, script, catalog, lines, photo_in_encounter=False, delivery=CountingDelivery())
        await d.run(start_at="invitation")
        assert d.artifact.kind == "art" and deliverable(d.artifact)


def test_caption_and_attribution_format(catalog):
    import random as _r

    from dataclasses import replace

    from is_this_us.performance.encounter import telegram_message

    art = replace(catalog.get("met:437966"), title="A <b>bold</b> & title", date="1897")
    text = telegram_message(art, {"painting": ["us?👉👈"], "meme": ["hehe or not hehe?"]}, _r.Random(0))
    assert text.startswith("us?👉👈\n\n<i>Anders Zorn — A &lt;b&gt;bold&lt;/b&gt; &amp; title, 1897.")
    assert "Public Domain (The Met Open Access)." in text and text.endswith("</i>")
    meme = replace(art, kind="meme")
    assert telegram_message(meme, {"painting": ["x"], "meme": ["hehe or not hehe?"]}).startswith("hehe or not hehe?")


def test_captions_are_written_never_spoken(lines):
    spoken = {t for _, t in lines.fixed_lines()}
    for c in lines.captions()["painting"] + lines.captions()["meme"]:
        assert c not in spoken


def test_stability_tracker_needs_a_held_pose():
    from is_this_us.performance.encounter import StabilityTracker
    from is_this_us.performance.pose_session import PoseReading

    t = StabilityTracker(hold_s=2.0)
    sit = PoseReading(True, "ok", "center", "sitting", None)
    assert not t.update(sit, 0.0) and not t.update(sit, 1.0) and t.update(sit, 2.1)
    assert not t.update(PoseReading(True, "ok", "center", "standing", None), 2.2)  # changed pose: restart
    assert not t.update(PoseReading(False), 2.3)
