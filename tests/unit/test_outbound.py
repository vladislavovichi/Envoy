import asyncio
import time

import pytest

from envoy.domain.telegram import SentMessage
from envoy.ports.clock import SystemClock
from envoy.telegram.outbound import OutboundQueue, RetryAfter
from envoy.telegram.ports import MessageSender


@pytest.fixture
def sender(mocker):
    result = mocker.create_autospec(MessageSender, instance=True)
    result.send_message.side_effect = lambda chat_id, text, **kw: SentMessage(1, chat_id, text)
    return result


def queue_for(sender, **kwargs):
    return OutboundQueue(
        sender,
        global_rate=kwargs.get("global_rate", 10000),
        per_chat_rate=kwargs.get("per_chat_rate", 10000),
        max_retry_after=kwargs.get("max_retry_after", 0.01),
        clock=SystemClock(),
    )


async def test_messages_to_one_chat_keep_order(sender) -> None:
    queue = queue_for(sender)
    result = await asyncio.gather(*(queue.submit(1, str(index)) for index in range(10)))
    await queue.close()
    assert [message.text for message in result] == [str(index) for index in range(10)]
    assert [call.args[1] for call in sender.send_message.await_args_list] == [
        str(i) for i in range(10)
    ]


@pytest.mark.parametrize("limit", ["global_rate", "per_chat_rate"])
async def test_rate_not_exceeded(sender, limit: str) -> None:
    queue = queue_for(sender, **{limit: 50})
    started = time.monotonic()
    await asyncio.gather(*(queue.submit(1, str(i)) for i in range(4)))
    await queue.close()
    assert time.monotonic() - started >= 0.055


@pytest.mark.parametrize("seconds", [0, -100, 1e12, 0.005])
async def test_retry_after_is_bounded_then_succeeds(sender, seconds: float) -> None:
    sender.send_message.side_effect = [RetryAfter(seconds), SentMessage(2, 1, "hi")]
    queue = queue_for(sender)
    started = time.monotonic()
    message = await asyncio.wait_for(queue.submit(1, "hi"), timeout=1)
    elapsed = time.monotonic() - started
    await queue.close()
    assert message.message_id == 2
    assert sender.send_message.await_count == 2
    assert 0.001 <= elapsed < 1
    if seconds > 0:
        assert elapsed >= min(seconds, 0.01) * 0.9


async def test_gives_up_after_five_attempts_and_raises(sender) -> None:
    sender.send_message.side_effect = RetryAfter(0)
    queue = queue_for(sender)
    with pytest.raises(RetryAfter):
        await queue.submit(1, "hi")
    await queue.close()
    assert sender.send_message.await_count == 5


async def test_sender_called_once_per_message(sender) -> None:
    queue = queue_for(sender)
    await queue.submit(1, "hi")
    await queue.close()
    sender.send_message.assert_awaited_once_with(1, "hi", reply_markup=None)
    with pytest.raises(RuntimeError, match="closed"):
        await queue.submit(1, "late")


async def test_one_chat_retry_does_not_block_other_chat(sender) -> None:
    first = True

    async def send(chat_id, text, **kwargs):
        nonlocal first
        if chat_id == 1 and first:
            first = False
            raise RetryAfter(0.05)
        return SentMessage(1, chat_id, text)

    sender.send_message.side_effect = send
    queue = queue_for(sender, max_retry_after=0.1)
    slow = asyncio.create_task(queue.submit(1, "slow"))
    fast = await queue.submit(2, "fast")
    assert fast.chat_id == 2
    assert not slow.done()
    await slow
    await queue.close()


async def test_per_chat_rate_holds_when_global_limiter_is_congested(sender) -> None:
    sent = []

    async def send(chat_id, text, **kwargs):
        sent.append((chat_id, time.monotonic()))
        return SentMessage(1, chat_id, text)

    sender.send_message.side_effect = send
    queue = queue_for(sender, global_rate=50, per_chat_rate=20)
    await asyncio.gather(*(queue.submit(chat, "hi") for chat in [1, 2, 3, 4, 5, 6, 99, 99]))
    await queue.close()
    times = [when for chat, when in sent if chat == 99]
    assert times[1] - times[0] >= 0.045
    assert all(right[1] - left[1] >= 0.018 for left, right in zip(sent, sent[1:], strict=False))
