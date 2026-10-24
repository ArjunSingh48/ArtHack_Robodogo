"""Complete mocked interactions through the orchestrator and the Telegram flows."""

from __future__ import annotations

import pytest

from is_this_us.bot.flows import CB_ANOTHER, CB_KEEP, CB_YES, VisitorFlows
from is_this_us.domain import ExecutionStatus, RobotBehavior
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

    async def deliver(self, chat_id, outcome):
        replies = self.flows.replies_for_outcome(chat_id, outcome)
        self.sent.append((chat_id, replies))
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
    assert kinds == ["text", "photo", "voice"]
    caption = sent[1].text
    assert "Is this us?" in caption
    assert "The Metropolitan Museum of Art" in caption or "Cleveland Museum of Art" in caption
    assert synth.requests and all("Is this us?" in t for t in synth.requests)
    buttons = [d for row in sent[1].buttons for _, d in row]
    assert {b.split(":")[0] for b in buttons} == {CB_YES, CB_ANOTHER, CB_KEEP}

    iid = buttons[0].split(":")[1]
    row = storage.get_interaction(iid, chat)
    assert row.origin == "installation" and row.artwork_id
    assert set(row.observation) <= {"posture", "posture_confidence", "gestures", "proximity",
                                    "people_detected", "landmark_confidence"}

    # ♡ yes
    yes = await flows.callback(chat, f"{CB_YES}:{iid}")
    assert "close" in yes[0].text
    # ✧ keep → memory card
    keep = await flows.callback(chat, f"{CB_KEEP}:{iid}")
    assert keep[0].kind == "photo" and keep[0].photo[:2] == b"\xff\xd8"
    mem = await flows.memory(chat)
    assert len(mem) == 2 and mem[1].kind == "photo"
    # ↻ another → a different painting
    first_art = row.artwork_id
    assert await flows.callback(chat, f"{CB_ANOTHER}:{iid}") == []
    second = storage.get_interaction(iid, chat)
    assert second.shown_ids[0] == first_art and len(second.shown_ids) == 2
    assert second.shown_ids[1] != first_art


async def test_state_machine_trace(make_orchestrator):
    from is_this_us.notifier import ConsoleNotifier

    orch = make_orchestrator(estimator=FakeEstimator("sitting"), notifier=ConsoleNotifier())
    orch.storage.touch_visitor(1)
    out = await orch.observe_installation(1, StaticSource())
    assert out.trace == [State.IDLE, State.OBSERVING, State.INTERPRETING, State.MATCHING, State.PLANNING,
                         State.MOVING, State.NARRATING, State.DELIVERING, State.AWAITING_RESPONSE, State.IDLE]
    assert out.result.status is ExecutionStatus.SIMULATED
    assert out.delivery_status == "printed"


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
