from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from envoy.domain.events import Outcome
from envoy.domain.offer import Offer
from envoy.domain.session import SessionState, SessionView
from envoy.security.projection import OfferProjection

type CardOutcome = Literal["pending", "approved", "declined", "timeout"]
type DealStatus = Literal["counterpart", "owner", "agreed", "fulfilled", "rejected", "expired"]

CLOSED_STATUS: Mapping[Outcome, DealStatus] = {
    Outcome.fulfilled: "fulfilled",
    Outcome.rejected: "rejected",
    Outcome.expired: "expired",
}


@dataclass(frozen=True)
class IssueRow:
    name: str
    ours: str | None
    theirs: str | None


@dataclass(frozen=True)
class Dashboard:
    revision: int
    round: int
    max_rounds: int
    status: str
    issues: tuple[IssueRow, ...]
    proximity: int | None
    verified: bool | None


@dataclass(frozen=True)
class EscalationCard:
    revision: int
    cause: int
    expires_at: datetime
    issues: tuple[IssueRow, ...]
    outcome: CardOutcome


@dataclass(frozen=True)
class SummaryRow:
    label: str
    status: str
    topic_url: str | None


def deal_status(view: SessionView) -> DealStatus:
    match view.state:
        case SessionState.PENDING_OWNER:
            return "owner"
        case SessionState.AGREED | SessionState.EXECUTING:
            return "agreed"
        case SessionState.CLOSED:
            return CLOSED_STATUS.get(view.outcome or Outcome.rejected, "rejected")
        case _:
            return "counterpart"


def proximity(ours: Offer | None, theirs: Offer | None) -> int | None:
    if ours is None or theirs is None:
        return None
    projection = OfferProjection()
    left = projection.project(ours)
    right = projection.project(theirs)
    common = sorted(left.keys() & right.keys())
    if not common:
        return None
    same = sum(1 for issue in common if left[issue] == right[issue])
    return (200 * same + len(common)) // (2 * len(common))


class WidgetProjection:
    def __init__(self, format_value: Callable[[JsonValue], str], labels: Mapping[str, str]) -> None:
        self._format = format_value
        self._labels = labels
        self._offers = OfferProjection()

    def rows(
        self, issues: Sequence[str], ours: Offer | None, theirs: Offer | None
    ) -> tuple[IssueRow, ...]:
        left = {} if ours is None else self._offers.project(ours)
        right = {} if theirs is None else self._offers.project(theirs)
        return tuple(
            IssueRow(
                self._labels.get(issue, issue),
                None if issue not in left else self._format(left[issue]),
                None if issue not in right else self._format(right[issue]),
            )
            for issue in sorted(set(issues) | left.keys() | right.keys())
        )

    def dashboard(
        self,
        *,
        revision: int,
        view: SessionView,
        max_rounds: int,
        issues: Sequence[str],
        verified: bool | None = None,
    ) -> Dashboard:
        ours = view.agreed or view.last_outgoing
        theirs = view.agreed or view.last_incoming
        return Dashboard(
            revision=revision,
            round=view.round,
            max_rounds=max_rounds,
            status=deal_status(view),
            issues=self.rows(issues, ours, theirs),
            proximity=proximity(ours, theirs),
            verified=verified,
        )

    def escalation(
        self,
        *,
        revision: int,
        cause: int,
        expires_at: datetime,
        offer: Offer | None,
        theirs: Offer | None,
        outcome: CardOutcome,
    ) -> EscalationCard:
        issues = () if offer is None else tuple(offer.values)
        return EscalationCard(
            revision=revision,
            cause=cause,
            expires_at=expires_at,
            issues=self.rows(issues, offer, theirs),
            outcome=outcome,
        )
