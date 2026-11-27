from uuid import uuid7

import pytest

from envoy.domain.events import ChecklistOpened, PeerMessage
from envoy.domain.offer import Offer
from envoy.domain.session import SessionState
from envoy.journal.verifier import JournalVerifier
from envoy.protocol.envelope import decode_message
from envoy.protocol.machine import Phase
from tests.support.federation import (
    ManualPeer,
    SimulatedCrash,
    message_type,
    restart,
    two_operators,
)
from tests.support.mandates import SLOTS

PARK = Offer(values={"time": SLOTS[7], "place": "park"})


async def negotiate(router, a, b):
    sid = await a.flow.open(b.bot_id, a.owner_id)
    await router.settle()
    return sid


async def assert_agreed(a, b, sid) -> None:
    left, right = await a.repo.get(sid), await b.repo.get(sid)
    assert (left.phase, right.phase) == (Phase.EXECUTING, Phase.EXECUTING), (
        left.state,
        right.state,
    )
    assert left.state.agreed == right.state.agreed
    for operator in (a, b):
        events = await operator.journal.events(sid)
        assert len([event for event in events if isinstance(event, ChecklistOpened)]) == 1


def reasons(peer: ManualPeer) -> list[str]:
    return [
        decode_message(text).envelope.body["reason"]
        for text in peer.inbox()
        if message_type(text) == "reject"
    ]


async def test_two_agents_agree_one_meeting_and_open_one_checklist_each() -> None:
    router, a, b = await two_operators()
    sid = await negotiate(router, a, b)
    await assert_agreed(a, b, sid)
    for operator, peer in ((a, b), (b, a)):
        events = await operator.journal.events(sid)
        assert (await operator.view(sid)).state is SessionState.EXECUTING
        incoming = [
            event.text
            for event in events
            if isinstance(event, PeerMessage) and event.direction == "in"
        ]
        assert incoming
        assert all(decode_message(text).verify(peer.agent.public()) for text in incoming)
        entries = await operator.journal_store.read(sid)
        JournalVerifier().verify(entries, {entries[0].author: operator.agent.public()})
        notices = [text for text, _ in router.owner_messages[operator.owner_user]]
        assert len([text for text in notices if "закреплена" in text]) == 1


async def test_crash_after_commit_before_send_is_recovered_by_retransmission() -> None:
    router, a, b = await two_operators()
    a.sender.crash_when = lambda chat, text: message_type(text) == "propose"
    sid = await a.flow.open(b.bot_id, a.owner_id)
    with pytest.raises(SimulatedCrash):
        await router.pump()
    assert any(item.type == "propose" for item in (await a.repo.get(sid)).outbox)
    a = restart(router, a)
    await router.settle()
    await assert_agreed(a, b, sid)


@pytest.mark.parametrize("kind", ["propose", "counter", "accept", "commit", "ack"])
async def test_lost_messages_and_acks_are_recovered(kind) -> None:
    router, a, b = await two_operators()
    router.drop_next(lambda sender, chat, text: message_type(text) == kind)
    sid = await negotiate(router, a, b)
    await assert_agreed(a, b, sid)


async def test_duplicated_delivery_has_single_effect() -> None:
    router, a, b = await two_operators()
    original = router.route

    def twice(from_bot, chat_id, text, markup):
        original(from_bot, chat_id, text, markup)
        if chat_id in router.operators:
            original(from_bot, chat_id, text, markup)

    router.route = twice
    sid = await negotiate(router, a, b)
    await assert_agreed(a, b, sid)


async def test_restart_mid_commit_restores_cursors_timers_and_outbox() -> None:
    router, a, b = await two_operators()
    router.drop_next(
        lambda sender, chat, text: sender == a.bot_id
        and message_type(text) == "commit"
        and decode_message(text).envelope.body["stage"] == "decision"
    )
    sid = await a.flow.open(b.bot_id, a.owner_id)
    for _ in range(60):
        await router.tick()
        if (await b.repo.get(sid)).phase is Phase.COMMITTING and not router.drops:
            break
    stored = await b.repo.get(sid)
    assert stored.phase is Phase.COMMITTING
    b = restart(router, b)
    assert (await b.repo.get(sid)).peer_seq == stored.peer_seq
    await router.settle()
    await assert_agreed(a, b, sid)


async def test_updates_with_same_id_for_different_bots_are_independent() -> None:
    router, a, b = await two_operators()
    sid = await negotiate(router, a, b)
    seen = {key for key in a.repo._updates | b.repo._updates if key[1] == 1}
    assert seen == {(a.bot_id, 1), (b.bot_id, 1)}
    await assert_agreed(a, b, sid)


async def handshake_with(router, operator, peer: ManualPeer):
    peer.send(operator.bot_id, peer.hello())
    await router.pump()
    peer.send(operator.bot_id, peer.capabilities(ack=2))
    await router.pump()
    return await operator.repo.get(peer.sid)


async def test_repeated_offer_under_new_seq_is_rejected_as_stalled() -> None:
    router, a, _ = await two_operators()
    peer = ManualPeer(router)
    session = await handshake_with(router, a, peer)
    assert session.phase is Phase.NEGOTIATING
    peer.send(a.bot_id, peer.propose(PARK, 1))
    await router.pump()
    previous = (await a.repo.get(peer.sid)).state.last_sent.hash
    peer.send(a.bot_id, peer.propose(PARK, 3, previous=previous))
    await router.pump()
    assert (await a.repo.get(peer.sid)).phase is Phase.CLOSED
    assert reasons(peer) == ["stalled"]


