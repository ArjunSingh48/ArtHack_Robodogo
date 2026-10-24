from __future__ import annotations

from pathlib import Path

import pytest

from is_this_us.artworks.repository import YamlArtworkRepository
from is_this_us.motion.guard import MotionGuard
from is_this_us.notifier import ConsoleNotifier
from is_this_us.orchestrator import InteractionOrchestrator
from is_this_us.robot.mock import MockRobotAdapter
from is_this_us.speech.synthesizer import AudioCache, MockSynthesizer
from is_this_us.storage import Storage
from is_this_us.vision.synthetic import synthetic_person

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "artworks"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo() -> YamlArtworkRepository:
    """The real curated archive (24 museum artworks)."""
    return YamlArtworkRepository(DATA)


@pytest.fixture
def mock_repo() -> YamlArtworkRepository:
    """Clearly fake fixtures (is_fixture: true) for deterministic unit tests."""
    return YamlArtworkRepository(FIXTURES, filename="mock_artworks.yaml")


@pytest.fixture
def storage() -> Storage:
    s = Storage(":memory:", pairing_ttl_min=10, session_ttl_min=30)
    yield s
    s.close()


class FakeEstimator:
    """Returns a synthetic skeleton instead of running MediaPipe."""

    def __init__(self, posture: str = "sitting", gesture: str | None = None, people: int = 1):
        self.posture, self.gesture, self.people = posture, gesture, people
        self.calls = 0

    def estimate(self, rgb):
        self.calls += 1
        return [synthetic_person(self.posture, self.gesture) for _ in range(self.people)]


@pytest.fixture
def make_orchestrator(repo, storage, tmp_path):
    def _make(*, estimator=None, robot=None, guard_kwargs=None, synth=None, seed=3, notifier=None):
        robot = robot or MockRobotAdapter(time_scale=0.0)
        robot.connect()
        guard = MotionGuard(robot, hardware_gates_open=False, cooldown_s=0, timeout_s=5, **(guard_kwargs or {}))
        return InteractionOrchestrator(
            repo=repo, estimator=estimator or FakeEstimator(), storage=storage, guard=guard,
            notifier=notifier or ConsoleNotifier(),
            synthesizer=synth or MockSynthesizer(AudioCache(tmp_path / "audio")), seed=seed,
        )
    return _make
