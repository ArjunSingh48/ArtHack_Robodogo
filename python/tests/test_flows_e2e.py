"""Complete mocked interactions through the orchestrator and the Telegram flows."""

from __future__ import annotations

import asyncio
import html
from dataclasses import replace
from types import SimpleNamespace

import pytest

from is_this_us.bot.flows import CB_ANOTHER, CB_KEEP, CB_YES, VisitorFlows
from is_this_us.domain import ExecutionStatus
from is_this_us.orchestrator import State
from is_this_us.robot.mock import MockRobotAdapter
from is_this_us.speech.synthesizer import AudioCache, MockSynthesizer
from is_this_us.vision.frame_sources import Frame
from tests.conftest import FakeEstimator

import numpy as np


class CollectingNotifier:
    def __init__(self):
        self.flows: VisitorFlows | None = None
        self.sent: list[tuple[int, list]] = []
        self.outcomes = []

    async def deliver(self, chat_id, outcome):
        replies = self.flows.replies_for_outcome(chat_id, outcome)
        self.sent.append((chat_id, replies))
        self.outcomes.append(outcome)
        return f"sent {len(replies)}"


class StaticSource:
    name = "test:static"

    def read(self):
        return Frame(np.zeros((480, 640, 3), dtype=np.uint8), self.name)

    def close(self):
        pass


@pytest.fixture
def setup(make_orchestrator, storage, repo, tmp_path, monkeypatch):
    # photos in these tests are placeholders; the fake estimator ignores pixels
    monkeypatch.setattr("is_this_us.orchestrator.decode_image_bytes", lambda b: np.zeros((4, 4, 3), np.uint8))
    def _setup(posture="sitting", gesture=None, operator_ids=(42,)):
        notifier = CollectingNotifier()
        synth = MockSynthesizer(AudioCache(tmp_path / "a"))
        orch = make_orchestrator(estimator=FakeEstimator(posture, gesture), notifier=notifier, synth=synth)
        flows = VisitorFlows(orchestrator=orch, storage=storage, repo=repo, operator_ids=list(operator_ids),
                             bot_username="IsThisUsBot", frame_source_factory=StaticSource)
        notifier.flows = flows
        return flows, orch, notifier, synth
    return _setup


async def test_complete_installation_interaction(setup, storage):
    flows, orch, notifier, synth = setup("kneeling", "extending")
    chat = 777
    assert "make a memory with me" in flows.start(chat, "en")[0].text
    # not paired yet
    replies = await flows.observe(chat)
    assert "QR" in replies[0].text
    # operator makes a QR, visitor pairs via deep link payload
    qr = flows.operator(42, "qr")[0]
    assert qr.kind == "photo" and qr.photo[:4] == b"\x89PNG"
    token = qr.text.split("pair_")[1].strip()
    assert "connected" in flows.start(chat, "en", f"pair_{token}")[0].text
    # observe → orchestrator → notifier
    assert await flows.observe(chat) == []
    (to, sent), = notifier.sent
    assert to == chat
    kinds = [r.kind for r in sent]
    assert kinds == ["photo"]
    caption = sent[0].text
    assert caption.split("\n")[0] in {"me and who?🥺", "us?👉👈"}
    assert "The Metropolitan Museum of Art" in caption or "Cleveland Museum of Art" in caption
    assert caption.split("\n\n")[1].startswith("<i>") and caption.endswith("</i>")
    assert not synth.requests and not sent[0].buttons and sent[0].image_required
    assert sent[0].photo == orch.repo.local_image(notifier.outcomes[0].artwork)

    iid = notifier.outcomes[0].interaction_id
    row = storage.get_interaction(iid, chat)
    assert row.origin == "installation" and row.artwork_id
    assert set(row.observation) <= {"posture", "posture_confidence", "gestures", "proximity",
                                    "people_detected", "landmark_confidence"}
    before = list(orch.guard.robot.calls)
    # Old experimental buttons cannot add a choice or replay a primary encounter.
    for callback in (CB_YES, CB_KEEP, CB_ANOTHER):
        assert "passed" in (await flows.callback(chat, f"{callback}:{iid}"))[0].text
    assert await orch.another(chat, iid) is None
    assert orch.guard.robot.calls == before


async def test_state_machine_trace(make_orchestrator):
    from is_this_us.notifier import ConsoleNotifier

    orch = make_orchestrator(estimator=FakeEstimator("sitting"), notifier=ConsoleNotifier())
    orch.storage.touch_visitor(1)
    out = await orch.observe_installation(1, StaticSource())
    assert out.trace == [State.IDLE, State.OBSERVING, State.INTERPRETING, State.MATCHING, State.PLANNING,
                         State.MOVING, State.NARRATING, State.DELIVERING, State.AWAITING_RESPONSE, State.IDLE]
    assert out.result.status is ExecutionStatus.SIMULATED
    assert out.delivery_status == "previewed image (not sent)"


