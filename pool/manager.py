from __future__ import annotations

import asyncio
import logging
import multiprocessing
import uuid
from dataclasses import dataclass
from itertools import count
from typing import Any, Awaitable, Callable

from domain.enums import SyncKind
from mt5api.terminal import AUTHORIZATION_FAILED, StartGate
from utils.logging import log_event

from .protocol import PRIORITY_HARD, PRIORITY_SCHEDULED, PoolStatus, SyncResult, SyncTask, WorkerState
from .worker import worker_main
from .worker_handle import WorkerHandle

logger = logging.getLogger(__name__)

ResultHandler = Callable[[SyncResult], Awaitable[None]]

_SHUTDOWN_DRAIN_SECONDS = 30.0
_REAP_INTERVAL_SECONDS = 60.0


@dataclass
class _QueuedTask:
    task: SyncTask
    priority: int
    future: "asyncio.Future[SyncResult]"
    attempts: int = 0
    reassignments: int = 0
    max_retries: int | None = None


class PoolManager:
    def __init__(
        self,
        terminal_paths: list[str],
        *,
        on_result: ResultHandler | None = None,
        task_timeout_seconds: float = 300.0,
        worker_start_timeout_seconds: float = 120.0,
        max_task_retries: int = 2,
        init_timeout_ms: int = 30000,
        login_timeout_ms: int = 30000,
        terminal_portable: bool = False,
        history_settle_timeout_seconds: float = 30.0,
        prune_cache_after_sync: bool = False,
        log_level: str = "INFO",
        log_json: bool = True,
        enrich_mae_mfe: bool = True,
        worker_target: Callable[..., None] = worker_main,
        mp_context: Any = None,
    ) -> None:
        self.terminal_paths = terminal_paths
        self.on_result = on_result
        self.task_timeout_seconds = task_timeout_seconds
        self.max_task_retries = max_task_retries

        self._mp = mp_context or multiprocessing.get_context("spawn")
        self._worker_target = worker_target
        self._worker_start_timeout = worker_start_timeout_seconds
        self._worker_kwargs = {
            "init_timeout_ms": init_timeout_ms,
            "login_timeout_ms": login_timeout_ms,
            "log_level": log_level,
            "log_json": log_json,
            "with_mae_mfe": enrich_mae_mfe,
            "portable": terminal_portable,
            "history_settle_timeout_seconds": history_settle_timeout_seconds,
            "prune_cache_after_sync": prune_cache_after_sync,
            "start_gate": StartGate(self._mp),
        }

        self._workers: list[WorkerHandle] = []
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._idle: asyncio.Queue[WorkerHandle] = asyncio.Queue()
        self._sequence = count()
        self._dispatcher: asyncio.Task | None = None
        self._reaper: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()
        self._pending: dict[str, tuple[str | None, "asyncio.Future[SyncResult]"]] = {}
        self._closing = False

    async def start(self) -> None:
        self._workers = [
            WorkerHandle(
                worker_id=f"w{index + 1}",
                terminal_path=path,
                mp_context=self._mp,
                target=self._worker_target,
                worker_kwargs=self._worker_kwargs,
                start_timeout_seconds=self._worker_start_timeout,
            )
            for index, path in enumerate(self.terminal_paths)
        ]
        results = await asyncio.gather(*(worker.spawn() for worker in self._workers), return_exceptions=True)
        for worker, ok in zip(self._workers, results):
            if ok is True:
                worker.state = WorkerState.idle
                self._idle.put_nowait(worker)

        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="pool-dispatcher")
        self._reaper = asyncio.create_task(self._reap_loop(), name="pool-reaper")
        log_event(
            logger, "info", "pool.started",
            workers=len(self._workers), ready=sum(1 for w in self._workers if w.state == WorkerState.idle),
        )

    async def stop(self) -> None:
        self._closing = True
        for task in (self._dispatcher, self._reaper):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._dispatcher = self._reaper = None

        for worker in self._workers:
            await worker.terminate()
            worker.state = WorkerState.stopped

        if self._running:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._running), return_exceptions=True), timeout=_SHUTDOWN_DRAIN_SECONDS
                )
            except asyncio.TimeoutError:
                log_event(
                    logger, "warning", "pool.stop.drain_timeout",
                    pending=len(self._running), timeout_seconds=_SHUTDOWN_DRAIN_SECONDS,
                )

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
        kind: SyncKind = SyncKind.scheduled,
        sync_run_id: str | None = None,
        connect_timeout_ms: int | None = None,
        already_measured: frozenset[str] = frozenset(),
        max_retries: int | None = None,
    ) -> "asyncio.Future[SyncResult]":
        future: "asyncio.Future[SyncResult]" = asyncio.get_running_loop().create_future()
        item = _QueuedTask(
            task=SyncTask(
                task_id=str(uuid.uuid4()),
                account_id=account_id,
                login=login,
                password=password,
                server=server,
                kind=kind,
                sync_run_id=sync_run_id,
                connect_timeout_ms=connect_timeout_ms,
                already_measured=already_measured,
            ),
            priority=priority,
            future=future,
            max_retries=max_retries,
        )
        self._pending[account_id] = (sync_run_id, future)
        self._queue.put_nowait((priority, next(self._sequence), item))
        log_event(
            logger, "info", "pool.task.queued",
            account_id=account_id, task_id=item.task.task_id, priority=priority, queue_depth=self._queue.qsize(),
        )
        return future

    def pending_for(self, account_id: str) -> "tuple[str | None, asyncio.Future[SyncResult]] | None":
        return self._pending.get(account_id)

    async def _dispatch_loop(self) -> None:
        while not self._closing:
            try:
                worker = await self._idle.get()
                if self._closing:
                    return
                _, _, item = await self._queue.get()
                task = asyncio.create_task(self._execute(worker, item), name=f"pool-task-{item.task.task_id}")
                self._running.add(task)
                task.add_done_callback(self._running.discard)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(logger, "error", "pool.dispatch.failed", error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(1.0)

    async def _reap_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(_REAP_INTERVAL_SECONDS)
                for worker in self._workers:
                    if self._closing:
                        return
                    if worker.state is not WorkerState.failed:
                        continue
                    log_event(logger, "info", "pool.worker.reaping", worker_id=worker.worker_id, error=worker.last_error)
                    if await worker.restart():
                        worker.state = WorkerState.idle
                        self._idle.put_nowait(worker)
                        log_event(logger, "info", "pool.worker.recovered", worker_id=worker.worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(logger, "error", "pool.reap.failed", error=f"{type(exc).__name__}: {exc}")

    async def _execute(self, worker: WorkerHandle, item: _QueuedTask) -> None:
        try:
            await self._execute_guarded(worker, item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                logger, "error", "pool.task.crashed",
                worker_id=worker.worker_id, account_id=item.task.account_id, error=f"{type(exc).__name__}: {exc}",
            )
            worker.state = WorkerState.failed
            await self._complete(item, self._failure(item, f"pool error: {type(exc).__name__}: {exc}"))

    async def _execute_guarded(self, worker: WorkerHandle, item: _QueuedTask) -> None:
        task = item.task
        item.attempts += 1
        worker.claim(task)

        result, worker_healthy = await self._roundtrip(worker, item)
        terminal_lost = worker_healthy and result.terminal_lost
        if terminal_lost:
            log_event(
                logger, "warning", "pool.worker.terminal_lost",
                worker_id=worker.worker_id, account_id=task.account_id, error_code=result.error_code,
            )
            worker_healthy = False

        result.worker_id = worker.worker_id
        worker.release(result)

        if not worker_healthy:
            worker.state = WorkerState.restarting
            worker_healthy = await worker.restart()
        if worker_healthy:
            worker.state = WorkerState.idle
            self._idle.put_nowait(worker)
        else:
            worker.state = WorkerState.failed

        if terminal_lost and task.kind is not SyncKind.initial and item.reassignments < len(self._workers):
            item.reassignments += 1
            item.attempts -= 1
            log_event(logger, "info", "pool.task.reassigning", account_id=task.account_id, reassignments=item.reassignments)
            self._queue.put_nowait((item.priority, next(self._sequence), item))
            return

        if self._should_retry(item, result):
            self._queue.put_nowait((item.priority, next(self._sequence), item))
            return
        await self._complete(item, result)

    async def _roundtrip(self, worker: WorkerHandle, item: _QueuedTask) -> tuple[SyncResult, bool]:
        try:
            result = await asyncio.wait_for(worker.roundtrip(item.task), timeout=self.task_timeout_seconds)
            return result, True
        except asyncio.TimeoutError:
            worker.last_error = "task timed out"
            log_event(
                logger, "error", "pool.task.timeout",
                worker_id=worker.worker_id, account_id=item.task.account_id, timeout_seconds=self.task_timeout_seconds,
            )
            return self._failure(item, f"timed out after {self.task_timeout_seconds}s"), False
        except (EOFError, OSError, ConnectionError) as exc:
            worker.last_error = str(exc)
            log_event(logger, "error", "pool.worker.lost", worker_id=worker.worker_id, account_id=item.task.account_id, error=str(exc))
            return self._failure(item, f"worker lost: {exc}"), False

    def _should_retry(self, item: _QueuedTask, result: SyncResult) -> bool:
        if result.ok:
            return False
        if result.error_code == AUTHORIZATION_FAILED:
            log_event(logger, "info", "pool.task.not_retrying", account_id=item.task.account_id, error_code=result.error_code)
            return False
        limit = self.max_task_retries if item.max_retries is None else item.max_retries
        if item.attempts > limit:
            return False
        log_event(logger, "warning", "pool.task.retrying", account_id=item.task.account_id, attempt=item.attempts, max_retries=limit)
        return True

    async def _complete(self, item: _QueuedTask, result: SyncResult) -> None:
        if self.on_result is not None:
            try:
                await self.on_result(result)
            except Exception as exc:
                log_event(logger, "error", "pool.result.handler_failed", account_id=result.account_id, error=str(exc))
        self._resolve(item, result)

    def _resolve(self, item: _QueuedTask, result: SyncResult) -> None:
        pending = self._pending.get(item.task.account_id)
        if pending is not None and pending[1] is item.future:
            self._pending.pop(item.task.account_id, None)
        if not item.future.done():
            item.future.set_result(result)

    def _failure(self, item: _QueuedTask, error: str) -> SyncResult:
        return SyncResult(
            task_id=item.task.task_id,
            account_id=item.task.account_id,
            ok=False,
            error=error,
            sync_run_id=item.task.sync_run_id,
            kind=item.task.kind,
        )

    @property
    def dispatcher_alive(self) -> bool:
        return self._dispatcher is not None and not self._dispatcher.done()

    @property
    def healthy_workers(self) -> int:
        return sum(1 for worker in self._workers if worker.state in (WorkerState.idle, WorkerState.busy))

    def status(self) -> PoolStatus:
        queued = getattr(self._queue, "_queue", [])
        return PoolStatus(
            workers=[worker.status() for worker in self._workers],
            queue_depth=self._queue.qsize(),
            hard_sync_queue_depth=sum(1 for priority, _, _ in queued if priority == PRIORITY_HARD),
            idle_workers=sum(1 for worker in self._workers if worker.state == WorkerState.idle),
        )
