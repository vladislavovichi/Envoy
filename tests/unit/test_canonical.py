from datetime import datetime
from uuid import UUID

import pytest

from envoy.crypto.canonical import encode


def test_canonical_known_bytes_and_nested_values() -> None:
    assert encode({"b": 2, "a": 1}) == bytes.fromhex("a2616101616202")
    assert encode([UUID(int=1)]) == encode((UUID(int=1),))


def test_canonical_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="aware"):
        encode(datetime(2026, 1, 1))
