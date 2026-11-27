from datetime import date, timedelta
from uuid import UUID

from envoy.app.memory import InMemoryOwnerViewStore
from envoy.app.owner_view import TextOwnerView, split_text
from envoy.app.ports import OwnerTarget
from envoy.app.renderer import TemplateRenderer
from envoy.domain.ids import OwnerId, SessionId
from envoy.security.widget_projection import EscalationCard, IssueRow, SummaryRow
from envoy.telegram.memory import FakeTransport
from envoy.telegram.ports import SendRejected
from tests.support.sessions import T0

SESSION = SessionId(UUID(int=5))
TARGET = OwnerTarget(OwnerId(UUID(int=1)), 1, 1, 1000)
ROWS = (IssueRow("место", "кафе", None), IssueRow("время", "01.12.2026 12:00", None))


def card(outcome="pending", cause=42) -> EscalationCard:
    return EscalationCard(3, cause, T0 + timedelta(hours=1), ROWS, outcome)


def view(transport=None) -> tuple[TextOwnerView, FakeTransport, InMemoryOwnerViewStore]:
    transport = transport or FakeTransport()
    store = InMemoryOwnerViewStore()
    return TextOwnerView(transport, TemplateRenderer("UTC"), store), transport, store


async def test_pending_escalation_sends_existing_buttons_once() -> None:
    text, transport, store = view()
    await text.escalation(TARGET, SESSION, card())
    await text.escalation(TARGET, SESSION, card())
    assert len(transport.sent) == 1
    sent = transport.sent[0]
    assert sent.message.text.startswith("Нужно решение: нужно ваше подтверждение сделки.")
    assert "Условия: место: кафе, время: 01.12.2026 12:00" in sent.message.text
    assert "до 01.12.2026 13:00" in sent.message.text
    assert [button.data for button in sent.reply_markup.buttons[0]] == ["approve:42", "decline:42"]
    assert sent.thread_id == 1000
    ref = await store.get_escalation(SESSION, 42)
    assert (ref.chat_id, ref.message_id, ref.version) == (1, sent.message.message_id, 0)


async def test_resolved_escalation_sends_nothing_new() -> None:
    text, transport, _ = view()
    for outcome in ("approved", "declined", "timeout"):
        await text.escalation(TARGET, SESSION, card(outcome))
    assert transport.sent == []
    rendered = text.renderer.owner_escalation(card("timeout"))
    assert rendered.markup() is None
    assert "время истекло" in rendered.text


async def test_dashboard_in_text_mode_keeps_phase1_notices_only() -> None:
    text, transport, _ = view()
    await text.dashboard(TARGET, SESSION, None)
    assert transport.sent == []


async def test_summary_keeps_every_row_and_topic_link() -> None:
    text, transport, _ = view()
    rows = tuple(
        SummaryRow(
            f"Чат {index}", "owner", f"https://t.me/envoy_bot/{index}" if index % 2 else None
        )
        for index in range(12)
    )
    await text.summary(TARGET, date(2026, 12, 1), rows)
    body = "\n".join(record.message.text for record in transport.sent)
    assert "Сводка Envoy за 01.12.2026, активных сделок: 12" in body
    for row in rows:
        assert row.label in body
        if row.topic_url:
            assert row.topic_url in body


class TopicRejecting(FakeTransport):
    async def send_message(self, chat_id, text, *, thread_id=None, **kwargs):
        if thread_id is not None:
            raise SendRejected("no topic")
        return await super().send_message(chat_id, text, thread_id=thread_id, **kwargs)


async def test_unavailable_topic_falls_back_to_main_private_chat() -> None:
    text, transport, _ = view(TopicRejecting())
    await text.escalation(TARGET, SESSION, card())
    assert len(transport.sent) == 1
    assert transport.sent[0].thread_id is None
    assert transport.sent[0].reply_markup is not None


class Rejecting(FakeTransport):
    async def send_message(self, *args, **kwargs):
        raise SendRejected("blocked")


async def test_rejected_delivery_does_not_bind_card() -> None:
    text, _, store = view(Rejecting())
    await text.escalation(TARGET, SESSION, card())
    assert await store.get_escalation(SESSION, 42) is None


async def test_long_text_is_split_and_buttons_stay_on_last_chunk() -> None:
    text, transport, _ = view()
    long_rows = (IssueRow("место", "<b>&" * 2500, None),)
    await text.escalation(TARGET, SESSION, EscalationCard(1, 7, T0, long_rows, "pending"))
    assert len(transport.sent) >= 2
    assert all(len(record.message.text) <= 4096 for record in transport.sent)
    assert [record.reply_markup is not None for record in transport.sent][-1]
    assert all(record.reply_markup is None for record in transport.sent[:-1])
    joined = "".join(record.message.text for record in transport.sent)
    assert "<b>&" * 2500 in joined


def test_split_text_preserves_content() -> None:
    assert split_text("") == [""]
    assert split_text("a\nb", 10) == ["a\nb"]
    assert split_text("aaaa\nbbbb", 5) == ["aaaa", "bbbb"]
    assert "".join(split_text("x" * 23, 10)) == "x" * 23
