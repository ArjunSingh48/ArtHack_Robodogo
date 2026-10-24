"""Performance mode: script, catalog, director state machine, pose, composition, delivery, voice."""

from __future__ import annotations

import asyncio
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
                      mode=mode, tick_s=0.005, hint_gap_s=0.0, rng=random.Random(0), **kw)
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
    assert d.stage.of("subtitle").count("How do I become a good dog?") == 2


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

        async def send_photo(self, chat_id, photo, caption=None):
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
    get_settings.cache_clear()
    try:
        cli.main(["show", "--dry-run", "--auto", "--fast", "0.002", "--no-voice", "--out", str(tmp_path / "r")])
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
