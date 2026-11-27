import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

import structlog

from envoy.domain.ids import MandateId, OwnerId, owner_for_user
from envoy.domain.telegram import WidgetQueryUpdate
from envoy.mandate.ports import MandateNotFound, MandateStore
from envoy.ports.rng import Rng
from envoy.telegram.ports import MessageSender, QueryExpired, SendRejected, WidgetTransport
from envoy.widgets.projection import (
    MandateRequestRejected,
    SlotOption,
    WidgetMandateView,
    apply_request,
    parse_request,
    project,
    state,
)
from envoy.widgets.resources import load_widget

logger = structlog.get_logger()
ANSWER_DEADLINE = timedelta(seconds=15)
STATE_LIMIT = 1024


@dataclass(frozen=True)
class MandateCard:
    owner: OwnerId
    user_id: int
    chat_id: int
    message_id: int
    revision: int
    draft_id: MandateId
    slots: tuple[datetime, ...]
    selected: tuple[bool, ...]
    issues: tuple[str, ...]


class MandateCardStore(Protocol):
    async def put(self, card: MandateCard) -> None: ...
    async def find(self, chat_id: int, message_id: int) -> MandateCard | None: ...


class InMemoryMandateCardStore:
    def __init__(self) -> None:
        self._cards: dict[tuple[int, int], MandateCard] = {}

    async def put(self, card: MandateCard) -> None:
        self._cards[(card.chat_id, card.message_id)] = card

    async def find(self, chat_id: int, message_id: int) -> MandateCard | None:
        return self._cards.get((chat_id, message_id))


def _encode(view: WidgetMandateView) -> str:
    return json.dumps(state(view), ensure_ascii=False, separators=(",", ":"))


class MandateWidgetFlow:
    def __init__(
        self,
        transport: WidgetTransport | None,
        cards: MandateCardStore,
        mandates: MandateStore,
        sender: MessageSender,
        rng: Rng,
        label: Callable[[datetime], str],
        miniapp_url: str | None = None,
    ) -> None:
        self._transport = transport
        self._cards = cards
        self._mandates = mandates
        self._sender = sender
        self._rng = rng
        self._label = label
        self._miniapp = miniapp_url

    def _fallback(self) -> str:
        link = self._miniapp or "Mini App Envoy"
        return f"Черновик мандата редактируется в Mini App: {link}"

    async def send(self, user_id: int) -> None:
        owner = owner_for_user(user_id)
        active = await self._mandates.active(owner)
        if active is None:
            await self._text(user_id, "Нет активного мандата: сначала опишите условия встречи.")
            return
        draft = await self._mandates.save_draft(owner, active.mandate, self._rng.token_bytes(32))
        view = project(draft.mandate, draft.version, self._label)
        encoded = _encode(view)
        if self._transport is None or len(encoded.encode("utf-8")) > STATE_LIMIT:
            await self._text(user_id, self._fallback())
            return
        try:
            message_id = await self._transport.send_widget(
                user_id,
                load_widget("mandate"),
                encoded,
                self._fallback(),
                thread_id=None,
                allow_queries=True,
            )
        except SendRejected:
            await self._text(user_id, self._fallback())
            return
        await self._cards.put(
            MandateCard(
                owner,
                user_id,
                user_id,
                message_id,
                view.revision,
                draft.id,
                tuple(slot.value for slot in view.slots),
                tuple(slot.selected for slot in view.slots),
                view.adjustable_issues,
            )
        )

    async def _text(self, chat_id: int, text: str) -> None:
        try:
            await self._sender.send_message(chat_id, text)
        except SendRejected:
            logger.warning("mandate_widget_notice_failed", chat=chat_id)

    async def _answer(self, update: WidgetQueryUpdate, result: str) -> None:
        if self._transport is None:
            return
        try:
            await self._transport.answer_widget(
                update.query_id, result, deadline=update.received_at + ANSWER_DEADLINE
            )
        except QueryExpired:
            logger.warning("mandate_widget_query_expired", update_id=update.update_id)
        except SendRejected:
            logger.warning("mandate_widget_answer_rejected", update_id=update.update_id)

    async def handle(self, update: WidgetQueryUpdate) -> None:
        await self._answer(update, await self._apply(update))

    async def _apply(self, update: WidgetQueryUpdate) -> str:
        if update.chat_id is None or update.message_id is None or update.data is None:
            return "no"
        card = await self._cards.find(update.chat_id, update.message_id)
        if card is None or card.user_id != update.user_id:
            return "no"
        try:
            request = parse_request(update.data)
            draft = await self._mandates.get(card.draft_id)
        except MandateRequestRejected:
            return "bad"
        except MandateNotFound:
            return "stale"
        if draft.status != "draft":
            return "stale"
        view = WidgetMandateView(
            card.revision,
            tuple(
                SlotOption(value, self._label(value), selected)
                for value, selected in zip(card.slots, card.selected, strict=True)
            ),
            card.issues,
        )
        try:
            updated = apply_request(draft.mandate, view, request)
        except MandateRequestRejected as error:
            return "stale" if error.reason == "stale_revision" else "bad"
        selected = tuple(index in request.selected for index in range(len(view.slots)))
        bumped = replace(card, revision=card.revision + 1, selected=selected)
        await self._cards.put(bumped)
        saved = await self._mandates.save_draft(card.owner, updated, self._rng.token_bytes(32))
        await self._cards.put(replace(bumped, draft_id=saved.id))
        fresh = WidgetMandateView(
            bumped.revision,
            tuple(
                replace(slot, selected=flag)
                for slot, flag in zip(view.slots, selected, strict=True)
            ),
            view.adjustable_issues,
        )
        assert self._transport is not None
        try:
            await self._transport.edit_widget(card.chat_id, card.message_id, _encode(fresh))
        except SendRejected:
            logger.warning("mandate_widget_edit_rejected", chat=card.chat_id)
        return "ok"
