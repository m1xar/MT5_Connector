from __future__ import annotations

import asyncio
import multiprocessing
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from typing import Any, Awaitable, Callable, Optional

from utils.id import new_id
from utils.logging import get_logger, log_event

from .protocol import (
    PRIORITY_HARD,
    PRIORITY_SCHEDULED,
    PoolStatus,
    SyncResult,
    SyncTask,
    WorkerReady,
    WorkerState,
    WorkerStatus,
)
from .worker import worker_main

logger = get_logger(__name__)

ResultHandler = Callable[[SyncResult], Awaitable[None]]

_RESTART_ATTEMPTS = 3
_RESTART_BACKOFF_SECONDS = 5.0


@dataclass
class _QueuedTask:
    task: SyncTask
    priority: int
    future: "asyncio.Future[SyncResult]"
    attempts: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)


@dataclass
class _Worker:
    worker_id: str
    terminal_path: str
    state: WorkerState = WorkerState.starting
    process: Any = None
    connection: Any = None
    current_account_id: Optional[str] = None
    current_task_started_at: Optional[datetime] = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    restarts: int = 0
    last_error: Optional[str] = None

    def status(self) -> WorkerStatus:
        return WorkerStatus(
            worker_id=self.worker_id,
            terminal_path=self.terminal_path,
            state=self.state,
            pid=getattr(self.process, "pid", None),
            current_account_id=self.current_account_id,
            current_task_started_at=self.current_task_started_at,
            tasks_completed=self.tasks_completed,
            tasks_failed=self.tasks_failed,
            restarts=self.restarts,
            last_error=self.last_error,
        )


