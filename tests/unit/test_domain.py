from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import ValidationError

from envoy.domain.offer import Offer


class OfferFactory(ModelFactory[Offer]):
    __model__ = Offer


def test_offer_hash_ignores_key_order() -> None:
    assert Offer(values={"a": 1, "b": "x"}).hash() == Offer(values={"b": "x", "a": 1}).hash()


def test_offer_hash_differs_on_value_change() -> None:
    assert Offer(values={"a": 1}).hash() != Offer(values={"a": 2}).hash()


def test_offer_is_immutable() -> None:
    source = {"a": 1}
    offer = OfferFactory.build(values=source)
    source["a"] = 2
    assert offer.values["a"] == 1
    with pytest.raises(ValidationError):
        offer.values = {}
    with pytest.raises(TypeError):
        offer.values["a"] = 2


@pytest.mark.parametrize("value", [1.5, True, float("nan"), float("inf")])
def test_offer_rejects_float(value: object) -> None:
    with pytest.raises(ValidationError):
        Offer(values={"a": value})


def test_offer_supports_all_issue_values() -> None:
    offer = Offer(
        values={
            "money": Decimal("1.25"),
            "date": datetime(2026, 1, 1, tzinfo=UTC),
            "duration": timedelta(seconds=10),
            "place": "here",
            "count": 2,
        }
    )
    assert len(offer.hash()) == 32
    assert offer.canonical_bytes() == offer.canonical_bytes()


def test_offer_rejects_naive_datetime_before_hashing() -> None:
    with pytest.raises(ValidationError):
        Offer(values={"time": datetime(2026, 1, 1)})
