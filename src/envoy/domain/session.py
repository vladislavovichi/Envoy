from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from envoy.domain.events import (
    ActionKind,
    ChecklistOpened,
    Closed,
    CommitmentDone,
    Decided,
    Delivered,
    Disclosed,
    Escalated,
    EscalationReason,
    IntentParsed,
    JournalEntryType,
    MandateAttested,
    Outcome,
    OwnerResolved,
    PartyRole,
    SessionEvent,
    SessionOpened,
    entry_type_of,
)
from envoy.domain.intent import Propose, Unclear
from envoy.domain.offer import Offer


class SessionState(StrEnum):
    INIT = "INIT"
    NEGOTIATING = "NEGOTIATING"
    PENDING_OWNER = "PENDING_OWNER"
    AGREED = "AGREED"
    EXECUTING = "EXECUTING"
    CLOSED = "CLOSED"


TERMINAL = frozenset({SessionState.CLOSED})

TRANSITIONS: Mapping[SessionState, frozenset[JournalEntryType]] = MappingProxyType(
    {
        SessionState.INIT: frozenset({JournalEntryType.session_opened, JournalEntryType.closed}),
        SessionState.NEGOTIATING: frozenset(
            {
                JournalEntryType.mandate_attested,
                JournalEntryType.disclosed,
                JournalEntryType.intent_parsed,
                JournalEntryType.decided,
                JournalEntryType.escalated,
                JournalEntryType.delivered,
                JournalEntryType.closed,
            }
        ),
        SessionState.PENDING_OWNER: frozenset(
            {
                JournalEntryType.owner_resolved,
                JournalEntryType.escalated,
                JournalEntryType.delivered,
                JournalEntryType.closed,
            }
        ),
        SessionState.AGREED: frozenset(
            {
                JournalEntryType.delivered,
                JournalEntryType.escalated,
                JournalEntryType.checklist_opened,
                JournalEntryType.closed,
            }
        ),
        SessionState.EXECUTING: frozenset(
            {
                JournalEntryType.delivered,
                JournalEntryType.escalated,
                JournalEntryType.commitment_done,
                JournalEntryType.closed,
            }
        ),
        SessionState.CLOSED: frozenset(),
    }
)


class IllegalTransition(ValueError):
    def __init__(self, state: SessionState, event_type: JournalEntryType) -> None:
        self.state = state
        self.event_type = event_type
        super().__init__(f"Event {event_type} is illegal in state {state}")


@dataclass(frozen=True)
class PendingEscalation:
    cause: int
    reason: EscalationReason
    expires_at: datetime | None
    kind: ActionKind | None
    offer: Offer | None


@dataclass(frozen=True)
class SessionView:
    state: SessionState = SessionState.INIT
    outcome: Outcome | None = None
    disclosed: bool = False
    round: int = 0
    last_incoming: Offer | None = None
    last_outgoing: Offer | None = None
    pending: PendingEscalation | None = None
    undelivered: tuple[Decided, ...] = ()
    commitments_done: frozenset[tuple[PartyRole, int]] = frozenset()
    clarifications: int = 0
    opened: SessionOpened | None = None
    agreed: Offer | None = None
    checklist: tuple[str, ...] = ()
    last_decision: Decided | None = None
    history: tuple[Offer, ...] = field(default=())


def _illegal(view: SessionView, event: SessionEvent) -> IllegalTransition:
    return IllegalTransition(view.state, entry_type_of(event))


def apply(view: SessionView, event: SessionEvent) -> SessionView:
    if entry_type_of(event) not in TRANSITIONS[view.state]:
        raise _illegal(view, event)
    match event:
        case SessionOpened():
            return replace(view, state=SessionState.NEGOTIATING, opened=event)
        case MandateAttested():
            return view
        case Disclosed():
            return replace(view, disclosed=True)
        case IntentParsed():
            intent = event.intent
            clarifications = view.clarifications + 1 if isinstance(intent, Unclear) else 0
            if isinstance(intent, Propose):
                return replace(
                    view,
                    last_incoming=intent.offer,
                    history=(*view.history, intent.offer),
                    clarifications=clarifications,
                )
            return replace(view, clarifications=clarifications)
        case Decided():
            return _decided(view, event)
        case Escalated():
            return _escalated(view, event)
        case OwnerResolved():
            if view.pending is None or view.pending.cause != event.cause:
                raise _illegal(view, event)
            return replace(view, state=SessionState.NEGOTIATING, pending=None)
        case Delivered():
            for index, decision in enumerate(view.undelivered):
                if decision.cause == event.cause:
                    remaining = view.undelivered[:index] + view.undelivered[index + 1 :]
                    return replace(view, undelivered=remaining)
            raise _illegal(view, event)
        case ChecklistOpened():
            return replace(view, state=SessionState.EXECUTING, checklist=event.items)
        case CommitmentDone():
            return replace(
                view, commitments_done=view.commitments_done | {(event.party, event.index)}
            )
        case Closed():
            if event.outcome is Outcome.fulfilled and view.state is not SessionState.EXECUTING:
                raise _illegal(view, event)
            return replace(view, state=SessionState.CLOSED, outcome=event.outcome, pending=None)


def _decided(view: SessionView, event: Decided) -> SessionView:
    view = replace(view, last_decision=event)
    if event.verdict != "allow":
        return view
    view = replace(view, undelivered=(*view.undelivered, event))
    match event.kind:
        case "send_offer":
            return replace(view, round=view.round + 1, last_outgoing=event.offer)
        case "accept_offer":
            return replace(view, state=SessionState.AGREED, agreed=event.offer)
        case _:
            return view


def _escalated(view: SessionView, event: Escalated) -> SessionView:
    if event.reason != "confirm_action":
        return view
    if view.state is not SessionState.NEGOTIATING:
        raise _illegal(view, event)
    decision = view.last_decision
    matched = decision is not None and decision.cause == event.cause
    return replace(
        view,
        state=SessionState.PENDING_OWNER,
        pending=PendingEscalation(
            cause=event.cause,
            reason=event.reason,
            expires_at=event.expires_at,
            kind=decision.kind if matched and decision is not None else None,
            offer=decision.offer if matched and decision is not None else None,
        ),
    )


def fold(events: Iterable[SessionEvent]) -> SessionView:
    view = SessionView()
    for event in events:
        view = apply(view, event)
    return view


def is_terminal(state: SessionState) -> bool:
    return state in TERMINAL