async def test_photo_flow_never_moves_the_installation_robot(setup):
    flows, orch, notifier, _ = setup("sitting")
    real_robot: MockRobotAdapter = orch.guard.robot
    await flows.photo(5, b"not-used-by-fake-estimator")
    assert real_robot.calls == []  # simulated separately
    (_, sent), = notifier.sent
    assert "simulated robot" in sent[1].text


async def test_photo_without_person(setup):
    flows, orch, notifier, _ = setup()
    orch.estimator.people = 0
    await flows.photo(6, b"x")
    (_, sent), = notifier.sent
    assert sent[0].kind == "text" and "see you" in sent[0].text


async def test_privacy_between_visitors(setup):
    flows, orch, notifier, _ = setup()
    await flows.photo(1, b"x")
    iid = notifier.sent[0][1][1].buttons[0][0][1].split(":")[1]
    stranger = await flows.callback(2, f"{CB_KEEP}:{iid}")
    assert "passed" in stranger[0].text  # treated as expired, nothing leaked
    assert flows.storage.memories(2) == []


async def test_forget_and_stop(setup, storage):
    flows, orch, notifier, _ = setup()
    await flows.photo(8, b"x")
    iid = notifier.sent[0][1][1].buttons[0][0][1].split(":")[1]
    await flows.callback(8, f"{CB_KEEP}:{iid}")
    assert flows.forget(8)[0].text.startswith("I've forgotten")
    assert storage.memories(8) == [] and storage.get_interaction(iid, 8) is None
    # an unpaired stranger's /stop does not touch the robot
    assert "Stopped" in flows.stop(9, user_id=9)[0].text
    assert "StopMove" not in orch.guard.robot.calls
    # an operator's /stop does
    flows.stop(9, user_id=42)
    assert "StopMove" in orch.guard.robot.calls


def test_operator_commands_are_protected(setup):
    flows, orch, *_ = setup()
    assert "operator" in flows.operator(1, "arm")[0].text
    assert flows.operator(42, "arm")[0].text.startswith("🟢")  # mock robot can be armed
    assert "connected=True" in flows.operator(42, "status")[0].text
    flows.operator(42, "close")
    assert orch.observation_open is False


async def test_voice_failure_falls_back_to_text(setup):
    flows, orch, notifier, synth = setup()
    synth.fail = True
    await flows.photo(11, b"x")
    kinds = [r.kind for r in notifier.sent[0][1]]
    assert kinds == ["text", "photo"]


async def test_language_follows_visitor(setup):
    flows, orch, notifier, _ = setup("standing")
    flows.start(12, "ru")
    await flows.photo(12, b"x")
    assert "Это мы?" in notifier.sent[0][1][1].text


async def test_primary_no_match_is_silent(setup, storage):
    flows, orch, notifier, synth = setup()
    orch.estimator.people = 0
    storage.pair(10, storage.create_pairing_token())
    assert await flows.observe(10) == []
    assert notifier.sent == [(10, [])]
    assert not synth.requests and not orch.guard.robot.calls


async def test_primary_caption_and_console_match(setup, capsys):
    from is_this_us.notifier import ConsoleNotifier

    flows, orch, notifier, _ = setup()
    outcome = await orch.observe_installation(4, StaticSource())
    expected = flows.replies_for_outcome(4, outcome)[0].text
    status = await ConsoleNotifier().deliver(4, outcome)
    printed = capsys.readouterr().out
    assert expected in printed and str(outcome.image_source) in printed
    assert "PREVIEW ONLY" in printed and status == "previewed image (not sent)"
    assert "button" not in printed and "voice:" not in printed


@pytest.mark.parametrize("invalid", ["stranger", "forgotten", "expired"])
async def test_primary_delivery_rejects_invalid_recipient(setup, invalid):
    from is_this_us.bot.app import TelegramNotifier

    flows, orch, _, _ = setup()
    out = await orch.observe_installation(4, StaticSource())
    bot = RecordingBot()
    notifier = TelegramNotifier(bot)
    notifier.flows = flows
    chat = 4
    if invalid == "stranger":
        chat = 5
    elif invalid == "forgotten":
        flows.storage.forget(4)
    else:
        flows.storage._exec("UPDATE interactions SET expires_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", out.interaction_id))
    assert (await notifier.deliver(chat, out)).startswith("failed:")
    assert not bot.calls


