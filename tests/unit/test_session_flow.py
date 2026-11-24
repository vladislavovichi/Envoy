from unittest.mock import call

from envoy.domain.events import Decided, Escalated, Outcome
from envoy.domain.session import SessionState
from envoy.llm.ports import LanguageModel, LanguageModelError
from envoy.llm.translator import IntentWire
from envoy.security.authorizer import Authorizer, Deny
from tests.support.harness import OWNER_USER, build_harness, propose
from tests.support.mandates import SLOTS


async def prepared(**kwargs):
    harness = build_harness(**kwargs)
    await harness.connect()
    await harness.activate()
    await harness.counterpart("привет", IntentWire(kind="unclear"))
    return harness


async def test_sender_not_called_when_authorizer_denies(mocker) -> None:
    harness = await prepared()
    authorizer = mocker.create_autospec(Authorizer, instance=True)
    authorizer.authorize.return_value = Deny("test_deny")
    harness.pipeline.steps.authorizer = authorizer
    await harness.owner_in_chat("/envoy")
    to_counterpart = harness.to_counterpart()
    assert len(to_counterpart) == 1
    assert "ИИ-агент" in to_counterpart[0]
    decisions = [event for event in await harness.events() if isinstance(event, Decided)]
    assert [(d.verdict, d.reason) for d in decisions] == [("deny", "test_deny")]
    assert any("test_deny" in text for text in harness.to_owner())


async def test_authorizer_called_before_sender(mocker) -> None:
    harness = await prepared()
    order = mocker.Mock()
    real_authorize = harness.pipeline.steps.authorizer.authorize
    real_send = harness.transport.send_message

    def authorize(*args, **kwargs):
        order.authorize()
        return real_authorize(*args, **kwargs)

    async def send(chat_id, text, **kwargs):
        if kwargs.get("business_connection_id") and text.startswith("Предлагаю"):
            order.send()
        return await real_send(chat_id, text, **kwargs)

    mocker.patch.object(harness.pipeline.steps.authorizer, "authorize", side_effect=authorize)
    mocker.patch.object(harness.transport, "send_message", side_effect=send)
    await harness.owner_in_chat("/envoy")
    assert order.mock_calls[:2] == [call.authorize(), call.send()]


async def test_llm_not_called_for_dropped_update(mocker) -> None:
    model = mocker.create_autospec(LanguageModel, instance=True)
    harness = build_harness(model=model)
    await harness.connect()
    await harness.activate()
    await harness.counterpart("x" * 5000)
    await harness.counterpart("от бота", is_bot=True)
    await harness.counterpart(None, content_kind="other")
    model.complete.assert_not_awaited()


async def test_disclosure_sent_before_first_offer() -> None:
    harness = await prepared()
    await harness.owner_in_chat("/envoy")
    texts = harness.to_counterpart()
    assert "ИИ-агент" in texts[0]
    assert texts[1].startswith("Предлагаю")


async def test_translation_failure_takes_no_action_and_notifies_owner(mocker) -> None:
    model = mocker.create_autospec(LanguageModel, instance=True)
    model.complete.side_effect = LanguageModelError("down")
    harness = build_harness(model=model)
    await harness.connect()
    await harness.activate()
    await harness.counterpart("привет")
    await harness.owner_in_chat("/envoy")
    before = len(harness.to_counterpart())
    await harness.counterpart("давай завтра")
    assert len(harness.to_counterpart()) == before
    events = await harness.events()
    assert isinstance(events[-1], Escalated)
    assert events[-1].reason == "translator_failed"
    assert any("не удалось разобрать" in text for text in harness.to_owner())


async def test_budget_exceeded_takes_no_action() -> None:
    harness = build_harness(budget=1)
    await harness.connect()
    await harness.activate()
    await harness.counterpart("привет", IntentWire(kind="unclear"))
    await harness.owner_in_chat("/envoy")
    before = len(harness.to_counterpart())
    await harness.counterpart("давай", propose(SLOTS[0].isoformat(), "cafe"))
    assert len(harness.to_counterpart()) == before
    assert harness.llm.requests == []
    assert (await harness.events())[-1].reason == "translator_failed"


async def test_unclear_thrice_escalates_notify() -> None:
    harness = await prepared()
    await harness.owner_in_chat("/envoy")
    before = len(harness.to_counterpart())
    for index in range(3):
        await harness.counterpart(f"хм {index}", IntentWire(kind="unclear", missing=["time"]))
    texts = harness.to_counterpart()[before:]
    assert len(texts) == 2
    assert all(text.startswith("Уточните") for text in texts)
    events = await harness.events()
    assert isinstance(events[-1], Escalated)
    assert events[-1].reason == "counterpart_unclear"
    assert any("трижды" in text for text in harness.to_owner())


async def test_topic_unavailable_falls_back_to_plain_chat() -> None:
    harness = await prepared(topics=False)
    await harness.owner_in_chat("/envoy")
    record = await harness.session()
    assert record.topic_id is None
    assert all(sent.thread_id is None for sent in harness.transport.sent)
    assert (await harness.view()).state is SessionState.NEGOTIATING


async def test_owner_stop_closes_rejected() -> None:
    harness = await prepared()
    await harness.owner_in_chat("/envoy")
    await harness.owner_in_chat("/stop")
    view = await harness.view()
    assert (view.state, view.outcome) == (SessionState.CLOSED, Outcome.rejected)
    assert await harness.pipeline.stores.scheduler.pending((await harness.session()).id) == []


async def test_owner_own_message_never_triggers_negotiation(mocker) -> None:
    harness = await prepared()
    await harness.owner_in_chat("/envoy")
    requests = len(harness.llm.requests)
    before = len(harness.to_counterpart())
    await harness.owner_in_chat("привет, это я сам пишу")
    assert len(harness.llm.requests) == requests
    assert len(harness.to_counterpart()) == before


async def test_no_active_mandate_tells_owner_and_opens_nothing() -> None:
    harness = build_harness()
    await harness.connect()
    await harness.counterpart("привет", IntentWire(kind="unclear"))
    await harness.owner_in_chat("/envoy")
    assert await harness.session() is None
    assert "Нет активного мандата" in harness.to_owner()[-1]
    assert harness.to_counterpart() == []


async def test_second_envoy_in_same_chat_does_not_open_twice() -> None:
    harness = await prepared()
    await harness.owner_in_chat("/envoy")
    await harness.owner_in_chat("/envoy")
    assert len(await harness.pipeline.stores.sessions.list_active()) == 1
    assert "уже идёт" in harness.to_owner()[-1]


async def test_owner_private_messages_route_to_owner_flow() -> None:
    harness = build_harness()
    await harness.private("/start")
    assert "Модель доверия" in harness.transport.messages[-1].text
    stranger_updates = len(harness.transport.messages)
    from envoy.domain.telegram import MessageUpdate

    await harness.feed(MessageUpdate(harness.update_id(), 99, 99, text="/start"))
    assert len(harness.transport.messages) == stranger_updates
    assert OWNER_USER == harness.transport.messages[-1].chat_id
