from uuid import UUID

import pytest

from envoy.app.bootstrap import build_application, default_registries
from envoy.app.settings import Settings
from envoy.domain.events import JournalEntryType
from envoy.domain.ids import SessionId
from envoy.domain.telegram import SentMessage
from envoy.ports.registry import UnknownImplementation
from envoy.telegram.memory import FakeTransport
from envoy.telegram.ports import MessageSender


def settings(**kwargs):
    return Settings(
        _env_file=None,
        agent_key="11" * 32,
        master_key="22" * 32,
        global_rate=10000,
        per_chat_rate=10000,
        **kwargs,
    )


@pytest.mark.parametrize("store_name", ["memory", "postgres"])
async def test_bootstrap_swaps_journal_store_by_config(
    store_name, pg_url, pg_session_factory
) -> None:
    app = build_application(settings(journal_store=store_name, database_url=pg_url))
    try:
        session = SessionId(UUID(int=1234567))
        entry = await app.record(session, JournalEntryType.session_opened, b"hello", b"commitment")
        entries = await app.reader.read(session)
        app.verifier.verify(entries, {entry.author: app.public_key})
        assert await app.verifier.read_payload(entry, app.vault, app.cipher) == b"hello"
    finally:
        await app.close()


@pytest.mark.parametrize("transport", ["fake", "aiogram"])
async def test_bootstrap_swaps_telegram_transport_by_config(transport, fake_bot_api) -> None:
    app = build_application(
        settings(
            telegram_transport=transport,
            bot_token="123456:TEST",
            telegram_api_base=fake_bot_api.base_url,
        )
    )
    try:
        assert (await app.sender.send_message(42, "hello")).text == "hello"
    finally:
        await app.close()


async def test_unknown_implementation_name_fails_fast_with_known_list() -> None:
    with pytest.raises(UnknownImplementation, match="memory.*postgres"):
        build_application(settings(journal_store="missing"))


async def test_composition_root_wires_ports_with_autospec_mocks(mocker) -> None:
    sender = mocker.create_autospec(MessageSender, instance=True)
    sender.send_message.return_value = SentMessage(1, 42, "hello")
    registries = default_registries()
    registries.senders.register("custom", lambda **kwargs: sender)
    from envoy.telegram.ports import UpdateSource

    source = mocker.create_autospec(UpdateSource, instance=True)
    registries.sources.register("custom", lambda **kwargs: source)
    registries.surfaces.register("custom", lambda sender: FakeTransport())
    app = build_application(settings(telegram_transport="custom"), registries=registries)
    try:
        assert (await app.sender.send_message(42, "hello")).message_id == 1
        sender.send_message.assert_awaited_once_with(
            42, "hello", reply_markup=None, business_connection_id=None, thread_id=None
        )
    finally:
        await app.close()
