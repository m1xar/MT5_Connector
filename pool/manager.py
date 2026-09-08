from __future__ import annotations

import asyncio
import logging
import multiprocessing
import uuid
from dataclasses import dataclass, field
from itertools import count
from typing import Awaitable, Callable

from domain.enums import SyncKind
from mt5api.proxy import Proxy
from mt5api.terminal import AUTHORIZATION_FAILED, StartGate
from services.proxy_service import ProxyRegistry
from utils.logging import log_event

from .protocol import PRIORITY_HARD, PRIORITY_SCHEDULED, SyncResult, SyncTask, WorkerState
from .worker import worker_main
from .worker_handle import WorkerHandle

logger = logging.getLogger(__name__)

ResultHandler = Callable[[SyncResult], Awaitable[None]]

_SHUTDOWN_DRAIN_SECONDS = 30.0
_REAP_INTERVAL_SECONDS = 60.0
_SPAWN_BATCH = 4


@dataclass
class _QueuedTask:
    task: SyncTask
    priority: int
    future: asyncio.Future[SyncResult]
    max_retries: int | None
    attempts: int = 0
    proxy_rotations: int = 0


@dataclass(slots=True)
class PoolStatus:
    workers: list[WorkerHandle] = field(default_factory=list)
    queue_depth: int = 0
    hard_sync_queue_depth: int = 0
    idle_workers: int = 0


