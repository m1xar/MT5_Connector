from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from utils.logging import get_logger, log_event

from .protocol import SyncResult, SyncTask, WorkerReady, WorkerState, WorkerStatus

logger = get_logger(__name__)

_RESTART_ATTEMPTS = 3
_RESTART_BACKOFF_SECONDS = 5.0
# How long a worker gets to close its terminal before it is killed outright.
_GRACEFUL_STOP_SECONDS = 5.0
_TERMINATE_JOIN_SECONDS = 10.0
_KILL_JOIN_SECONDS = 5.0


@dataclass
class WorkerHandle:
    """One worker process, and everything about keeping it alive.

    The pool decides *what* runs; this decides *where*. Spawning, the pipe, the
    restart ladder and the counters all belong to one process, so they live
    together rather than being spread through the dispatcher.
    """

    worker_id: str
    terminal_path: str
    mp_context: Any
    target: Callable[..., None]
    worker_kwargs: dict[str, Any]
    start_timeout_seconds: float

    state: WorkerState = WorkerState.starting
    process: Any = None
    connection: Any = None
    current_account_id: Optional[str] = None
    current_task_started_at: Optional[datetime] = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    restarts: int = 0
    last_error: Optional[str] = None

    async def spawn(self) -> bool:
        # Starting a process can fail outright - the OS refuses, or the target
        # will not pickle. Reported like any other failed start, because the
        # caller is a restart ladder that has to keep its footing.
        try:
            parent_conn, child_conn = self.mp_context.Pipe()
            process = self.mp_context.Process(
                target=self.target,
                args=(self.worker_id, self.terminal_path, child_conn),
                kwargs=self.worker_kwargs,
                daemon=True,
                name=f"mt5-worker-{self.worker_id}",
            )
            process.start()
            child_conn.close()
        except Exception as exc:
            return await self._start_failed(
                f"worker would not spawn: {type(exc).__name__}: {exc}"
            )

        self.process = process
        self.connection = parent_conn
        self.state = WorkerState.starting

        try:
            ready: WorkerReady = await asyncio.wait_for(
                asyncio.to_thread(parent_conn.recv),
                timeout=self.start_timeout_seconds,
            )
        except (asyncio.TimeoutError, EOFError, OSError) as exc:
            return await self._start_failed(f"worker did not start: {exc}")

        if not ready.ok:
            return await self._start_failed(ready.error or "worker reported not ready")

        self.last_error = None
        return True

    async def restart(self) -> bool:
        await self.terminate()
        for attempt in range(1, _RESTART_ATTEMPTS + 1):
            self.restarts += 1
            log_event(
                logger, "info", "pool.worker.restarting",
                worker_id=self.worker_id, attempt=attempt,
            )
            if await self.spawn():
                return True
            await asyncio.sleep(_RESTART_BACKOFF_SECONDS * attempt)
        log_event(logger, "error", "pool.worker.restart_failed", worker_id=self.worker_id)
        return False

    async def terminate(self) -> None:
        process, connection = self.process, self.connection
        self.process, self.connection = None, None

        # Ask the worker to stop before killing it. Only the worker can close
        # its terminal, and a killed process leaves terminal64.exe running.
        if connection is not None and process is not None:
            try:
                if process.is_alive():
                    connection.send(None)
                    await asyncio.to_thread(process.join, _GRACEFUL_STOP_SECONDS)
            except Exception:
                pass

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
            await asyncio.to_thread(process.join, _TERMINATE_JOIN_SECONDS)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, _KILL_JOIN_SECONDS)
        except Exception as exc:
            log_event(
                logger, "warning", "pool.worker.terminate_failed",
                worker_id=self.worker_id, error=str(exc),
            )

    async def roundtrip(self, task: SyncTask) -> SyncResult:
        # A handle torn down between dispatch and here has no pipe. Saying so
        # as a ConnectionError puts it in the same bucket as any other lost
        # worker, instead of an AttributeError nobody catches.
        connection = self.connection
        if connection is None:
            raise ConnectionError(f"worker {self.worker_id} has no open pipe")
        await asyncio.to_thread(connection.send, task)
        return await asyncio.to_thread(connection.recv)

    def claim(self, task: SyncTask) -> None:
        self.state = WorkerState.busy
        self.current_account_id = task.account_id
        self.current_task_started_at = datetime.now(timezone.utc)

    def release(self, result: SyncResult) -> None:
        self.current_account_id = None
        self.current_task_started_at = None
        if result.ok:
            self.tasks_completed += 1
        else:
            self.tasks_failed += 1
            self.last_error = result.error

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

    async def _start_failed(self, reason: str) -> bool:
        self.last_error = reason
        log_event(
            logger, "error", "pool.worker.start_failed",
            worker_id=self.worker_id, terminal_path=self.terminal_path, error=reason,
        )
        await self.terminate()
        self.state = WorkerState.failed
        return False
