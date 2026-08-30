from __future__ import annotations

from enum import Enum


class AccountStatus(str, Enum):
    """Whether the pool can currently reach the account.

    Two states only: an account either syncs, or its connection is broken. How
    an account came to be unreachable lives in ``last_error``; whether anyone
    wants it synced at all is the separate ``enabled`` flag.
    """

    active = "active"
    error_connection = "error_connection"


class SyncKind(str, Enum):
    initial = "initial"
    hard = "hard"
    scheduled = "scheduled"


class SyncStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
