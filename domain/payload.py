from __future__ import annotations

from typing import Any, List

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
    # The trade server's clock as [server label, offset minutes] pairs, one
    # per daylight saving switch. Empty when it could not be measured, and then
    # every *Utc field is null.
    server_clock: List[List[Any]] = Field(default_factory=list, alias="ServerClock")
