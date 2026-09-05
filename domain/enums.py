from __future__ import annotations

from enum import Enum


class AccountStatus(str, Enum):
    active = "active"
    error_connection = "error_connection"


class SyncKind(str, Enum):
    initial = "initial"
    hard = "hard"
    scheduled = "scheduled"
