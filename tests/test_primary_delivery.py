"""Delivery ownership while a primary image send is still in flight."""

import asyncio

import pytest

from is_this_us.bot.app import TelegramNotifier
from is_this_us.bot.flows import VisitorFlows
from is_this_us.domain import HumanObservation


class PendingImageBot:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def send_photo(self, chat_id, photo, **kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()


@pytest.fixture
async def pending_delivery(make_orchestrator, storage):
    orch = make_orchestrator()
    outcome = await orch.observe_given(
        42, HumanObservation(posture="sitting", posture_confidence=0.9), origin="installation",
    )
    bot = PendingImageBot()
    notifier = TelegramNotifier(bot)
    notifier.flows = VisitorFlows(orchestrator=orch, storage=storage, repo=orch.repo, operator_ids=[])
    return notifier, bot, outcome


async def test_concurrent_delivery_attempts_send_one_image(pending_delivery):
    notifier, bot, outcome = pending_delivery
    first = asyncio.create_task(notifier.deliver(42, outcome))
    try:
        await asyncio.wait_for(bot.started.wait(), timeout=1)
        second_status = await notifier.deliver(42, outcome)
        assert second_status == "sending"
        assert bot.calls == 1
    finally:
        bot.release.set()
        first_status = await first
    assert first_status == outcome.delivery_status == "sent 1/1 message(s)"
    assert await notifier.deliver(42, outcome) == first_status
    assert bot.calls == 1


async def test_cancelled_send_is_unknown_and_not_replayed(pending_delivery):
    notifier, bot, outcome = pending_delivery
    delivery = asyncio.create_task(notifier.deliver(42, outcome))
    try:
        await asyncio.wait_for(bot.started.wait(), timeout=1)
    finally:
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
    assert outcome.delivery_status.startswith("unknown:")
    assert await notifier.deliver(42, outcome) == outcome.delivery_status
    assert bot.calls == 1
