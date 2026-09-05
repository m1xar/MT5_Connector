from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from domain import fx


class SyncQueuedResponse(BaseModel):
    account_id: str
    status: str = "queued"
    queue_depth: int


class SyncResultResponse(BaseModel):
    account_id: str
    ok: bool
    error: Optional[str] = None
    duration_ms: int = 0
    worker_id: Optional[str] = None
    account_info: Optional[fx.FXAccountInfo] = None
    positions: List[fx.FXPosition] = []
    open_positions: List[fx.FXOpenPosition] = []
    transactions: List[fx.Transaction] = []


class PositionListResponse(BaseModel):
    account_id: str
    count: int
    positions: List[fx.FXPosition]


class OpenPositionListResponse(BaseModel):
    account_id: str
    count: int
    open_positions: List[fx.FXOpenPosition]


class TransactionListResponse(BaseModel):
    account_id: str
    count: int
    transactions: List[fx.Transaction]


class BalanceSnapshotListResponse(BaseModel):
    account_id: str
    count: int
    snapshots: List[fx.UserBalanceSnapshot]
