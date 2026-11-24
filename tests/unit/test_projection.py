import json
from datetime import timedelta
from decimal import Decimal

from envoy.domain.offer import Offer
from envoy.mandate.compiler import compile_mandate
from envoy.security.projection import (
    ClarificationProjection,
    OfferProjection,
    OwnerCard,
    OwnerCardProjection,
)
from tests.support.mandates import RESERVATION, SLOTS, WEIGHT_PLACE, WEIGHT_TIME, meeting_mandate

CANARIES = [str(RESERVATION), str(WEIGHT_TIME), str(WEIGHT_PLACE), "0.37", "0.6123", "0.3877"]


def test_offer_projection_has_only_offered_issues() -> None:
    offer = Offer(values={"time": SLOTS[1], "place": "cafe"})
    assert OfferProjection().project(offer) == {
        "place": "cafe",
        "time": SLOTS[1].isoformat(),
    }
    mixed = Offer(values={"price": Decimal("1.50"), "count": 2, "length": timedelta(minutes=5)})
    assert OfferProjection().project(mixed) == {"count": 2, "length": 300, "price": "1.50"}


def test_clarification_projection_lists_only_missing_names() -> None:
    assert ClarificationProjection().project(["time", "place", "time"]) == {
        "missing": ["place", "time"]
    }


def test_projections_never_contain_private_mandate_fields() -> None:
    compiled = compile_mandate(meeting_mandate())
    offer = compiled.grid.offer_at((2, 1))
    outputs = [
        OfferProjection().project(offer),
        ClarificationProjection().project(["time"]),
        OwnerCardProjection().project(OwnerCard(offer, "confirm_action")),
        OwnerCardProjection().project(OwnerCard(None, "translator_failed")),
    ]
    text = json.dumps(outputs)
    for canary in CANARIES:
        assert canary not in text
    for private in ("weights", "reservation", "value_functions", "utility", "minimum_utility"):
        assert private not in text
