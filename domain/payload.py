from __future__ import annotations

from typing import List, Optional

from pydantic import Field

from .fx import (
    FXAccountInfo,
    FXBase,
    FXOpenPosition,
    FXPosition,
    Transaction,
)


class SyncPayload(FXBase):
    account_info: FXAccountInfo = Field(default_factory=FXAccountInfo, alias="AccountInfo")
    equity: float = Field(default=0.0, alias="Equity")
    positions: List[FXPosition] = Field(default_factory=list, alias="Positions")
    open_positions: List[FXOpenPosition] = Field(default_factory=list, alias="OpenPositions")
    transactions: List[Transaction] = Field(default_factory=list, alias="Transactions")
    server_utc_offset_minutes: Optional[int] = Field(
        default=None, alias="ServerUtcOffsetMinutes"
    )
