from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from envoy.domain.ids import MandateId
from envoy.domain.offer import IssueValue, Offer
from envoy.domain.telegram import InlineButton, ReplyMarkup
from envoy.mandate.schema import Mandate
from envoy.security.widget_projection import Dashboard, EscalationCard, IssueRow, SummaryRow

ISSUE_LABELS = {"time": "время", "place": "место"}
REASON_TEXT = {
    "confirm_action": "нужно ваше подтверждение сделки",
    "reply_window_closed": "окно ответа в чате закрыто: собеседник не писал больше 24 часов",
    "translator_failed": "не удалось разобрать сообщение собеседника",
    "counterpart_unclear": "собеседник трижды ответил неясно",
}

STATUS_TEXT = {
    "counterpart": "ждёт ответа собеседника",
    "owner": "ждёт вашего решения",
    "agreed": "согласовано",
    "fulfilled": "закрыто: встреча состоялась",
    "rejected": "закрыто без сделки",
    "expired": "закрыто по сроку",
}
OUTCOME_TEXT = {
    "approved": "одобрено",
    "declined": "отклонено",
    "timeout": "время истекло — отклонено",
}

type Button = InlineButton


@dataclass(frozen=True)
class RenderedCard:
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()

    def markup(self) -> ReplyMarkup | None:
        return ReplyMarkup(self.buttons) if self.buttons else None


class MessageRenderer(Protocol):
    def offer(self, offer: Offer) -> str: ...
    def accept(self, offer: Offer) -> str: ...
    def clarification(self, missing: Sequence[str]) -> str: ...
    def rejection(self, reason: str | None) -> str: ...
    def disclosure(self, owner_name: str, agent_name: str) -> str: ...
    def deal_summary(self, offer: Offer) -> str: ...
    def owner_notice(self, reason: str, link: str | None = None) -> str: ...


class CardRenderer(Protocol):
    def mandate_draft(self, mandate: Mandate, draft_id: MandateId) -> RenderedCard: ...
    def escalation(self, view_projection: Mapping[str, JsonValue], cause: int) -> RenderedCard: ...