class PoolManager:
    def __init__(
        self,
        terminal_paths: list[str],
        *,
        task_timeout_seconds: float,
        worker_start_timeout_seconds: float,
        max_task_retries: int,
        init_timeout_ms: int,
        login_timeout_ms: int,
        terminal_portable: bool,
        history_settle_timeout_seconds: float,
        prune_cache_after_sync: bool,
        log_level: str,
        log_json: bool,
        enrich_mae_mfe: bool,
        proxies: dict[str, Proxy | None] | None = None,
        proxy_registry: ProxyRegistry | None = None,
        proxy_probe_timeout_seconds: float = 10.0,
        proxy_recheck_minutes: int = 60,
    ) -> None:
        self.terminal_paths = terminal_paths
        self._proxies = proxies
        self._proxy_registry = proxy_registry
        self._proxy_recheck_seconds = proxy_recheck_minutes * 60.0
        self._reproxy_due = 0.0
        self.on_result: ResultHandler | None = None
        self.task_timeout_seconds = task_timeout_seconds
        self.max_task_retries = max_task_retries

        self._mp = multiprocessing.get_context("spawn")
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
            "managed": proxies is not None,
            "proxy_probe_timeout_seconds": proxy_probe_timeout_seconds,
        }

        self._workers: dict[str, WorkerHandle] = {}
        self._sequence = count()
        self._dispatchers: list[asyncio.Task] = []
        self._reaper: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()
        self._pending: dict[str, asyncio.Future[SyncResult]] = {}
        self._closing = False

    async def start(self) -> None:
        self._workers = {
            path: WorkerHandle(
                worker_id=f"w{index + 1}",
                terminal_path=path,
                mp_context=self._mp,
                target=worker_main,
                worker_kwargs=self._worker_kwargs,
                start_timeout_seconds=self._worker_start_timeout,
                proxy=(self._proxies or {}).get(path),
            )
            for index, path in enumerate(self.terminal_paths)
        }
        workers = list(self._workers.values())
        await self._spawn_in_batches(workers, WorkerHandle.spawn)

        self._dispatchers = [
            asyncio.create_task(self._dispatch_loop(worker), name=f"pool-dispatcher-{worker.worker_id}")
            for worker in workers
        ]
        self._reproxy_due = asyncio.get_running_loop().time() + self._proxy_recheck_seconds
        self._reaper = asyncio.create_task(self._reap_loop(), name="pool-reaper")
        log_event(
            logger, "info", "pool.started",
            workers=len(workers), ready=sum(1 for worker in workers if worker.state is WorkerState.idle),
        )

    async def stop(self) -> None:
        self._closing = True
        for task in [*self._dispatchers, self._reaper]:
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._dispatchers, self._reaper = [], None

        await asyncio.gather(*(worker.terminate() for worker in self._workers.values()))
        for worker in self._workers.values():
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

        for worker in self._workers.values():
            while not worker.queue.empty():
                _, _, item = worker.queue.get_nowait()
                self._resolve(item, self._failure(worker, item, "pool is shutting down"))
        log_event(logger, "info", "pool.stopped")

    def submit(
        self,
        account_id: str,
        login: int,
        password: str,
        server: str,
        *,
        terminal_path: str,
        kind: SyncKind,
        sync_run_id: str,
        connect_timeout_ms: int | None = None,
        already_measured: frozenset[str] = frozenset(),
    ) -> asyncio.Future[SyncResult]:
        worker = self._workers[terminal_path]
        future: asyncio.Future[SyncResult] = asyncio.get_running_loop().create_future()
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
            priority=PRIORITY_SCHEDULED if kind is SyncKind.scheduled else PRIORITY_HARD,
            future=future,
            max_retries=0 if kind is SyncKind.initial else None,
        )
        if self._closing:
            future.set_result(self._failure(worker, item, "pool is shutting down"))
            return future
        self._pending.setdefault(account_id, future)
        self._enqueue(worker, item)
        log_event(
            logger, "info", "pool.task.queued",
            account_id=account_id, task_id=item.task.task_id, worker_id=worker.worker_id,
            priority=item.priority, queue_depth=worker.queue_depth,
        )
        return future

    def pending_for(self, account_id: str) -> asyncio.Future[SyncResult] | None:
        return self._pending.get(account_id)

    def queue_depth_for(self, terminal_path: str) -> int:
        return self._workers[terminal_path].queue_depth

    def _enqueue(self, worker: WorkerHandle, item: _QueuedTask) -> None:
        if item.priority == PRIORITY_HARD:
            worker.hard_queued += 1
        worker.queue.put_nowait((item.priority, next(self._sequence), item))

    async def _spawn_in_batches(
        self, workers: list[WorkerHandle], spawn: Callable[[WorkerHandle], Awaitable[bool]]
    ) -> None:
        for start in range(0, len(workers), _SPAWN_BATCH):
            batch = workers[start:start + _SPAWN_BATCH]
            results = await asyncio.gather(*(spawn(worker) for worker in batch), return_exceptions=True)
            for worker, outcome in zip(batch, results):
                if isinstance(outcome, BaseException):
                    worker.last_error = f"{type(outcome).__name__}: {outcome}"
                    worker.mark_failed()
            if self._closing:
                return

    async def _dispatch_loop(self, worker: WorkerHandle) -> None:
        while not self._closing:
            item: _QueuedTask | None = None
            try:
                await worker.ready.wait()
                _, _, item = await worker.queue.get()
                await worker.ready.wait()
                if item.priority == PRIORITY_HARD:
                    worker.hard_queued -= 1
                running = asyncio.create_task(self._execute(worker, item), name=f"pool-task-{item.task.task_id}")
                item = None
                self._running.add(running)
                running.add_done_callback(self._running.discard)
                await asyncio.shield(running)
            except asyncio.CancelledError:
                if item is not None:
                    self._resolve(item, self._failure(worker, item, "pool is shutting down"))
                raise
            except Exception as exc:
                if item is not None:
                    self._resolve(item, self._failure(worker, item, f"pool error: {type(exc).__name__}: {exc}"))
                log_event(
                    logger, "error", "pool.dispatch.failed",
                    worker_id=worker.worker_id, error=f"{type(exc).__name__}: {exc}",
                )
                await asyncio.sleep(1.0)

    async def _reap_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(_REAP_INTERVAL_SECONDS)
                if self._proxy_registry and asyncio.get_running_loop().time() >= self._reproxy_due:
                    self._reproxy_due = asyncio.get_running_loop().time() + self._proxy_recheck_seconds
                    await self._reproxy_idle()
                failed = [worker for worker in self._workers.values() if worker.state is WorkerState.failed]
                if not failed:
                    continue
                for worker in failed:
                    log_event(logger, "info", "pool.worker.reaping", worker_id=worker.worker_id, error=worker.last_error)
                await self._spawn_in_batches(failed, WorkerHandle.restart)
                for worker in failed:
                    if worker.state is WorkerState.idle:
                        log_event(logger, "info", "pool.worker.recovered", worker_id=worker.worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(logger, "error", "pool.reap.failed", error=f"{type(exc).__name__}: {exc}")

    async def _reproxy_idle(self) -> None:
        for worker in self._workers.values():
            if worker.proxy is not None or worker.state is not WorkerState.idle or self._closing:
                continue
            proxy = await self._proxy_registry.rotate(worker.terminal_path, None)
            if proxy is None or worker.state is not WorkerState.idle:
                continue
            worker.proxy = proxy
            worker.state = WorkerState.restarting
            worker.ready.clear()
            log_event(logger, "info", "pool.worker.reproxied", worker_id=worker.worker_id, proxy=proxy.endpoint)
            if await worker.restart():
                worker.mark_idle()
            else:
                worker.mark_failed()

    async def _execute(self, worker: WorkerHandle, item: _QueuedTask) -> None:
        try:
            await self._run_item(worker, item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                logger, "error", "pool.task.crashed",
                worker_id=worker.worker_id, account_id=item.task.account_id, error=f"{type(exc).__name__}: {exc}",
            )
            worker.mark_failed()
            await self._complete(item, self._failure(worker, item, f"pool error: {type(exc).__name__}: {exc}"))

    async def _run_item(self, worker: WorkerHandle, item: _QueuedTask) -> None:
        task = item.task
        item.attempts += 1
        worker.claim(task)

        result, worker_healthy = await self._roundtrip(worker, item)
        if worker_healthy and result.terminal_lost:
            log_event(
                logger, "warning", "pool.worker.terminal_lost",
                worker_id=worker.worker_id, account_id=task.account_id, error_code=result.error_code,
            )
            worker_healthy = False

        worker.release(result)
        if self._closing:
            self._resolve(item, self._failure(worker, item, "pool is shutting down"))
            return

        if result.proxy_dead and self._proxy_registry is not None:
            dead = worker.proxy
            worker.proxy = await self._proxy_registry.rotate(worker.terminal_path, dead)
            log_event(
                logger, "warning", "pool.worker.proxy_rotated",
                worker_id=worker.worker_id, dead=dead.endpoint if dead else None,
                proxy=worker.proxy.endpoint if worker.proxy else None,
            )
            worker_healthy = False

        if not worker_healthy:
            worker_healthy = await worker.restart()
        if worker_healthy:
            worker.mark_idle()
        else:
            worker.mark_failed()

        if self._should_retry(item, result):
            self._enqueue(worker, item)
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
            return self._failure(worker, item, f"timed out after {self.task_timeout_seconds}s"), False
        except (EOFError, OSError, ConnectionError) as exc:
            worker.last_error = f"{type(exc).__name__}: {exc}"
            log_event(
                logger, "error", "pool.worker.lost",
                worker_id=worker.worker_id, account_id=item.task.account_id, error=worker.last_error,
            )
            return self._failure(worker, item, f"worker lost: {exc}"), False

    def _should_retry(self, item: _QueuedTask, result: SyncResult) -> bool:
        if result.ok:
            return False
        if result.error_code == AUTHORIZATION_FAILED:
            log_event(logger, "info", "pool.task.not_retrying", account_id=item.task.account_id, error_code=result.error_code)
            return False
        if result.proxy_dead and item.proxy_rotations == 0:
            item.proxy_rotations += 1
            item.attempts -= 1
            log_event(logger, "info", "pool.task.retrying_after_proxy", account_id=item.task.account_id)
            return True
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
                log_event(
                    logger, "error", "pool.result.handler_failed",
                    account_id=result.account_id, error=f"{type(exc).__name__}: {exc}",
                )
        self._resolve(item, result)

    def _resolve(self, item: _QueuedTask, result: SyncResult) -> None:
        if self._pending.get(item.task.account_id) is item.future:
            self._pending.pop(item.task.account_id, None)
        if not item.future.done():
            item.future.set_result(result)

    def _failure(self, worker: WorkerHandle, item: _QueuedTask, error: str) -> SyncResult:
        return SyncResult(
            task_id=item.task.task_id,
            account_id=item.task.account_id,
            ok=False,
            error=error,
            worker_id=worker.worker_id,
            sync_run_id=item.task.sync_run_id,
            kind=item.task.kind,
        )

    @property
    def dispatcher_alive(self) -> bool:
        return bool(self._dispatchers) and all(not task.done() for task in self._dispatchers)

    @property
    def healthy_workers(self) -> int:
        return sum(1 for worker in self._workers.values() if worker.state in (WorkerState.idle, WorkerState.busy))

    def status(self) -> PoolStatus:
        workers = list(self._workers.values())
        return PoolStatus(
            workers=workers,
            queue_depth=sum(worker.queue_depth for worker in workers),
            hard_sync_queue_depth=sum(worker.hard_queued for worker in workers),
            idle_workers=sum(1 for worker in workers if worker.state is WorkerState.idle),
        )
