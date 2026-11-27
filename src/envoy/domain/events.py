from enum import StrEnum
from typing import Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict

from envoy.domain.canonical import decode, encode
from envoy.domain.ids import MandateId, OwnerId
from envoy.domain.intent import Intent
from envoy.domain.offer import Offer


class JournalEntryType(StrEnum):
    session_opened = "session_opened"
    mandate_attested = "mandate_attested"
    disclosed = "disclosed"
    intent_parsed = "intent_parsed"
    decided = "decided"
    escalated = "escalated"
    owner_resolved = "owner_resolved"
    delivered = "delivered"
    checklist_opened = "checklist_opened"
    commitment_done = "commitment_done"
    closed = "closed"
    peer_message = "peer_message"


type ActionKind = Literal[
    "send_offer", "accept_offer", "reject_session", "ask_clarification", "open_checklist"
]
type EscalationReason = Literal[
    "confirm_action", "reply_window_closed", "translator_failed", "counterpart_unclear"
]
type PartyRole = Literal["owner", "counterpart"]
type VerdictKind = Literal["allow", "escalate", "deny"]
type Resolution = Literal["approved", "declined", "timeout"]


class Outcome(StrEnum):
    fulfilled = "fulfilled"
    rejected = "rejected"
    expired = "expired"


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionOpened(_Event):
    owner_id: OwnerId
    mandate_id: MandateId
    connection_id: str
    counterpart_chat_id: int
    owner_chat_id: int
    topic_id: int | None
    deadline: AwareDatetime


class MandateAttested(_Event):
    mandate_id: MandateId
    version: int
    attestation: Literal["platform", "owner"] = "platform"


class Disclosed(_Event):
    pass


class IntentParsed(_Event):
    cause: int
    intent: Intent


class Decided(_Event):
    cause: int
    kind: ActionKind
    offer: Offer | None = None
    verdict: VerdictKind
    reason: str | None = None
    missing: tuple[str, ...] = ()


class Escalated(_Event):
    cause: int
    reason: EscalationReason
    expires_at: AwareDatetime | None = None


class OwnerResolved(_Event):
    cause: int
    resolution: Resolution


class Delivered(_Event):
    cause: int
    message_id: int


class ChecklistOpened(_Event):
    items: tuple[str, ...]


class CommitmentDone(_Event):
    party: PartyRole
    index: int


class Closed(_Event):
    outcome: Outcome
    reason: str


class PeerMessage(_Event):
    direction: Literal["in", "out"]
    text: str


type SessionEvent = (
    SessionOpened
    | MandateAttested
    | Disclosed
    | IntentParsed
    | Decided
    | Escalated
    | OwnerResolved
    | Delivered
    | ChecklistOpened
    | CommitmentDone
    | Closed
    | PeerMessage
)

EVENT_TYPES: dict[JournalEntryType, type[_Event]] = {
    JournalEntryType.session_opened: SessionOpened,
    JournalEntryType.mandate_attested: MandateAttested,
    JournalEntryType.disclosed: Disclosed,
    JournalEntryType.intent_parsed: IntentParsed,
    JournalEntryType.decided: Decided,
    JournalEntryType.escalated: Escalated,
    JournalEntryType.owner_resolved: OwnerResolved,
    JournalEntryType.delivered: Delivered,
    JournalEntryType.checklist_opened: ChecklistOpened,
    JournalEntryType.commitment_done: CommitmentDone,
    JournalEntryType.closed: Closed,
    JournalEntryType.peer_message: PeerMessage,
}
ENTRY_TYPES: dict[type[_Event], JournalEntryType] = {
    model: entry_type for entry_type, model in EVENT_TYPES.items()
}


def entry_type_of(event: SessionEvent | type[SessionEvent]) -> JournalEntryType:
    model = event if isinstance(event, type) else type(event)
    return ENTRY_TYPES[model]


def encode_event(event: SessionEvent) -> tuple[JournalEntryType, bytes]:
    return entry_type_of(event), encode(event.model_dump(mode="python"))


def decode_event(type: JournalEntryType, payload: bytes) -> SessionEvent:
    return cast(SessionEvent, EVENT_TYPES[type].model_validate(decode(payload)))
