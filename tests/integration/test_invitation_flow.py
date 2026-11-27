import re
from datetime import timedelta
from uuid import UUID

import pytest

from envoy.app.agent_ports import InMemoryDelegations, InMemoryProtocolRepository
from envoy.app.invitations import HUMAN, agent_route
from envoy.app.pipeline import AgentWiring, PipelineConfig, Stores, assemble_pipeline
from envoy.crypto.keys import SigningKey
from envoy.domain.ids import AgentId, owner_for_user
from envoy.domain.telegram import (
    BotMessageUpdate,
    GuestMessageUpdate,
    InlineQueryUpdate,
    MessageUpdate,
)
from envoy.llm.translator import IntentWire
from envoy.mandate.commitment import commitment_digest
from envoy.protocol.delegation import Delegation, InMemoryKeyRegistry, sign_delegation
from envoy.protocol.envelope import decode_message
from envoy.protocol.machine import Phase
from tests.support.federation import ManualPeer, Router
from tests.support.harness import COUNTERPART, OWNER_USER, Harness, build_harness, propose
from tests.support.mandates import SLOTS

AGENT_BOT = 501
PEER_BOT = 777
OWNER_KEY = SigningKey.from_bytes(bytes([3]) * 32)


async def started():
    harness = build_harness()
    stores = harness.pipeline.stores
    repo = InMemoryProtocolRepository()
    delegations = InMemoryDelegations()
    agent_key = harness.signing_key
    pipeline = assemble_pipeline(
        PipelineConfig(owner_ids=frozenset({OWNER_USER}), timezone="UTC"),
        Stores(
            journal=stores.journal,
            mandates=stores.mandates,
            sessions=stores.sessions,
            connections=stores.connections,
            ledger=stores.ledger,
            scheduler=stores.scheduler,
            usage=stores.usage,
        ),
        harness.transport,
        harness.transport,
        harness.llm,
        agent_key,
        harness.clock,
        __import__("envoy.ports.rng", fromlist=["SeededRng"]).SeededRng(3),
        agent=AgentWiring(
            repo, InMemoryKeyRegistry(), delegations, agent_key, AGENT_BOT, OWNER_USER
        ),
    )
    harness.pipeline = pipeline
    await harness.connect()
    await harness.activate()
    stored = await stores.mandates.active(owner_for_user(OWNER_USER))
    delegations.put(
        sign_delegation(
            Delegation(
                owner_id=owner_for_user(OWNER_USER),
                owner_key=OWNER_KEY.public().to_bytes(),
                agent_id=AgentId(UUID(int=55)),
                bot_id=AGENT_BOT,
                agent_key=agent_key.public().to_bytes(),
                mandate_id=stored.id,
                version=stored.version,
                commitment=commitment_digest(stored.mandate, stored.salt),
                expires_at=harness.clock.now() + timedelta(days=30),
            ),
            OWNER_KEY,
        )
    )
    await harness.counterpart("привет", IntentWire(kind="unclear"))
    await harness.owner_in_chat("/envoy")
    await harness.counterpart("давай в парке", propose(SLOTS[7].isoformat(), "park"))
    return harness, repo


async def invite(harness: Harness) -> str:
    await harness.owner_in_chat("/invite")
    card = harness.to_counterpart()[-1]
    assert "ИИ-агент" in card
    match = re.search(r"start=([A-Za-z0-9_-]+)", card)
    assert match is not None
    return match.group(1)


async def private(harness: Harness, user: int, text: str) -> None:
    await harness.feed(MessageUpdate(harness.update_id(), user, user, text=text))


def peer_for(harness: Harness, bot_id: int = PEER_BOT, seed: int = 77) -> ManualPeer:
    peer = ManualPeer(Router(harness.clock), bot_id=bot_id, seed=seed)
    return peer


async def deliver(harness: Harness, peer: ManualPeer, text: str) -> None:
    await harness.feed(
        BotMessageUpdate(
            AGENT_BOT, harness.update_id(), peer.bot_id, peer.bot_id, text, harness.clock.now()
        )
    )


def replies(harness: Harness, peer: ManualPeer) -> list:
    return [
        decode_message(record.message.text).envelope
        for record in harness.transport.sent
        if record.message.chat_id == peer.bot_id
    ]


