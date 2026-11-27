import asyncio
from datetime import timedelta
from uuid import uuid7

import pytest

from envoy.app.invitations import (
    HUMAN,
    InMemoryInvitationRepository,
    Invitation,
    InvitationRejected,
    agent_route,
    token_hash,
)
from envoy.domain.ids import SessionId
from envoy.domain.telegram import GuestMessageUpdate, InlineQueryUpdate
from envoy.persistence.invitation_repo import PgInvitationRepository
from envoy.telegram.adapter import AiogramAdapter
from tests.support.sessions import T0


@pytest.fixture
async def adapter(fake_bot_api):
    transport = AiogramAdapter("123456:TEST", fake_bot_api.base_url)
    try:
        yield transport
    finally:
        await transport.close()


async def test_guest_and_inline_updates_map_and_answer_once(adapter, fake_bot_api) -> None:
    guest = fake_bot_api.inject_guest_message(42, -100, "@envoy_bot привет")
    inline = fake_bot_api.inject_inline_query(42, "встреча")
    updates = adapter.updates()
    first = await asyncio.wait_for(anext(updates), 2)
    second = await asyncio.wait_for(anext(updates), 2)
    await updates.aclose()
    assert isinstance(first, GuestMessageUpdate)
    assert (first.guest_query_id, first.user_id, first.chat_id) == (guest, 42, -100)
    assert isinstance(second, InlineQueryUpdate)
    assert (second.query_id, second.query) == (inline, "встреча")
    article = {
        "type": "article",
        "id": "1",
        "title": "Envoy",
        "input_message_content": {"message_text": "ИИ-агент"},
    }
    await adapter.answer_guest(guest, article)
    assert fake_bot_api.guest_answered[guest]["type"] == "article"
    with pytest.raises(Exception):  # noqa: B017, PT011
        await adapter.answer_guest(guest, article)
    await adapter.answer_inline(inline, [article])
    call = fake_bot_api.inline_answers[-1]
    assert call["inline_query_id"] == inline
    assert call["results"][0]["input_message_content"]["message_text"] == "ИИ-агент"


@pytest.fixture(params=["memory", "postgres"])
def repo(request):
    if request.param == "memory":
        return InMemoryInvitationRepository()
    return PgInvitationRepository(request.getfixturevalue("pg_session_factory"))


async def test_claim_is_atomic_single_use_and_bound(repo) -> None:
    sid = SessionId(uuid7())
    hash = token_hash("t" * 40)
    await repo.issue(Invitation(hash, sid, 2, T0 + timedelta(hours=1)))
    with pytest.raises(InvitationRejected, match="wrong_user"):
        await repo.claim(hash, T0, HUMAN, 3)
    results = await asyncio.gather(
        repo.claim(hash, T0, agent_route(777), 2),
        repo.claim(hash, T0, agent_route(778), 2),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InvitationRejected) for result in results) == 1
    route = await repo.route(sid)
    assert route in (agent_route(777), agent_route(778))
    assert not await repo.switch(sid, agent_route(1), HUMAN)
    assert await repo.switch(sid, route, HUMAN)
    assert await repo.route(sid) == HUMAN
    with pytest.raises(InvitationRejected, match="unknown_token"):
        await repo.claim(token_hash("other"), T0, HUMAN, None)
    expired = token_hash("e" * 40)
    await repo.issue(Invitation(expired, sid, None, T0))
    with pytest.raises(InvitationRejected, match="token_expired"):
        await repo.claim(expired, T0, HUMAN, None)
    assert not await repo.authorized(2, 777)
    await repo.authorize(2, 777)
    await repo.authorize(2, 777)
    assert await repo.authorized(2, 777)
