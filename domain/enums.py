from __future__ import annotations

from enum import Enum


class AccountStatus(str, Enum):
    pending = "pending"
    active = "active"
    error = "error"
    disabled = "disabled"


class SyncKind(str, Enum):
    hard = "hard"
    scheduled = "scheduled"


class SyncStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
