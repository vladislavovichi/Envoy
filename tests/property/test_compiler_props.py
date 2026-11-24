from itertools import product

import pytest
from hypothesis import given, settings

from envoy.mandate.compiler import compile_mandate
from envoy.mandate.schema import Mandate
from tests.strategies import mandates


@settings(max_examples=60, deadline=None)
@given(mandates())
def test_scalar_and_vector_paths_agree_on_random_mandates(mandate: Mandate) -> None:
    compiled = compile_mandate(mandate)
    mask = compiled.constraints.mask()
    table = compiled.utility.table()
    assert mask.shape == table.shape == compiled.grid.shape
    for index in product(*(range(size) for size in compiled.grid.shape)):
        offer = compiled.grid.offer_at(index)
        assert compiled.grid.index_of(offer) == index
        assert bool(mask[index]) == compiled.constraints.is_feasible(offer)
        assert float(table[index]) == pytest.approx(compiled.utility.value(offer), abs=1e-12)
        assert 0.0 <= float(table[index]) <= 1.0
