import json
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from envoy.protocol.machine import (
    LocalAccept,
    LocalOffer,
    LocalReady,
    NegotiationState,
    Opened,
    PeerAccept,
    PeerDecision,
    PeerOffer,
    Phase,
    TimerFired,
    transition,
    turn_priority,
    wins_tie,
)
from tests.support.protocol_sim import SIDES, Network, offer

TRACES = sorted(Path("formal/traces").glob("*.json"))


def negotiating(role="coordinator", me=b"a", peer=b"b") -> NegotiationState:
    return transition(NegotiationState(role=role, me=me, peer=peer), Opened()).state


def happy(net: Network) -> None:
    net.local("A", "offer", "a1")
    net.deliver(0)
    net.local("B", "accept")
    net.deliver(0)
    net.local("A", "ready")
    net.local("B", "ready")


def test_happy_path_commits_once_on_both_sides() -> None:
    net = Network.start()
    happy(net)
    net.settle()
    net.check_safety()
    assert net.states["A"].phase is Phase.EXECUTING
    assert net.states["B"].phase is Phase.EXECUTING
    assert net.checklists == {"A": [offer("a1")], "B": [offer("a1")]}


def test_only_last_unanswered_offer_is_acceptable() -> None:
    state = negotiating()
    state = transition(state, PeerOffer(offer("b1"), None, 1)).state
    state = transition(state, LocalOffer(offer("a2"))).state
    stale = transition(state, LocalAccept(offer("b1")))
    assert stale.rejected == "not_last_unanswered"
    late_peer = transition(state, PeerAccept(offer("x")))
    assert late_peer.rejected == "not_last_unanswered"
    assert transition(state, LocalOffer(offer("a3"))).rejected == "awaiting_reply"


def test_accept_and_counter_on_same_hash_are_exclusive() -> None:
    state = transition(negotiating(), PeerOffer(offer("b1"), None, 1)).state
    accepted = transition(state, LocalAccept(offer("b1"))).state
    assert transition(accepted, LocalOffer(offer("a2"))).rejected == "already_agreed"
    countered = transition(state, LocalOffer(offer("a2"))).state
    assert transition(countered, LocalAccept(offer("b1"))).rejected == "not_last_unanswered"
    assert transition(countered, PeerOffer(offer("b1"), None, 1)).rejected == "duplicate_offer"


def test_simultaneous_offers_resolve_by_smaller_agent_hash() -> None:
    ids = sorted([b"agent-a", b"agent-b"], key=turn_priority)
    winner, loser = ids
    net = Network.start(a_id=winner, b_id=loser)
    assert wins_tie(net.states["A"])
    assert not wins_tie(net.states["B"])
    net.local("A", "offer", "a1")
    net.local("B", "offer", "b1")
    net.deliver(0)
    net.deliver(0)
    assert net.states["A"].last_sent.hash == offer("a1")
    assert net.states["A"].last_received is None
    assert net.states["B"].last_sent is None
    assert net.states["B"].last_received.hash == offer("a1")
    net.local("B", "accept")
    net.deliver_all()
    assert net.states["A"].agreed == net.states["B"].agreed == offer("a1")


def test_revoke_before_peer_ready_rolls_back_and_after_commit_disputes() -> None:
    net = Network.start()
    net.local("A", "offer", "a1")
    net.deliver(0)
    net.local("B", "accept")
    net.deliver(0)
    net.local("B", "revoke")
    net.settle()
    assert net.states["B"].phase is Phase.REVOKED
    assert net.states["A"].phase is Phase.ABORTED
    assert net.checklists == {"A": [], "B": []}
    committed = Network.start()
    happy(committed)
    committed.settle()
    committed.local("B", "revoke")
    committed.settle()
    assert committed.states["B"].phase is Phase.DISPUTED
    assert committed.states["A"].phase is Phase.DISPUTED
    assert committed.checklists["A"] == committed.checklists["B"] == [offer("a1")]


def test_ready_crash_timeout_then_commit_decision_has_one_effect_per_side() -> None:
    net = Network.start()
    net.local("A", "offer", "a1")
    net.deliver(0)
    net.local("B", "accept")
    net.deliver(0)
    net.local("B", "ready")
    net.deliver(0)
    net.local("A", "ready")
    decision = [
        index for index, (_, event) in enumerate(net.inflight) if isinstance(event, PeerDecision)
    ]
    assert net.states["A"].phase is Phase.EXECUTING
    net.drop(decision[0])
    net.restart("B")
    net.fire("B", "decision")
    assert net.escalations["B"] == ["decision_overdue"]
    net.settle()
    net.fire("B", "decision")
    net.settle()
    assert net.states["B"].phase is Phase.EXECUTING
    assert net.checklists == {"A": [offer("a1")], "B": [offer("a1")]}
    assert net.escalations["B"] == ["decision_overdue"]


