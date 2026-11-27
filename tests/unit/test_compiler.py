from datetime import timedelta
from decimal import Decimal
from itertools import product

import numpy as np
import pytest
from pydantic import ValidationError

from envoy.domain.offer import Offer
from envoy.mandate.compiler import OutsideDomain, compile_mandate
from envoy.mandate.grid import CandidateGrid
from envoy.mandate.schema import Mandate
from tests.support.mandates import PLACES, RESERVATION, SLOTS, meeting_data, meeting_mandate


def purchase(constraints: list[dict], issues: list[dict] | None = None) -> Mandate:
    issue_list = issues or [
        {"id": "price", "kind": "money", "minimum": "100", "maximum": "500", "step": "100"},
        {"id": "delivery", "kind": "enum", "choices": ["pickup", "courier"]},
    ]
    ids = [issue["id"] for issue in issue_list]
    return Mandate.model_validate(
        {
            "template": "purchase",
            "issues": issue_list,
            "utility": {
                "weights": {issue_id: 1 / len(ids) for issue_id in ids},
                "value_functions": {issue_id: {"points": [[0, 0], [500, 1]]} for issue_id in ids},
            },
            "hard_constraints": constraints,
            "reservation": {"minimum_utility": 0.1},
            "strategy": {"kind": "time_dependent"},
            "limits": {"max_rounds": 3},
            "escalation": {},
        }
    )


def feasible_cells(mandate: Mandate) -> set[tuple[int, ...]]:
    compiled = compile_mandate(mandate)
    return {tuple(int(i) for i in index) for index in np.argwhere(compiled.constraints.mask())}


def test_grid_roundtrip_offer_at_index_of() -> None:
    grid = compile_mandate(meeting_mandate()).grid
    for index in product(*(range(size) for size in grid.shape)):
        assert grid.index_of(grid.offer_at(index)) == index


@pytest.mark.parametrize(
    "values",
    [
        {"time": SLOTS[0]},
        {"time": SLOTS[0], "place": "moon"},
        {"time": SLOTS[0] + timedelta(minutes=1), "place": "cafe"},
        {"time": SLOTS[0], "place": "cafe", "extra": 1},
        {"time": "2026-12-01T09:00:00+00:00", "place": "cafe"},
    ],
)
def test_index_of_out_of_domain_is_none(values: dict) -> None:
    grid = compile_mandate(meeting_mandate()).grid
    assert grid.index_of(Offer(values=values)) is None


def test_index_of_accepts_int_for_money_but_not_decimal_for_integer() -> None:
    grid = CandidateGrid(purchase([]).issues)
    assert grid.index_of(Offer(values={"price": 200, "delivery": "pickup"})) == (1, 0)
    integer = CandidateGrid(
        purchase([], [{"id": "n", "kind": "integer", "minimum": 1, "maximum": 3}]).issues
    )
    assert integer.index_of(Offer(values={"n": Decimal(2)})) is None
    assert integer.index_of(Offer(values={"n": 2})) == (1,)


def test_mask_matches_scalar_is_feasible_for_every_cell() -> None:
    compiled = compile_mandate(meeting_mandate())
    mask = compiled.constraints.mask()
    for index in product(*(range(size) for size in compiled.grid.shape)):
        assert mask[index] == compiled.constraints.is_feasible(compiled.grid.offer_at(index))
    assert not mask[:, PLACES.index("station")].any()
    assert not compiled.constraints.is_feasible(Offer(values={"time": SLOTS[0]}))


def test_utility_table_matches_scalar_value() -> None:
    compiled = compile_mandate(meeting_mandate())
    table = compiled.utility.table()
    for index in product(*(range(size) for size in compiled.grid.shape)):
        assert table[index] == pytest.approx(compiled.utility.value(compiled.grid.offer_at(index)))
    assert table[0, 0] == pytest.approx(1.0)
    assert table[-1, -1] == pytest.approx(0.0)
    assert ((table >= 0) & (table <= 1)).all()
    with pytest.raises(OutsideDomain):
        compiled.utility.value(Offer(values={"time": SLOTS[0], "place": "moon"}))


def test_requires_is_implication() -> None:
    mandate = purchase(
        [
            {
                "kind": "requires",
                "condition": {"kind": "cmp", "issue": "delivery", "op": "in", "value": ["courier"]},
                "consequence": {"kind": "cmp", "issue": "price", "op": ">=", "value": "300"},
            }
        ]
    )
    assert feasible_cells(mandate) == {
        (0, 0),
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
        (2, 1),
        (3, 1),
        (4, 1),
    }


def test_not_in_and_between() -> None:
    mandate = purchase(
        [
            {"kind": "cmp", "issue": "price", "op": "between", "value": ["200", "400"]},
            {"kind": "cmp", "issue": "delivery", "op": "not_in", "value": ["pickup"]},
        ]
    )
    assert feasible_cells(mandate) == {(1, 1), (2, 1), (3, 1)}


def test_or_and_not_combinators() -> None:
    mandate = purchase(
        [
            {
                "kind": "or",
                "predicates": [
                    {"kind": "cmp", "issue": "price", "op": "<=", "value": "100"},
                    {
                        "kind": "not",
                        "predicate": {"kind": "cmp", "issue": "price", "op": "<=", "value": 400},
                    },
                ],
            },
            {
                "kind": "and",
                "predicates": [
                    {"kind": "cmp", "issue": "delivery", "op": "in", "value": ["pickup"]}
                ],
            },
        ]
    )
    assert feasible_cells(mandate) == {(0, 0), (4, 0)}


def test_meeting_mandate_compiles_to_expected_shape(snapshot) -> None:
    compiled = compile_mandate(meeting_mandate())
    summary = {
        "shape": compiled.grid.shape,
        "ids": compiled.grid.ids,
        "feasible": int(compiled.constraints.mask().sum()),
        "utility_row0": [round(float(value), 6) for value in compiled.utility.table()[0]],
    }
    assert summary == snapshot


def test_grid_over_one_million_cells_rejected() -> None:
    issues = [
        {"id": key, "kind": "integer", "minimum": 0, "maximum": 100} for key in ("a", "b", "c")
    ]
    with pytest.raises(ValidationError, match="1000000"):
        purchase([], issues)


def test_meeting_template_requires_time_and_place() -> None:
    data = meeting_data()
    data["issues"][1]["kind"] = "enum"
    with pytest.raises(ValidationError, match="Meeting"):
        Mandate.model_validate(data)


def test_invalid_constraint_value_rejected() -> None:
    with pytest.raises(ValidationError):
        purchase([{"kind": "cmp", "issue": "price", "op": "<=", "value": "cheap"}])
    with pytest.raises(ValidationError):
        purchase([{"kind": "cmp", "issue": "price", "op": "between", "value": ["1"]}])


def test_reservation_exposed() -> None:
    compiled = compile_mandate(meeting_mandate())
    assert compiled.reservation == RESERVATION
    assert compiled.limits.max_rounds == 6
    assert compiled.strategy.params["beta"] == 0.5


def test_compiled_tables_are_read_only() -> None:
    compiled = compile_mandate(meeting_mandate())
    with pytest.raises(ValueError, match="read-only"):
        compiled.utility.table()[0, 0] = 0
    with pytest.raises(ValueError, match="read-only"):
        compiled.constraints.mask()[0, 0] = False
