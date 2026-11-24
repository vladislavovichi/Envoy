from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from envoy.domain.events import PartyRole
from envoy.domain.offer import Offer
from envoy.domain.session import SessionView


@dataclass(frozen=True)
class Commitment:
    party: PartyRole
    text: str


def plan_commitments(
    offer: Offer, template: Literal["meeting"], timezone_label: str = ""
) -> tuple[Commitment, ...]:
    if template != "meeting":
        raise ValueError(f"Unsupported template {template}")
    time = offer.values["time"]
    place = offer.values["place"]
    when = time.strftime("%d.%m.%Y %H:%M") if isinstance(time, datetime) else str(time)
    suffix = f" {timezone_label}" if timezone_label else ""
    return (
        Commitment("owner", f"Владелец: прийти в «{place}» {when}{suffix}"),
        Commitment("counterpart", f"Собеседник: прийти в «{place}» {when}{suffix}"),
    )


def is_complete(view: SessionView, plan: Sequence[Commitment]) -> bool:
    return bool(plan) and all(
        (commitment.party, index) in view.commitments_done for index, commitment in enumerate(plan)
    )


def meeting_time(offer: Offer) -> datetime | None:
    time = offer.values.get("time")
    return time if isinstance(time, datetime) else None
