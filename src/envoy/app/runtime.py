import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

import structlog

from envoy.app.agent_flow import AgentFlow
from envoy.app.daily_summary import DailySummary
from envoy.app.ports import Scheduler
from envoy.app.processor import UpdateProcessor
from envoy.app.recovery import Recovery
from envoy.ports.clock import Clock
from envoy.telegram.ports import UpdateSource

logger = structlog.get_logger()


class Runtime:
    def __init__(
        self,
        source: UpdateSource,
        processor: UpdateProcessor,
        scheduler: Scheduler,
        recovery: Recovery,
        clock: Clock,
        job_interval: float = 1.0,
        daily_summary: DailySummary | None = None,
        agents: AgentFlow | None = None,
        startup: Sequence[Callable[[], Awaitable[None]]] = (),
        ticks: Sequence[Callable[[datetime], Awaitable[None]]] = (),
    ) -> None:
        self._source = source
        self._processor = processor
        self._scheduler = scheduler
        self._recovery = recovery
        self._clock = clock
        self._job_interval = job_interval
        self._daily_summary = daily_summary
        self._agents = agents
        self._startup = tuple(startup)
        self._ticks = tuple(ticks)

    async def run(self) -> None:
        for hook in self._startup:
            try:
                await hook()
            except Exception:
                logger.exception("startup_hook_failed")
        await self._recovery.resume()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._updates())
            group.create_task(self._jobs())

    async def _updates(self) -> None:
        async for update in self._source.updates():
            try:
                await self._processor.process(update)
            except Exception:
                logger.exception("update_failed", update_id=update.update_id)

    async def run_due_jobs(self) -> None:
        for job in await self._scheduler.due(self._clock.now()):
            try:
                await self._processor.run_job(job)
            except Exception:
                logger.exception("job_failed", key=job.key)
        if self._agents is not None:
            try:
                await self._agents.run_due(self._clock.now())
            except Exception:
                logger.exception("agent_jobs_failed")
        for tick in self._ticks:
            try:
                await tick(self._clock.now())
            except Exception:
                logger.exception("tick_failed")
        if self._daily_summary is not None:
            try:
                await self._daily_summary.run_due(self._clock.now())
            except Exception:
                logger.exception("daily_summary_failed")

    async def _jobs(self) -> None:
        while True:
            await self.run_due_jobs()
            await asyncio.sleep(self._job_interval)
