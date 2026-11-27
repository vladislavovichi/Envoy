import json

from envoy.domain.events import OwnerResolved
from envoy.llm.translator import IntentWire
from tests.support.harness import build_harness, propose
from tests.support.mandates import SLOTS, meeting_mandate

CONFIRMING = meeting_mandate(escalation={"always": ["accept_deal"]})
DASHBOARD = 'id="round"'
ESCALATION = "Нужно ваше решение"
SUMMARY = 'id="rows"'


async def widget_started(mandate=None, **options):
    options.setdefault("owner_view", "widget")
    harness = build_harness(**options)
    await harness.connect()
    await harness.activate(mandate)
    await harness.counterpart("привет", IntentWire(kind="unclear"))
    await harness.owner_in_chat("/envoy")
    return harness


async def escalated(**options):
    harness = await widget_started(CONFIRMING, **options)
    await harness.counterpart("ок", IntentWire(kind="accept"))
    return harness


async def counter_round(harness, index: int) -> None:
    await harness.counterpart(f"раунд {index}", propose(SLOTS[7].isoformat(), "park"))


def state_of(harness, message_id: int) -> dict:
    return json.loads(harness.transport.widgets[message_id].state)


def states(harness, message_id: int) -> list[dict]:
    return [json.loads(state) for state in harness.transport.widget_states[message_id]]


def resolutions(events, cause: int) -> list[OwnerResolved]:
    return [event for event in events if isinstance(event, OwnerResolved) and event.cause == cause]
