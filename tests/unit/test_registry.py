import pytest

from envoy.ports.registry import DuplicateRegistration, Registry, UnknownImplementation


def test_registry_creates_registered_factory() -> None:
    registry = Registry[int]()
    registry.register("length", lambda value: len(value))
    assert registry.create("length", value="abc") == 3


def test_registry_rejects_duplicate_name() -> None:
    registry = Registry[int]()
    registry.register("one", lambda: 1)
    with pytest.raises(DuplicateRegistration):
        registry.register("one", lambda: 2)


def test_registry_unknown_name_lists_known() -> None:
    registry = Registry[int]()
    registry.register("one", lambda: 1)
    with pytest.raises(UnknownImplementation, match="one"):
        registry.create("missing")
