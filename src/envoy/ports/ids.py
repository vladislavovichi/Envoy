from typing import Protocol
from uuid import UUID, uuid7


class IdGenerator(Protocol):
    def new_id(self) -> UUID: ...


class UuidV7Generator:
    def new_id(self) -> UUID:
        return uuid7()


class SequentialIdGenerator:
    def __init__(self) -> None:
        self._next = 0

    def new_id(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)
