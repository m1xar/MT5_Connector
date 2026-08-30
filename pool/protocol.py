from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from domain.payload import SyncPayload

PRIORITY_HARD = 0
PRIORITY_SCHEDULED = 1


class WorkerState(str, Enum):
    starting = "starting"
    idle = "idle"
    busy = "busy"
    restarting = "restarting"
    failed = "failed"
    stopped = "stopped"


@dataclass(slots=True)
class SyncTask:

    task_id: str
    account_id: str
    login: int
    password: str
    server: str
    sync_run_id: Optional[str] = None
    # Overrides the worker default; the first sync of a new account gets a
    # longer one because that connection has never been proven to work.
    connect_timeout_ms: Optional[int] = None
    # Positions whose MAE/MFE is already stored; the worker skips pricing
    # them rather than re-fetching candles from years ago.
    already_measured: frozenset = frozenset()


@dataclass(slots=True)
class SyncResult:
    task_id: str
    account_id: str
    ok: bool
    payload: Optional[SyncPayload] = None
    error: Optional[str] = None
    error_code: Optional[int] = None
    duration_ms: int = 0
    worker_id: Optional[str] = None
    sync_run_id: Optional[str] = None


@dataclass(slots=True)
class WorkerReady:

    worker_id: str
    ok: bool
    error: Optional[str] = None


@dataclass(slots=True)
class WorkerStatus:
    worker_id: str
    terminal_path: str
    state: WorkerState
    pid: Optional[int] = None
    current_account_id: Optional[str] = None
    current_task_started_at: Optional[datetime] = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    restarts: int = 0
    last_error: Optional[str] = None


@dataclass(slots=True)
class PoolStatus:
    workers: list[WorkerStatus] = field(default_factory=list)
    queue_depth: int = 0
    hard_sync_queue_depth: int = 0
    idle_workers: int = 0
