from datetime import UTC, datetime, timedelta

import pytest

from envoy.llm.budget import BudgetedLanguageModel, InMemoryUsageLedger, estimate_tokens
from envoy.llm.ports import BudgetExceeded, LanguageModel, StructuredRequest
from envoy.llm.translator import IntentWire
from envoy.ports.clock import FixedClock

REQUEST = StructuredRequest("s" * 30, "d" * 30, IntentWire, 100)


@pytest.fixture
def inner(mocker):
    model = mocker.create_autospec(LanguageModel, instance=True)
    model.complete.return_value = IntentWire(kind="accept")
    return model


async def test_budget_blocks_before_inner_call(inner) -> None:
    clock = FixedClock(datetime(2026, 12, 1, tzinfo=UTC))
    budgeted = BudgetedLanguageModel(inner, InMemoryUsageLedger(), estimate_tokens(REQUEST), clock)
    assert await budgeted.complete(REQUEST) == IntentWire(kind="accept")
    inner.complete.reset_mock()
    with pytest.raises(BudgetExceeded):
        await budgeted.complete(REQUEST)
    inner.complete.assert_not_awaited()


async def test_budget_resets_next_day(inner) -> None:
    clock = FixedClock(datetime(2026, 12, 1, 23, tzinfo=UTC))
    budgeted = BudgetedLanguageModel(inner, InMemoryUsageLedger(), estimate_tokens(REQUEST), clock)
    await budgeted.complete(REQUEST)
    with pytest.raises(BudgetExceeded):
        await budgeted.complete(REQUEST)
    clock.advance(timedelta(hours=2))
    await budgeted.complete(REQUEST)
    assert inner.complete.await_count == 2


async def test_ledger_accumulates_per_day() -> None:
    ledger = InMemoryUsageLedger()
    day = datetime(2026, 12, 1).date()
    assert await ledger.add(day, 5) == 5
    assert await ledger.add(day, 7) == 12
    assert await ledger.add(day + timedelta(days=1), 1) == 1