class PoolManager:
    def __init__(
        self,
        terminal_paths: list[str],
        *,
        on_result: ResultHandler | None = None,
        task_timeout_seconds: float = 300.0,
        worker_start_timeout_seconds: float = 120.0,
        max_task_retries: int = 2,
        init_timeout_ms: int = 60000,
        login_timeout_ms: int = 60000,
        log_level: str = "INFO",
        log_json: bool = True,
        enrich_mae_mfe: bool = True,
        worker_target: Callable[..., None] = worker_main,
        mp_context: Any = None,
    ) -> None:
        self.terminal_paths = terminal_paths
        self.on_result = on_result
        self.task_timeout_seconds = task_timeout_seconds
        self.worker_start_timeout_seconds = worker_start_timeout_seconds
        self.max_task_retries = max_task_retries
        self.init_timeout_ms = init_timeout_ms
        self.login_timeout_ms = login_timeout_ms
        self.log_level = log_level
        self.log_json = log_json
        self.enrich_mae_mfe = enrich_mae_mfe
        self._worker_target = worker_target
        self._mp = mp_context or multiprocessing.get_context("spawn")

        self._workers: list[_Worker] = []
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._idle: asyncio.Queue[_Worker] = asyncio.Queue()
        self._sequence = count()
        self._dispatcher: asyncio.Task | None = None
        self._in_flight: dict[str, _QueuedTask] = {}
        self._pending_accounts: dict[str, "asyncio.Future[SyncResult]"] = {}
        self._closing = False


    async def start(self) -> None:
        for index, path in enumerate(self.terminal_paths):
            worker = _Worker(worker_id=f"w{index + 1}", terminal_path=path)
            self._workers.append(worker)

        results = await asyncio.gather(
            *(self._spawn(worker) for worker in self._workers),
            return_exceptions=True,
        )
        for worker, ok in zip(self._workers, results):
            if ok is True:
                worker.state = WorkerState.idle
                self._idle.put_nowait(worker)

        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="pool-dispatcher")
        log_event(
            logger,
            "info",
            "pool.started",
            workers=len(self._workers),
            ready=sum(1 for worker in self._workers if worker.state == WorkerState.idle),
        )

    async def stop(self) -> None:
        self._closing = True
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
            self._dispatcher = None

        for worker in self._workers:
            await self._terminate(worker)
            worker.state = WorkerState.stopped

        while not self._queue.empty():
            _, _, item = self._queue.get_nowait()
            self._resolve(item, self._failure(item, "pool is shutting down"))
        log_event(logger, "info", "pool.stopped")


    def submit(
        self,
        account_id: str,
        login: int,
        password: str,
        server: str,
        *,
        priority: int = PRIORITY_SCHEDULED,
        dedupe: bool = False,
        sync_run_id: str | None = None,
    ) -> "asyncio.Future[SyncResult]":
        if dedupe and account_id in self._pending_accounts:
            return self._pending_accounts[account_id]

        loop = asyncio.get_running_loop()
        future: "asyncio.Future[SyncResult]" = loop.create_future()
        task = SyncTask(
            task_id=new_id(),
            account_id=account_id,
            login=login,
            password=password,
            server=server,
            sync_run_id=sync_run_id,
        )
        item = _QueuedTask(task=task, priority=priority, future=future)
        if dedupe:
            self._pending_accounts[account_id] = future
        self._queue.put_nowait((priority, next(self._sequence), item))
        log_event(
            logger,
            "info",
            "pool.task.queued",
            account_id=account_id,
            task_id=task.task_id,
            priority=priority,
            queue_depth=self._queue.qsize(),
        )
        return future


    async def _dispatch_loop(self) -> None:
        while not self._closing:
            worker = await self._idle.get()
            if self._closing:
                return
            _, _, item = await self._queue.get()
            asyncio.create_task(
                self._execute(worker, item),
                name=f"pool-task-{item.task.task_id}",
            )

    async def _execute(self, worker: _Worker, item: _QueuedTask) -> None:
        task = item.task
        item.attempts += 1
        worker.state = WorkerState.busy
        worker.current_account_id = task.account_id
        worker.current_task_started_at = datetime.now(timezone.utc)
        self._in_flight[task.task_id] = item

        healthy = True
        try:
            result = await asyncio.wait_for(
                self._roundtrip(worker, task),
                timeout=self.task_timeout_seconds,
            )
        except asyncio.TimeoutError:
            healthy = False
            worker.last_error = "task timed out"
            log_event(
                logger,
                "error",
                "pool.task.timeout",
                worker_id=worker.worker_id,
                account_id=task.account_id,
                timeout_seconds=self.task_timeout_seconds,
            )
            result = self._failure(item, f"timed out after {self.task_timeout_seconds}s")
        except (EOFError, OSError, ConnectionError) as exc:
            healthy = False
            worker.last_error = str(exc)
            log_event(
                logger,
                "error",
                "pool.worker.lost",
                worker_id=worker.worker_id,
                account_id=task.account_id,
                error=str(exc),
            )
            result = self._failure(item, f"worker lost: {exc}")

        self._in_flight.pop(task.task_id, None)
        worker.current_account_id = None
        worker.current_task_started_at = None
        result.worker_id = worker.worker_id

        if result.ok:
            worker.tasks_completed += 1
        else:
            worker.tasks_failed += 1
            worker.last_error = result.error

        if not healthy:
            worker.state = WorkerState.restarting
            healthy = await self._restart(worker)

        if healthy:
            worker.state = WorkerState.idle
            self._idle.put_nowait(worker)
        else:
            worker.state = WorkerState.failed

        if not result.ok and item.attempts <= self.max_task_retries:
            log_event(
                logger,
                "warning",
                "pool.task.retrying",
                account_id=task.account_id,
                attempt=item.attempts,
                max_retries=self.max_task_retries,
            )
            self._queue.put_nowait((item.priority, next(self._sequence), item))
            return

        await self._complete(item, result)

    async def _roundtrip(self, worker: _Worker, task: SyncTask) -> SyncResult:
        await asyncio.to_thread(worker.connection.send, task)
        return await asyncio.to_thread(worker.connection.recv)

    async def _complete(self, item: _QueuedTask, result: SyncResult) -> None:
        if self.on_result is not None:
            try:
                await self.on_result(result)
            except Exception as exc:
                log_event(
                    logger,
                    "error",
                    "pool.result.handler_failed",
                    account_id=result.account_id,
                    error=str(exc),
                )
        self._resolve(item, result)

    def _resolve(self, item: _QueuedTask, result: SyncResult) -> None:
        account_id = item.task.account_id
        if self._pending_accounts.get(account_id) is item.future:
            self._pending_accounts.pop(account_id, None)
        if not item.future.done():
            item.future.set_result(result)

    def _failure(self, item: _QueuedTask, error: str) -> SyncResult:
        return SyncResult(
            task_id=item.task.task_id,
            account_id=item.task.account_id,
            ok=False,
            error=error,
            sync_run_id=item.task.sync_run_id,
        )


    async def _spawn(self, worker: _Worker) -> bool:
        parent_conn, child_conn = self._mp.Pipe()
        process = self._mp.Process(
            target=self._worker_target,
            args=(worker.worker_id, worker.terminal_path, child_conn),
            kwargs={
                "init_timeout_ms": self.init_timeout_ms,
                "login_timeout_ms": self.login_timeout_ms,
                "log_level": self.log_level,
                "log_json": self.log_json,
                "with_mae_mfe": self.enrich_mae_mfe,
            },
            daemon=True,
            name=f"mt5-worker-{worker.worker_id}",
        )
        process.start()
        child_conn.close()

        worker.process = process
        worker.connection = parent_conn
        worker.state = WorkerState.starting

        try:
            ready: WorkerReady = await asyncio.wait_for(
                asyncio.to_thread(parent_conn.recv),
                timeout=self.worker_start_timeout_seconds,
            )
        except (asyncio.TimeoutError, EOFError, OSError) as exc:
            worker.last_error = f"worker did not start: {exc}"
            log_event(
                logger,
                "error",
                "pool.worker.start_failed",
                worker_id=worker.worker_id,
                terminal_path=worker.terminal_path,
                error=worker.last_error,
            )
            await self._terminate(worker)
            worker.state = WorkerState.failed
            return False

        if not ready.ok:
            worker.last_error = ready.error
            log_event(
                logger,
                "error",
                "pool.worker.init_failed",
                worker_id=worker.worker_id,
                terminal_path=worker.terminal_path,
                error=ready.error,
            )
            await self._terminate(worker)
            worker.state = WorkerState.failed
            return False

        worker.last_error = None
        return True

    async def _restart(self, worker: _Worker) -> bool:
        await self._terminate(worker)
        for attempt in range(1, _RESTART_ATTEMPTS + 1):
            worker.restarts += 1
            log_event(
                logger,
                "info",
                "pool.worker.restarting",
                worker_id=worker.worker_id,
                attempt=attempt,
            )
            if await self._spawn(worker):
                return True
            await asyncio.sleep(_RESTART_BACKOFF_SECONDS * attempt)
        log_event(logger, "error", "pool.worker.restart_failed", worker_id=worker.worker_id)
        return False

    async def _terminate(self, worker: _Worker) -> None:
        process, connection = worker.process, worker.connection
        worker.process, worker.connection = None, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if process is None:
            return
        try:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 5)
        except Exception as exc:
            log_event(
                logger,
                "warning",
                "pool.worker.terminate_failed",
                worker_id=worker.worker_id,
                error=str(exc),
            )


    def status(self) -> PoolStatus:
        hard = sum(
            1
            for priority, _, _ in getattr(self._queue, "_queue", [])
            if priority == PRIORITY_HARD
        )
        return PoolStatus(
            workers=[worker.status() for worker in self._workers],
            queue_depth=self._queue.qsize(),
            hard_sync_queue_depth=hard,
            idle_workers=sum(
                1 for worker in self._workers if worker.state == WorkerState.idle
            ),
        )

    @property
    def healthy_workers(self) -> int:
        return sum(
            1
            for worker in self._workers
            if worker.state in (WorkerState.idle, WorkerState.busy)
        )
