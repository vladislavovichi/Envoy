from collections.abc import Callable
from typing import Any


class DuplicateRegistration(ValueError):
    pass


class UnknownImplementation(LookupError):
    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        super().__init__(f"Unknown implementation {name!r}; known: {', '.join(known)}")


class Registry[T]:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., T]] = {}

    def register(self, name: str, factory: Callable[..., T]) -> None:
        if name in self._factories:
            raise DuplicateRegistration(name)
        self._factories[name] = factory

    def create(self, name: str, **kwargs: Any) -> T:
        try:
            factory = self._factories[name]
        except KeyError:
            raise UnknownImplementation(name, sorted(self._factories)) from None
        return factory(**kwargs)
