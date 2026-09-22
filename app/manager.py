from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .analyzer import AnalysisArtifacts, DesignAnalyzer, ProgressUpdate
from .storage import Job, Storage

logger = logging.getLogger(__name__)
Notify = Callable[[Job, AnalysisArtifacts | None, str | None], Awaitable[None]]
ProgressNotify = Callable[[Job, ProgressUpdate], Awaitable[None]]


class AnalysisManager:
    def __init__(
        self,
        storage: Storage,
        analyzer: DesignAnalyzer,
        workers: int,
        notify: Notify,
        progress_notify: ProgressNotify,
    ) -> None:
        self.storage = storage
        self.analyzer = analyzer
        self.workers = workers
        self.notify = notify
        self.progress_notify = progress_notify
        self.queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._known_ids: set[int] = set()
        self._progress: dict[int, ProgressUpdate] = {}

    async def start(self) -> None:
        if self._tasks:
            return
        for job in self.storage.pending_jobs():
            self._enqueue_id(job.id)
        self._tasks = [asyncio.create_task(self._worker(i), name=f"analysis-worker-{i}") for i in range(self.workers)]

    async def stop(self) -> None:
        if not self._tasks:
            return
        for _ in self._tasks:
            await self.queue.put(None)
        await self.queue.join()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def enqueue(self, job_id: int) -> int:
        self._enqueue_id(job_id)
        return self.queue.qsize()

    def approximate_position(self) -> int:
        return self.queue.qsize() + 1

    def get_progress(self, job_id: int) -> ProgressUpdate | None:
        return self._progress.get(job_id)

    def _enqueue_id(self, job_id: int) -> None:
        if job_id in self._known_ids:
            return
        self._known_ids.add(job_id)
        self.queue.put_nowait(job_id)

    async def _publish_progress(self, job_id: int, update: ProgressUpdate) -> None:
        self._progress[job_id] = update
        try:
            await self.progress_notify(self.storage.get_job(job_id), update)
        except Exception:
            logger.exception("failed to publish progress for job %s", job_id)

    async def _worker(self, worker_id: int) -> None:
        while True:
            job_id = await self.queue.get()
            if job_id is None:
                self.queue.task_done()
                return
            try:
                job = self.storage.get_job(job_id)
                if job.status != "queued":
                    continue

                self.storage.mark_running(job_id)
                job = self.storage.get_job(job_id)
                estimate = self.storage.estimate_duration_seconds(job.plan, job.pages)

                await self._publish_progress(
                    job_id,
                    ProgressUpdate(1, "Iniciando análise", 0, estimate),
                )

                async def progress_cb(update: ProgressUpdate) -> None:
                    await self._publish_progress(job_id, update)

                artifacts = await self.analyzer.analyze(
                    job.id,
                    job.url,
                    job.pages,
                    job.plan,
                    progress=progress_cb,
                    estimated_seconds=estimate,
                )
                self.storage.mark_completed(job_id, str(artifacts.output_dir))
                await self._publish_progress(
                    job_id,
                    ProgressUpdate(100, "Concluída", max(1, self._progress.get(job_id, ProgressUpdate(99, "")).elapsed_seconds), 0),
                )
                await self.notify(self.storage.get_job(job_id), artifacts, None)
            except Exception as exc:
                logger.exception("analysis job %s failed on worker %s", job_id, worker_id)
                try:
                    self.storage.mark_failed(job_id, str(exc))
                    failed_job = self.storage.get_job(job_id)
                    await self.notify(failed_job, None, str(exc))
                except Exception:
                    logger.exception("failed to persist/notify failure for job %s", job_id)
            finally:
                self._known_ids.discard(job_id)
                self.queue.task_done()