class TemplateRenderer:
    def __init__(self, timezone: str) -> None:
        self._zone = ZoneInfo(timezone)

    def _value(self, value: IssueValue | JsonValue) -> str:
        if isinstance(value, datetime):
            return value.astimezone(self._zone).strftime("%d.%m.%Y %H:%M")
        if isinstance(value, str):
            try:
                return self._value(datetime.fromisoformat(value))
            except ValueError:
                return value
        return str(value)

    def value(self, value: JsonValue) -> str:
        return self._value(value)

    def _moment(self, value: datetime) -> str:
        return value.astimezone(self._zone).strftime("%d.%m.%Y %H:%M")

    def _terms(self, values: Mapping[str, IssueValue | JsonValue]) -> str:
        return ", ".join(
            f"{ISSUE_LABELS.get(issue, issue)}: {self._value(value)}"
            for issue, value in sorted(values.items())
        )

    def offer(self, offer: Offer) -> str:
        return f"Предлагаю так — {self._terms(offer.values)}. Подходит?"

    def accept(self, offer: Offer) -> str:
        return f"Договорились: {self._terms(offer.values)}."

    def clarification(self, missing: Sequence[str]) -> str:
        names = ", ".join(ISSUE_LABELS.get(issue, issue) for issue in missing)
        return f"Уточните, пожалуйста: {names}."

    def rejection(self, reason: str | None) -> str:
        return "К сожалению, в этот раз договориться не получится. Спасибо за время!"

    def disclosure(self, owner_name: str, agent_name: str) -> str:
        return (
            f"Здравствуйте! Пишет {agent_name} — ИИ-агент, который договаривается "
            f"от имени {owner_name}. Я помогу согласовать время и место встречи; "
            "итог подтверждается владельцем."
        )

    def deal_summary(self, offer: Offer) -> str:
        return f"Сделка согласована: {self._terms(offer.values)}."

    def owner_notice(self, reason: str, link: str | None = None) -> str:
        text = f"Envoy: {REASON_TEXT.get(reason, reason)}."
        return text if link is None else f"{text}\n{link}"

    def mandate_draft(self, mandate: Mandate, draft_id: MandateId) -> RenderedCard:
        lines = ["Черновик мандата «встреча»:"]
        for issue in mandate.issues:
            values = ", ".join(self._value(value) for value in issue.values())
            lines.append(f"• {ISSUE_LABELS.get(issue.id, issue.id)} (по приоритету): {values}")
        excluded: list[str] = []
        for predicate in mandate.hard_constraints:
            if predicate.kind == "cmp" and predicate.op == "not_in":
                if isinstance(predicate.value, tuple):
                    excluded += [str(item) for item in predicate.value]
        if excluded:
            lines.append(f"• исключить: {', '.join(excluded)}")
        lines.append(f"• раундов не больше: {mandate.limits.max_rounds}")
        confirm = "да" if "accept_deal" in mandate.escalation.always else "нет"
        lines.append(f"• спрашивать вас перед согласием: {confirm}")
        lines.append("Проверьте значения — после подтверждения агент начнёт действовать.")
        return RenderedCard(
            "\n".join(lines),
            (
                (
                    InlineButton("Подтвердить", f"mandate_confirm:{draft_id}", "success"),
                    InlineButton("Отменить", f"mandate_cancel:{draft_id}", "danger"),
                ),
            ),
        )

    def escalation(self, view_projection: Mapping[str, JsonValue], cause: int) -> RenderedCard:
        offer = view_projection.get("offer")
        terms = self._terms(offer) if isinstance(offer, dict) else "—"
        reason = str(view_projection.get("reason", ""))
        return RenderedCard(
            f"Нужно решение: {REASON_TEXT.get(reason, reason)}.\nУсловия: {terms}",
            (
                (
                    InlineButton("Одобрить", f"approve:{cause}", "success"),
                    InlineButton("Отклонить", f"decline:{cause}", "danger"),
                ),
            ),
        )

    @staticmethod
    def _rows(rows: tuple[IssueRow, ...]) -> str:
        return ", ".join(f"{row.name}: {row.ours or '—'}" for row in rows) or "—"

    def owner_escalation(self, card: EscalationCard) -> RenderedCard:
        text = (
            f"Нужно решение: {REASON_TEXT['confirm_action']}.\nУсловия: {self._rows(card.issues)}"
        )
        if card.outcome != "pending":
            return RenderedCard(f"{text}\nИтог: {OUTCOME_TEXT[card.outcome]}.")
        return RenderedCard(
            f"{text}\nРешение нужно до {self._moment(card.expires_at)}.",
            (
                (
                    InlineButton("Одобрить", f"approve:{card.cause}", "success"),
                    InlineButton("Отклонить", f"decline:{card.cause}", "danger"),
                ),
            ),
        )

    def owner_dashboard(self, card: Dashboard) -> str:
        lines = [
            f"Ход сделки: раунд {card.round} из {card.max_rounds}, "
            f"{STATUS_TEXT.get(card.status, card.status)}."
        ]
        for row in card.issues:
            lines.append(f"• {row.name}: вы — {row.ours or '—'}, они — {row.theirs or '—'}")
        if card.proximity is not None:
            lines.append(f"Совпадение условий: {card.proximity}%")
        if card.verified is not None:
            lines.append("Журнал проверен." if card.verified else "Проверка журнала не пройдена.")
        return "\n".join(lines)

    def summary_row(self, row: SummaryRow) -> str:
        line = f"• {row.label} — {STATUS_TEXT.get(row.status, row.status)}"
        return line if row.topic_url is None else f"{line} — {row.topic_url}"

    def owner_summary(self, day: date, rows: tuple[SummaryRow, ...], total: int) -> str:
        head = f"Сводка Envoy за {day.strftime('%d.%m.%Y')}, активных сделок: {total}"
        return "\n".join([head, *(self.summary_row(row) for row in rows)])
