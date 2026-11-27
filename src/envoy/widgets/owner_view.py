import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Literal

import structlog
from pydantic import JsonValue

from envoy.app.owner_view import TEXT_VERSION, TextOwnerView
from envoy.app.ports import (
    OwnerTarget,
    OwnerViewStore,
    SessionRepository,
    SummaryWidgetRef,
    WidgetRef,
)
from envoy.domain.ids import SessionId
from envoy.domain.telegram import ReplyMarkup
from envoy.ports.clock import Clock
from envoy.security.widget_projection import Dashboard, EscalationCard, IssueRow, SummaryRow
from envoy.telegram.ports import SendRejected, WidgetTransport
from envoy.widgets.resources import CURRENT_VERSION, WidgetKind, load_widget

logger = structlog.get_logger()
STATE_LIMIT = 1024
CAPTION_LIMIT = 1024
SUMMARY_ROWS = 10
SUMMARY_REVISION = 1
SHORT_VALUE = 40

type LinkMode = Literal["direct", "request"]


def encode_state(payload: Mapping[str, JsonValue]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def fits(state: str) -> bool:
    return len(state.encode("utf-8")) <= STATE_LIMIT and "\x00" not in state


def _short(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _issues(rows: tuple[IssueRow, ...], limit: int | None = None) -> list[JsonValue]:
    return [
        {
            "name": row.name,
            "ours": row.ours if limit is None else _short(row.ours, limit),
            "theirs": row.theirs if limit is None else _short(row.theirs, limit),
        }
        for row in rows
    ]


def dashboard_state(card: Dashboard, limit: int | None = None) -> str:
    return encode_state(
        {
            "r": card.revision,
            "round": card.round,
            "max": card.max_rounds,
            "status": card.status,
            "issues": _issues(card.issues, limit),
            "proximity": card.proximity,
            "verified": card.verified,
        }
    )


def escalation_state(card: EscalationCard, now: datetime) -> str:
    remaining = max(0, int((card.expires_at - now).total_seconds()))
    return encode_state(
        {
            "r": card.revision,
            "cause": card.cause,
            "expires_at": card.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "remaining": remaining if card.outcome == "pending" else 0,
            "issues": _issues(card.issues),
            "outcome": card.outcome,
        }
    )


def summary_state(rows: tuple[SummaryRow, ...], total: int, links: LinkMode) -> str:
    return encode_state(
        {
            "r": SUMMARY_REVISION,
            "rows": [
                {"label": row.label, "status": row.status, "url": row.topic_url} for row in rows
            ],
            "total": total,
            "links": links,
        }
    )


def caption_for(text: str) -> str:
    return text if len(text) <= CAPTION_LIMIT else text[: CAPTION_LIMIT - 1] + "…"


class WidgetOwnerView:
    def __init__(
        self,
        transport: WidgetTransport,
        text: TextOwnerView,
        sessions: SessionRepository,
        store: OwnerViewStore,
        clock: Clock,
        links: LinkMode = "request",
    ) -> None:
        self._transport = transport
        self._text = text
        self._sessions = sessions
        self._store = store
        self._clock = clock
        self._links = links

    async def _send(
        self,
        target: OwnerTarget,
        kind: WidgetKind,
        state: str,
        caption: str,
        *,
        allow_queries: bool,
        markup: ReplyMarkup | None = None,
    ) -> int | None:
        try:
            return await self._transport.send_widget(
                target.chat_id,
                load_widget(kind, CURRENT_VERSION),
                state,
                caption,
                thread_id=target.thread_id,
                allow_queries=allow_queries,
                reply_markup=markup,
            )
        except SendRejected:
            logger.warning("owner_widget_rejected", kind=kind, chat=target.chat_id)
            return None

    async def _edit(self, ref: WidgetRef, state: str) -> bool:
        try:
            await self._transport.edit_widget(ref.chat_id, ref.message_id, state)
        except SendRejected:
            logger.warning("owner_widget_edit_rejected", chat=ref.chat_id)
            return False
        return True

    @staticmethod
    def _dashboard_state(card: Dashboard) -> str | None:
        for limit in (None, SHORT_VALUE):
            state = dashboard_state(card, limit)
            if fits(state):
                return state
        return None

    async def dashboard(self, target: OwnerTarget, session: SessionId, card: Dashboard) -> None:
        record = await self._sessions.get(session)
        if record is None:
            return
        state = self._dashboard_state(card)
        if state is None:
            logger.warning("owner_dashboard_too_large", sid=str(session))
            await self._text.dashboard(target, session, card)
            return
        ref = record.dashboard_widget
        if ref is not None and ref.version == CURRENT_VERSION:
            if card.revision <= ref.revision:
                return
            if await self._edit(ref, state):
                await self._sessions.set_dashboard_widget(
                    session, replace(ref, revision=card.revision)
                )
            return
        caption = caption_for(self._text.renderer.owner_dashboard(card))
        message_id = await self._send(target, "dashboard", state, caption, allow_queries=False)
        if message_id is None:
            await self._text.dashboard(target, session, card)
            return
        await self._sessions.set_dashboard_widget(
            session, WidgetRef(target.chat_id, message_id, CURRENT_VERSION, card.revision)
        )

    async def escalation(
        self, target: OwnerTarget, session: SessionId, card: EscalationCard
    ) -> None:
        ref = await self._store.get_escalation(session, card.cause)
        if ref is not None:
            if ref.version == TEXT_VERSION or card.revision <= ref.revision:
                return
            if await self._edit(ref, escalation_state(card, self._clock.now())):
                await self._store.put_escalation(
                    session, card.cause, replace(ref, revision=card.revision)
                )
            return
        if card.outcome != "pending":
            return
        state = escalation_state(card, self._clock.now())
        rendered = self._text.renderer.owner_escalation(card)
        if not fits(state) or len(rendered.text) > CAPTION_LIMIT:
            logger.warning("owner_escalation_widget_too_large", sid=str(session))
            await self._text.escalation(target, session, card)
            return
        message_id = await self._send(
            target, "escalation", state, rendered.text, allow_queries=True, markup=rendered.markup()
        )
        if message_id is None:
            await self._text.escalation(target, session, card)
            return
        await self._store.put_escalation(
            session,
            card.cause,
            WidgetRef(target.chat_id, message_id, CURRENT_VERSION, card.revision),
        )

    def _prefix(
        self, day: date, rows: tuple[SummaryRow, ...]
    ) -> tuple[tuple[SummaryRow, ...], str, str]:
        renderer = self._text.renderer
        for size in range(min(SUMMARY_ROWS, len(rows)), 0, -1):
            prefix = rows[:size]
            state = summary_state(prefix, len(rows), self._links)
            caption = renderer.owner_summary(day, prefix, len(rows))
            if fits(state) and len(caption) <= CAPTION_LIMIT:
                return prefix, state, caption
        return (), "", ""

    async def summary(self, target: OwnerTarget, day: date, rows: tuple[SummaryRow, ...]) -> None:
        existing = await self._store.get_summary(target.owner_id, day)
        if existing is None:
            prefix, state, caption = self._prefix(day, rows)
            if not prefix:
                await self._text.summary(target, day, rows)
                return
            message_id = await self._send(target, "summary", state, caption, allow_queries=True)
            if message_id is None:
                await self._text.summary(target, day, rows)
                return
            ref = WidgetRef(target.chat_id, message_id, CURRENT_VERSION, SUMMARY_REVISION)
            await self._store.put_summary(SummaryWidgetRef(target, day, ref, prefix))
            shown = len(prefix)
        else:
            shown = len(existing.rows)
        rest = rows[shown:]
        if rest:
            renderer = self._text.renderer
            lines = [
                f"Ещё {len(rest)} из {len(rows)}:",
                *(renderer.summary_row(row) for row in rest),
            ]
            await self._text.deliver(target, "\n".join(lines))
