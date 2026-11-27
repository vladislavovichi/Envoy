from datetime import UTC, datetime, timedelta
from uuid import UUID

from envoy.domain.events import (
    ChecklistOpened,
    Closed,
    CommitmentDone,
    Decided,
    Delivered,
    Disclosed,
    Escalated,
    IntentParsed,
    MandateAttested,
    Outcome,
    OwnerResolved,
    SessionOpened,
)
from envoy.domain.ids import MandateId, OwnerId
from envoy.domain.intent import Propose
from envoy.domain.offer import Offer

T0 = datetime(2026, 12, 1, 12, tzinfo=UTC)
OFFER = Offer(values={"time": T0, "place": "office"})
OTHER = Offer(values={"time": T0 + timedelta(hours=1), "place": "cafe"})


def opened() -> SessionOpened:
    return SessionOpened(
        owner_id=OwnerId(UUID(int=1)),
        mandate_id=MandateId(UUID(int=2)),
        connection_id="conn",
        counterpart_chat_id=10,
        owner_chat_id=20,
        topic_id=30,
        deadline=T0 + timedelta(days=1),
    )


def happy_path() -> list:
    return [
        opened(),
        MandateAttested(mandate_id=MandateId(UUID(int=2)), version=1),
        Disclosed(),
        Decided(cause=1, kind="send_offer", offer=OFFER, verdict="allow"),
        Delivered(cause=1, message_id=100),
        IntentParsed(cause=2, intent=Propose(offer=OTHER)),
        Decided(cause=2, kind="accept_offer", offer=OTHER, verdict="escalate"),
        Escalated(cause=2, reason="confirm_action", expires_at=T0 + timedelta(hours=1)),
        OwnerResolved(cause=2, resolution="approved"),
        Decided(cause=3, kind="accept_offer", offer=OTHER, verdict="allow"),
        Delivered(cause=3, message_id=101),
        ChecklistOpened(items=("owner: arrive", "counterpart: arrive")),
        CommitmentDone(party="owner", index=0),
        CommitmentDone(party="counterpart", index=1),
        Closed(outcome=Outcome.fulfilled, reason="all_commitments_done"),
    ]
