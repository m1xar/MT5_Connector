from __future__ import annotations

import logging
import time
from typing import Any

from mt5api.cache import prune_price_cache
from mt5api.enrichment import enrich_mae_mfe
from mt5api.fetch import fetch_candles, fetch_history, history_withheld
from mt5api.payload import build_sync_payload
from mt5api.terminal import MT5Terminal, StartGate, TerminalError
from utils.logging import configure_logging, log_event

from .protocol import SyncResult, SyncTask

logger = logging.getLogger(__name__)


def worker_main(
    worker_id: str,
    terminal_path: str,
    connection: Any,
    *,
    init_timeout_ms: int,
    login_timeout_ms: int,
    log_level: str,
    log_json: bool,
    with_mae_mfe: bool,
    portable: bool,
    history_settle_timeout_seconds: float,
    prune_cache_after_sync: bool,
    start_gate: StartGate,
) -> None:
    configure_logging(log_level, log_json)
    terminal = MT5Terminal(
        terminal_path,
        init_timeout_ms=init_timeout_ms,
        login_timeout_ms=login_timeout_ms,
        portable=portable,
        start_gate=start_gate,
    )
    connection.send(worker_id)
    log_event(logger, "info", "worker.started", worker_id=worker_id, terminal_path=terminal_path)

    try:
        while True:
            try:
                task = connection.recv()
            except (EOFError, OSError):
                break
            if task is None:
                break
            connection.send(_run_task(terminal, worker_id, task, with_mae_mfe, history_settle_timeout_seconds))
            if prune_cache_after_sync:
                try:
                    prune_price_cache(terminal_path)
                except Exception as exc:
                    log_event(logger, "warning", "worker.cache.prune_failed", worker_id=worker_id, error=f"{type(exc).__name__}: {exc}")
    finally:
        terminal.shutdown()
        log_event(logger, "info", "worker.stopped", worker_id=worker_id)


def _run_task(
    terminal: MT5Terminal,
    worker_id: str,
    task: SyncTask,
    with_mae_mfe: bool,
    history_settle_timeout_seconds: float,
) -> SyncResult:
    started = time.perf_counter()
    result = SyncResult(
        task_id=task.task_id, account_id=task.account_id, ok=False,
        worker_id=worker_id, sync_run_id=task.sync_run_id, kind=task.kind,
    )
    try:
        terminal.connect(task.login, task.password, task.server, timeout_ms=task.connect_timeout_ms)
        history = fetch_history(terminal, settle_timeout_seconds=history_settle_timeout_seconds)
        result.history_withheld = history_withheld(history)
        payload = build_sync_payload(history)
        if with_mae_mfe and not result.history_withheld:
            enrich_mae_mfe(
                payload.positions,
                lambda symbol, interval, start, end: fetch_candles(terminal, symbol, interval, start, end),
                already_measured=task.already_measured,
            )
    except TerminalError as exc:
        if exc.terminal_lost:
            terminal.reset()
        else:
            terminal.forget_login()
        result.error, result.error_code, result.terminal_lost = str(exc), exc.code, exc.terminal_lost
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log_event(
            logger, "warning", "worker.task.failed",
            worker_id=worker_id, account_id=task.account_id, error=result.error,
            error_code=exc.code, terminal_lost=exc.terminal_lost,
        )
        return result
    except Exception as exc:
        terminal.forget_login()
        result.error = f"{type(exc).__name__}: {exc}"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log_event(logger, "error", "worker.task.crashed", worker_id=worker_id, account_id=task.account_id, error=result.error)
        return result

    result.ok, result.payload = True, payload
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    log_event(
        logger, "info", "worker.task.completed",
        worker_id=worker_id, account_id=task.account_id, positions=len(payload.positions),
        history_withheld=result.history_withheld, duration_ms=result.duration_ms,
    )
    return result