async def test_rounds_without_progress_are_rejected_as_stalled() -> None:
    router, a, _ = await two_operators()
    peer = ManualPeer(router)
    await handshake_with(router, a, peer)
    previous = None
    for index, place in enumerate(["park", "station", "cafe", "station", "park"]):
        session = await a.repo.get(peer.sid)
        if session.phase is not Phase.NEGOTIATING:
            break
        offer = Offer(values={"time": SLOTS[7 - index % 2], "place": place})
        peer.send(a.bot_id, peer.propose(offer, 2 * index + 1, previous=previous))
        await router.pump()
        session = await a.repo.get(peer.sid)
        previous = None if session.state.last_sent is None else session.state.last_sent.hash
    assert (await a.repo.get(peer.sid)).phase is Phase.CLOSED
    assert reasons(peer) == ["stalled"]


async def test_fourth_concurrent_session_with_same_pair_is_rejected() -> None:
    router, a, _ = await two_operators()
    peer = ManualPeer(router)
    for _ in range(4):
        peer.send(a.bot_id, peer.hello(sid=uuid7()))
        await router.pump()
    assert len(await a.repo.active_with(a.bot_id, peer.bot_id)) == 3
    assert reasons(peer) == ["too_many_sessions"]


async def test_replay_gap_and_stale_messages_do_not_advance() -> None:
    router, a, _ = await two_operators()
    peer = ManualPeer(router)
    session = await handshake_with(router, a, peer)
    gap = peer.propose(PARK, 1, seq=4)
    peer.send(a.bot_id, gap)
    await router.pump()
    assert (await a.repo.get(peer.sid)).peer_seq == session.peer_seq
    peer.send(a.bot_id, peer.propose(PARK, 1, seq=3, ts_offset=-3600, ttl=60))
    await router.pump()
    assert (await a.repo.get(peer.sid)).peer_seq == session.peer_seq
    fresh = peer.propose(PARK, 1, seq=3)
    peer.send(a.bot_id, fresh)
    await router.pump()
    advanced = await a.repo.get(peer.sid)
    assert advanced.peer_seq == 3
    peer.send(a.bot_id, fresh)
    await router.pump()
    assert (await a.repo.get(peer.sid)).state == advanced.state
    peer.send(a.bot_id, gap)
    await router.pump()
    assert (await a.repo.get(peer.sid)).peer_seq == 4


async def test_forged_and_foreign_messages_are_ignored() -> None:
    router, a, _ = await two_operators()
    peer = ManualPeer(router)
    session = await handshake_with(router, a, peer)
    impostor = ManualPeer(router, bot_id=778, seed=78, sid=peer.sid)
    impostor.seq = 2
    impostor.send(a.bot_id, impostor.propose(PARK, 1))
    await router.pump()
    forged = ManualPeer(router, seed=79, sid=peer.sid)
    forged.seq = 2
    forged.send(a.bot_id, forged.propose(PARK, 1))
    await router.pump()
    router.route(peer.bot_id, a.bot_id, "привет, я бот", None)
    router.route(peer.bot_id, a.bot_id, "envoy/1:broken", None)
    await router.pump()
    assert (await a.repo.get(peer.sid)).peer_seq == session.peer_seq


async def test_flood_of_new_sessions_is_rate_limited() -> None:
    router, a, _ = await two_operators()
    peer = ManualPeer(router)
    for _ in range(80):
        peer.send(a.bot_id, peer.hello(sid=uuid7()))
    await router.pump()
    assert len(await a.repo.active_with(a.bot_id, peer.bot_id)) == 3
    assert len(peer.inbox()) <= 60


def test_agent_flow_has_no_language_model_dependency() -> None:
    from pathlib import Path

    import envoy.app.agent_flow as module

    source = Path(module.__file__).read_text("utf-8")
    assert "envoy.llm" not in source
    assert "Translator" not in source


async def test_bootstrap_routes_bot_messages_commands_and_publishes_fingerprint() -> None:
    from datetime import UTC, datetime

    from envoy.domain.telegram import BotMessageUpdate, MessageUpdate
    from envoy.ports.clock import FixedClock
    from tests.integration.test_meeting_e2e import OWNER, build
    from tests.support.federation import Router

    driver = build("fake", agent_protocol=True, agent_bot_id=501)
    try:
        flow = driver.app.pipeline.agent_flow
        assert flow is not None
        assert flow.bot_id == 501
        peer = ManualPeer(Router(FixedClock(datetime.now(UTC))))
        await driver.feed(
            BotMessageUpdate(501, 1, peer.bot_id, peer.bot_id, peer.hello(), datetime.now(UTC))
        )
        rejects = [
            record.message.text
            for record in driver.transport.sent
            if record.message.chat_id == peer.bot_id
        ]
        assert [decode_message(text).envelope.body["reason"] for text in rejects] == [
            "no_delegation"
        ]
        await driver.feed(MessageUpdate(driver.next_id(), OWNER, OWNER, text="/agent 777"))
        assert "Не удалось начать переговоры" in driver.transport.sent[-1].message.text
        await driver.app.runtime._startup[0]()
        assert driver.transport.descriptions[-1].endswith(flow.fingerprint())
    finally:
        await driver.app.close()