async def test_caption_escapes_metadata_without_losing_credit(setup):
    from is_this_us.bot.flows import caption_for
    from is_this_us.dialogue.engine import Texts

    _, orch, _, _ = setup()
    out = await orch.observe_installation(4, StaticSource())
    scene = out.artwork.model_copy(update={
        "artist": 'A & B <artist>', "title": '<script>"Title"</script>',
        "page_url": 'https://example.org/?a="quoted"&b=<tag>',
        "rights": out.artwork.rights.model_copy(update={"source_statement": "Credit & permission <required>"}),
    })
    caption = caption_for(replace(out, artwork=scene), Texts())
    assert '<script>' not in caption
    assert html.escape(scene.title) in caption
    assert 'href="https://example.org/?a=&quot;quoted&quot;&amp;b=&lt;tag&gt;"' in caption
    assert "Credit &amp; permission &lt;required&gt;" in caption
    assert caption.endswith("</i>")


async def test_caption_limit_counts_parsed_text_and_never_truncates(setup):
    from is_this_us.bot.flows import caption_for
    from is_this_us.dialogue.engine import Texts
    from is_this_us.image_message import ImageMessageError

    _, orch, _, _ = setup()
    out = await orch.observe_installation(4, StaticSource())
    # Escapes lengthen HTML but still fit within Telegram's parsed character limit.
    scene = out.artwork.model_copy(update={"title": "&" * 300})
    caption = caption_for(replace(out, artwork=scene), Texts())
    assert len(caption) > 1024 and caption.endswith("</i>")
    scene = scene.model_copy(update={"title": "long title " * 150})
    with pytest.raises(ImageMessageError, match="1024-character"):
        caption_for(replace(out, artwork=scene), Texts())


class RecordingBot:
    def __init__(self, faults=()):
        self.faults = list(faults)
        self.calls = []

    async def send_photo(self, chat_id, photo, **kwargs):
        self.calls.append(("photo", chat_id, kwargs))
        if self.faults:
            fault = self.faults.pop(0)
            if fault == "wait":
                await asyncio.sleep(10)
            elif fault:
                raise fault

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("text", chat_id, text))


@pytest.mark.parametrize("fault", ["rejected", "network", "timeout"])
async def test_primary_image_failure_has_no_text_fallback_or_duplicate(fault):
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
    from is_this_us.bot.app import send_replies
    from is_this_us.bot.flows import Reply

    errors = {"rejected": TelegramBadRequest(method=None, message="bad photo"),
              "network": TelegramNetworkError(method=None, message="connection lost"), "timeout": "wait"}
    bot = RecordingBot([errors[fault]])
    status = await send_replies(bot, 4, [Reply("photo", photo=b"image", text="caption", image_required=True)], timeout_s=0.02)
    assert status.startswith("failed:" if fault == "rejected" else "unknown:")
    assert len(bot.calls) == 1 and bot.calls[0][:2] == ("photo", 4)


@pytest.mark.parametrize("retry_after,timeout_s,expected_calls", [(60, 0.1, 1), (0, 3, 3)])
async def test_rate_limit_retry_is_bounded(retry_after, timeout_s, expected_calls):
    from aiogram.exceptions import TelegramRetryAfter
    from is_this_us.bot.app import send_replies
    from is_this_us.bot.flows import Reply

    bot = RecordingBot([TelegramRetryAfter(method=None, message="limited", retry_after=retry_after)] * 5)
    status = await send_replies(bot, 4, [Reply("photo", photo=b"image", image_required=True)], timeout_s=timeout_s)
    assert status.startswith("failed: rate limited")
    assert len(bot.calls) == expected_calls


@pytest.mark.parametrize("initial", ["success", "rejected", "network"])
async def test_delivery_retry_does_not_recapture_or_replay_motion(setup, initial):
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
    from is_this_us.bot.app import TelegramNotifier

    flows, orch, _, _ = setup()
    errors = {"success": None, "rejected": TelegramBadRequest(method=None, message="bad photo"),
              "network": TelegramNetworkError(method=None, message="connection lost")}
    bot = RecordingBot([errors[initial]])
    notifier = TelegramNotifier(bot)
    notifier.flows = flows
    orch.notifier = notifier
    out = await orch.observe_installation(4, StaticSource())
    calls, captures = list(orch.guard.robot.calls), orch.estimator.calls
    first_status = out.delivery_status
    status = await notifier.deliver(4, out)
    assert orch.guard.robot.calls == calls and orch.estimator.calls == captures
    assert len(bot.calls) == (2 if initial == "rejected" else 1)
    assert status.startswith("unknown:" if initial == "network" else "sent 1/1")
    if initial != "rejected":
        assert status == first_status


async def test_observe_handler_adds_no_intro_message(setup):
    from is_this_us.bot.app import build_router
    from is_this_us.config import Settings

    flows, _, _, _ = setup()
    flows.storage.pair(4, flows.storage.create_pairing_token())
    bot = RecordingBot()
    router = build_router(flows, Settings())
    handler = next(h.callback for h in router.message.handlers if h.callback.__name__ == "observe")
    await handler(SimpleNamespace(bot=bot, chat=SimpleNamespace(id=4)))
    assert not bot.calls  # only the orchestrator's notifier delivers the image
