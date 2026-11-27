from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from envoy.app.owner_view import OwnerView
from envoy.app.ports import OwnerTarget, OwnerViewStore, SessionRecord
from envoy.app.steps import SessionSteps
from envoy.domain.ids import OwnerId
from envoy.security.widget_projection import SummaryRow, deal_status
from envoy.telegram.links import topic_link
from envoy.telegram.ports import BotIdentity, TopicCreator, TopicUnavailable

logger = structlog.get_logger()
SUMMARY_HOUR = 20
SUMMARY_TOPIC = "Сводка Envoy"
RUN_INTERVAL = timedelta(minutes=1)


class DailySummary:
    def __init__(
        self,
        steps: SessionSteps,
        view: OwnerView,
        store: OwnerViewStore,
        topics: TopicCreator,
        identity: BotIdentity,
        timezone: str,
        hour: int = SUMMARY_HOUR,
    ) -> None:
        self._steps = steps
        self._view = view
        self._store = store
        self._topics = topics
        self._identity = identity
        self._zone = ZoneInfo(timezone)
        self._hour = hour
        self._last_run: datetime | None = None

    async def run_due(self, now: datetime) -> None:
        if self._last_run is not None and now - self._last_run < RUN_INTERVAL:
            return
        self._last_run = now
        local = now.astimezone(self._zone)
        if local.hour < self._hour:
            return
        day = local.date()
        groups: dict[OwnerId, list[SessionRecord]] = {}
        for record in await self._steps.sessions.list_active():
            groups.setdefault(record.owner_id, []).append(record)
        for owner, records in sorted(groups.items(), key=lambda item: str(item[0])):
            if await self._store.summary_delivered(owner, day):
                continue
            try:
                rows = await self._rows(records)
                chat = records[0].owner_chat_id
                target = OwnerTarget(owner, chat, chat, await self._topic(owner, chat))
                await self._view.summary(target, day, rows)
                await self._store.mark_summary_delivered(owner, day)
            except Exception:
                logger.exception("daily_summary_failed", owner=str(owner))

    async def _rows(self, records: list[SessionRecord]) -> tuple[SummaryRow, ...]:
        username: str | None = None
        ordered: list[tuple[datetime, str, SummaryRow]] = []
        for record in records:
            ctx = await self._steps.load(record)
            due = record.deadline
            pending = ctx.view.pending
            if pending is not None and pending.expires_at is not None:
                due = min(due, pending.expires_at)
            url = None
            if record.topic_id is not None:
                if username is None:
                    username = await self._identity.bot_username()
                url = topic_link(username, record.topic_id)
            row = SummaryRow(f"Чат {record.counterpart_chat_id}", deal_status(ctx.view), url)
            ordered.append((due, str(record.id), row))
        ordered.sort(key=lambda item: (item[0], item[1]))
        return tuple(row for _, _, row in ordered)

    async def _topic(self, owner: OwnerId, chat_id: int) -> int | None:
        stored = await self._store.summary_topic(owner)
        if stored is not None:
            return stored
        try:
            thread_id = await self._topics.create_topic(chat_id, SUMMARY_TOPIC)
        except TopicUnavailable:
            return None
        await self._store.set_summary_topic(owner, thread_id)
        return thread_id