async def test_guest_and_inline_queries_get_one_disclosure_card() -> None:
    harness, _ = await started()
    guest = GuestMessageUpdate(harness.update_id(), "gq1", 42, -100, "@envoy_bot кто ты?")
    await harness.feed(guest)
    await harness.feed(guest)
    assert len(harness.transport.guest_answers) == 1
    query, article = harness.transport.guest_answers[0]
    assert query == "gq1"
    assert "ИИ-агент" in article["input_message_content"]["message_text"]
    assert article["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/envoy_bot"
    await harness.feed(InlineQueryUpdate(harness.update_id(), "iq1", 42, ""))
    assert harness.transport.inline_answers[0][1][0]["type"] == "article"


async def test_human_to_agent_keeps_sid_round_offer_mandate_and_history() -> None:
    harness, repo = await started()
    record = await harness.session()
    before = await harness.view()
    token = await invite(harness)
    peer = peer_for(harness)
    peer.sid = record.id
    await private(harness, COUNTERPART, f"/authorize {PEER_BOT}")
    await deliver(harness, peer, peer.hello(sid=record.id, token=token))
    session = await repo.get(record.id)
    assert session is not None
    assert session.state.phase is Phase.NEGOTIATING
    assert session.state.round == before.round
    assert session.state.last_sent.hash == before.last_outgoing.hash()
    assert session.mandate_id == record.mandate_id
    assert {offer.hash() for offer in before.history} <= set(session.offers)
    assert [envelope.type for envelope in replies(harness, peer)] == ["hello", "capabilities"]
    assert await harness.pipeline.invitations.repo.route(record.id) == agent_route(PEER_BOT)
    sent = len(harness.to_counterpart())
    await harness.counterpart("давай в кафе", propose(SLOTS[2].isoformat(), "cafe"))
    assert len(harness.to_counterpart()) == sent
    peer.seq = 1
    await deliver(harness, peer, peer.capabilities(ack=2))
    previous = before.last_outgoing.hash()
    offer = before.history[-1]
    moved = offer.model_copy(update={"values": {**offer.values, "place": "cafe"}})
    await deliver(harness, peer, peer.propose(moved, before.round + 1, previous=previous))
    assert replies(harness, peer)[-1].type in ("counter", "accept")
    assert (await repo.get(record.id)).peer_seq == 3
    events = await harness.events()
    assert events[0].connection_id == "conn"


@pytest.mark.parametrize("case", ["wrong_user", "unauthorized", "expired", "reused", "no_token"])
async def test_invalid_bindings_do_not_change_route(case) -> None:
    harness, repo = await started()
    record = await harness.session()
    token = await invite(harness)
    peer = peer_for(harness)
    if case != "unauthorized":
        await private(harness, COUNTERPART, f"/authorize {PEER_BOT}")
    if case == "wrong_user":
        await private(harness, 55, f"/start {token}")
        assert "wrong_user" in harness.transport.sent[-1].message.text
    if case == "expired":
        harness.clock.advance(record.deadline - harness.clock.now())
    if case == "reused":
        await private(harness, COUNTERPART, f"/start {token}")
        assert await harness.pipeline.invitations.repo.route(record.id) == HUMAN
    hello = peer.hello(sid=record.id, token=None if case == "no_token" else token)
    await deliver(harness, peer, hello)
    if case == "wrong_user":
        assert (await repo.get(record.id)).peer_bot_id == PEER_BOT
        return
    assert await repo.get(record.id) is None
    expected = {
        "unauthorized": "agent_not_authorized",
        "expired": "token_expired",
        "reused": "token_used",
        "no_token": "session_exists",
    }[case]
    reasons = [envelope.body["reason"] for envelope in replies(harness, peer)]
    assert reasons == [expected]
    route = await harness.pipeline.invitations.repo.route(record.id)
    assert route in (None, HUMAN)


async def test_second_and_late_hello_after_binding_do_not_rebind() -> None:
    harness, repo = await started()
    record = await harness.session()
    token = await invite(harness)
    await private(harness, COUNTERPART, f"/authorize {PEER_BOT}")
    await private(harness, COUNTERPART, "/authorize 778")
    peer = peer_for(harness)
    rival = peer_for(harness, bot_id=778, seed=78)
    await deliver(harness, peer, peer.hello(sid=record.id, token=token))
    await deliver(harness, rival, rival.hello(sid=record.id, token=token))
    session = await repo.get(record.id)
    assert session.peer_bot_id == PEER_BOT
    assert await harness.pipeline.invitations.repo.route(record.id) == agent_route(PEER_BOT)
    assert replies(harness, rival) == []


async def test_silent_agent_switches_back_to_human_before_readiness() -> None:
    harness, repo = await started()
    record = await harness.session()
    token = await invite(harness)
    await private(harness, COUNTERPART, f"/authorize {PEER_BOT}")
    peer = peer_for(harness)
    peer.sid = record.id
    await deliver(harness, peer, peer.hello(sid=record.id, token=token))
    harness.clock.advance(timedelta(seconds=61))
    await harness.pipeline.agent_flow.run_due(harness.clock.now())
    session = await repo.get(record.id)
    assert session.closed
    assert session.state.outcome == "switched"
    assert await harness.pipeline.invitations.repo.route(record.id) == HUMAN
    assert harness.to_counterpart()[-1].startswith("Агент не ответил")
    sent = len(harness.to_counterpart())
    await harness.counterpart("давай в кафе в 11", propose(SLOTS[2].isoformat(), "cafe"))
    assert len(harness.to_counterpart()) == sent + 1
    peer.seq = 1
    await deliver(harness, peer, peer.capabilities(ack=2))
    assert (await repo.get(record.id)).closed
