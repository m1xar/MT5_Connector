from __future__ import annotations

import time
from typing import Any

from mt5api.api import build_sync_payload
from mt5api.enrichment import enrich_mae_mfe
from mt5api.executors import fetch_candles, fetch_history
from mt5api.terminal import MT5Terminal, TerminalError
from utils.logging import configure_logging, duration_ms_since, get_logger, log_event

from .protocol import SyncResult, SyncTask, WorkerReady

logger = get_logger(__name__)


def worker_main(
    worker_id: str,
    terminal_path: str,
    connection: Any,
    *,
    init_timeout_ms: int,
    login_timeout_ms: int,
    log_level: str,
    log_json: bool,
    with_mae_mfe: bool = True,
) -> None:
    configure_logging(log_level, log_json)

    terminal = MT5Terminal(
        terminal_path,
        init_timeout_ms=init_timeout_ms,
        login_timeout_ms=login_timeout_ms,
    )
    try:
        terminal.initialize()
    except TerminalError as exc:
        log_event(
            logger,
            "error",
            "worker.initialize.failed",
            worker_id=worker_id,
            terminal_path=terminal_path,
            error=str(exc),
        )
        connection.send(WorkerReady(worker_id=worker_id, ok=False, error=str(exc)))
        return

    connection.send(WorkerReady(worker_id=worker_id, ok=True))
    log_event(logger, "info", "worker.started", worker_id=worker_id, terminal_path=terminal_path)

    try:
        while True:
            try:
                task = connection.recv()
            except (EOFError, OSError):
                break
            if task is None:
                break
            connection.send(_run_task(terminal, worker_id, task, with_mae_mfe))
    finally:
        terminal.shutdown()
        log_event(logger, "info", "worker.stopped", worker_id=worker_id)


def _run_task(
    terminal: MT5Terminal,
    worker_id: str,
    task: SyncTask,
    with_mae_mfe: bool = True,
) -> SyncResult:
    started = time.perf_counter()
    try:
        terminal.login(task.login, task.password, task.server)
        payload = build_sync_payload(fetch_history(terminal))
        if with_mae_mfe:
            enrich_mae_mfe(
                payload.positions,
                lambda symbol, interval, start, end: fetch_candles(
                    terminal, symbol, interval, start, end
                ),
            )
    except TerminalError as exc:
        terminal.forget_login()
        log_event(
            logger,
            "warning",
            "worker.task.failed",
            worker_id=worker_id,
            account_id=task.account_id,
            error=str(exc),
            error_code=exc.code,
        )
        return SyncResult(
            task_id=task.task_id,
            account_id=task.account_id,
            ok=False,
            error=str(exc),
            error_code=exc.code,
            duration_ms=duration_ms_since(started),
            worker_id=worker_id,
            sync_run_id=task.sync_run_id,
        )
    except Exception as exc:
        terminal.forget_login()
        log_event(
            logger,
            "error",
            "worker.task.crashed",
            worker_id=worker_id,
            account_id=task.account_id,
            error=str(exc),
        )
        return SyncResult(
            task_id=task.task_id,
            account_id=task.account_id,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=duration_ms_since(started),
            worker_id=worker_id,
            sync_run_id=task.sync_run_id,
        )

    duration_ms = duration_ms_since(started)
    log_event(
        logger,
        "info",
        "worker.task.completed",
        worker_id=worker_id,
        account_id=task.account_id,
        positions=len(payload.positions),
        duration_ms=duration_ms,
    )
    return SyncResult(
        task_id=task.task_id,
        account_id=task.account_id,
        ok=True,
        payload=payload,
        duration_ms=duration_ms,
        worker_id=worker_id,
        sync_run_id=task.sync_run_id,
    )
