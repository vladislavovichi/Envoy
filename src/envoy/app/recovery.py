import structlog

from envoy.app.ports import Scheduler, SessionRepository
from envoy.app.processor import UpdateProcessor
from envoy.ports.clock import Clock

logger = structlog.get_logger()


class Recovery:
    def __init__(
        self,
        sessions: SessionRepository,
        processor: UpdateProcessor,
        scheduler: Scheduler,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._processor = processor
        self._scheduler = scheduler
        self._clock = clock

    async def resume(self) -> None:
        for record in await self._sessions.list_active():
            try:
                await self._processor.resume(record)
            except Exception:
                logger.exception("session_resume_failed", sid=str(record.id))
        for job in await self._scheduler.due(self._clock.now()):
            try:
                await self._processor.run_job(job)
            except Exception:
                logger.exception("job_failed", key=job.key)