def test_ready_timeout_then_abort_decision_has_no_effect() -> None:
    net = Network.start()
    net.local("A", "offer", "a1")
    net.deliver(0)
    net.local("B", "accept")
    net.deliver(0)
    net.local("B", "ready")
    net.inflight.clear()
    net.fire("A", "ready")
    net.settle()
    assert net.states["A"].phase is Phase.ABORTED
    assert net.states["B"].phase is Phase.ABORTED
    assert net.checklists == {"A": [], "B": []}


def test_lost_accept_never_leaves_ready_participant_hanging() -> None:
    net = Network.start(a_id=b"agent-b", b_id=b"agent-a")
    net.local("B", "offer", "b1")
    net.deliver(0)
    net.local("A", "accept")
    net.drop(0)
    net.fire("B", "reply")
    net.settle()
    assert net.states["A"].terminal
    assert net.states["B"].terminal
    net.check_safety()


@pytest.mark.parametrize("trace", TRACES, ids=[trace.stem for trace in TRACES])
def test_shared_traces(trace) -> None:
    spec = json.loads(trace.read_text("utf-8"))
    net = Network.start()
    for step in spec["steps"]:
        match step:
            case ["local", side, action, *label]:
                net.local(side, action, *label)
            case ["deliver", index]:
                net.deliver(index)
            case ["drop", index]:
                net.drop(index)
            case ["duplicate", index]:
                net.duplicate(index)
            case ["fire", side, timer]:
                net.fire(side, timer)
            case ["restart", side]:
                net.restart(side)
            case ["settle"]:
                net.settle()
        net.check_safety()
    for side, phase in spec["expect"].items():
        assert net.states[side].phase is Phase(phase), (trace.stem, side, net.states[side])
    assert {side: len(found) for side, found in net.checklists.items()} == spec["checklists"]


class Faults(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.net = Network.start()

    @rule(
        side=st.sampled_from(SIDES),
        action=st.sampled_from(["offer", "accept", "ready", "revoke", "reject"]),
        label=st.sampled_from(["x", "y", "z"]),
    )
    def local(self, side, action, label) -> None:
        if action == "reject" and self.net.states[side].phase is not Phase.NEGOTIATING:
            return
        self.net.local(side, action, label)

    @rule(data=st.data())
    def deliver(self, data) -> None:
        if self.net.inflight:
            self.net.deliver(data.draw(st.integers(0, len(self.net.inflight) - 1)))

    @rule(data=st.data())
    def drop(self, data) -> None:
        if self.net.inflight:
            self.net.drop(data.draw(st.integers(0, len(self.net.inflight) - 1)))

    @rule(data=st.data())
    def duplicate(self, data) -> None:
        if self.net.inflight and len(self.net.inflight) < 30:
            self.net.duplicate(data.draw(st.integers(0, len(self.net.inflight) - 1)))

    @rule(side=st.sampled_from(SIDES), timer=st.sampled_from(["reply", "ready", "decision"]))
    def fire(self, side, timer) -> None:
        self.net.fire(side, timer)

    @rule(side=st.sampled_from(SIDES))
    def restart(self, side) -> None:
        self.net.restart(side)

    @invariant()
    def safe(self) -> None:
        self.net.check_safety()

    def teardown(self) -> None:
        net = self.net
        for side in SIDES:
            net.restart(side)
        net.settle(2000)
        net.check_safety()
        a, b = net.states["A"], net.states["B"]
        stuck = [
            side
            for side in SIDES
            if not net.states[side].terminal and net.states[side].phase is not Phase.NEGOTIATING
        ]
        assert not stuck, (a, b)
        if Phase.EXECUTING in (a.phase, b.phase):
            assert {a.phase, b.phase} <= {Phase.EXECUTING, Phase.DISPUTED}
        assert not any(net.timers[side] for side in SIDES if net.states[side].terminal)


TestFaults = Faults.TestCase
TestFaults.settings = settings(max_examples=400, stateful_step_count=40, deadline=None)


def test_timer_events_in_handshake_are_ignored() -> None:
    state = NegotiationState(role="participant", me=b"a", peer=b"b")
    assert transition(state, TimerFired("reply")).rejected == "handshake_pending"
    assert transition(state, LocalReady(offer("x"))).rejected == "handshake_pending"
