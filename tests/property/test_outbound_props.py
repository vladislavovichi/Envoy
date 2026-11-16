import asyncio
from unittest.mock import create_autospec

from hypothesis import given, settings
from hypothesis import strategies as st

from envoy.domain.telegram import SentMessage
from envoy.ports.clock import SystemClock
from envoy.telegram.outbound import OutboundQueue
from envoy.telegram.ports import MessageSender


@settings(max_examples=20)
@given(st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=25))
async def test_order_preserved_for_any_interleaving_of_chats(chats: list[int]) -> None:
    sender = create_autospec(MessageSender, instance=True)
    sender.send_message.side_effect = lambda chat_id, text, **kw: SentMessage(1, chat_id, text)
    queue = OutboundQueue(sender, 100000, 100000, 0.01, SystemClock())
    await asyncio.gather(*(queue.submit(chat, str(index)) for index, chat in enumerate(chats)))
    await queue.close()
    for chat in set(chats):
        assert [
            call.args[1] for call in sender.send_message.await_args_list if call.args[0] == chat
        ] == [str(index) for index, value in enumerate(chats) if value == chat]
