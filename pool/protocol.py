from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from domain.enums import SyncKind
from domain.fx import SyncPayload

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
    kind: SyncKind = SyncKind.scheduled
    sync_run_id: str | None = None
    connect_timeout_ms: int | None = None
    already_measured: frozenset[str] = frozenset()


@dataclass(slots=True)
class SyncResult:
    task_id: str
    account_id: str
    ok: bool
    payload: SyncPayload | None = None
    error: str | None = None
    error_code: int | None = None
    terminal_lost: bool = False
    duration_ms: int = 0
    worker_id: str | None = None
    sync_run_id: str | None = None
    kind: SyncKind = SyncKind.scheduled
