from datetime import date

from pydantic import BaseModel

from envoy.llm.ports import BudgetExceeded, LanguageModel, StructuredRequest, UsageLedger
from envoy.ports.clock import Clock


def estimate_tokens[T: BaseModel](request: StructuredRequest[T]) -> int:
    return request.max_tokens + (len(request.system) + len(request.data)) // 3 + 1


class InMemoryUsageLedger:
    def __init__(self) -> None:
        self._usage: dict[date, int] = {}

    async def add(self, day: date, tokens: int) -> int:
        self._usage[day] = self._usage.get(day, 0) + tokens
        return self._usage[day]


class BudgetedLanguageModel:
    def __init__(
        self, inner: LanguageModel, ledger: UsageLedger, daily_token_budget: int, clock: Clock
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._budget = daily_token_budget
        self._clock = clock

    async def complete[T: BaseModel](self, request: StructuredRequest[T]) -> T:
        day = self._clock.now().date()
        cost = estimate_tokens(request)
        if await self._ledger.add(day, 0) + cost > self._budget:
            raise BudgetExceeded("Daily language model budget exhausted")
        await self._ledger.add(day, cost)
        return await self._inner.complete(request)
