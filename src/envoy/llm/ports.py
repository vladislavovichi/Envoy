from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import BaseModel


class LanguageModelError(Exception):
    pass


class BudgetExceeded(LanguageModelError):
    pass


@dataclass(frozen=True)
class StructuredRequest[T: BaseModel]:
    system: str
    data: str
    schema: type[T]
    max_tokens: int


class LanguageModel(Protocol):
    async def complete[T: BaseModel](self, request: StructuredRequest[T]) -> T: ...


class UsageLedger(Protocol):
    async def add(self, day: date, tokens: int) -> int: ...
