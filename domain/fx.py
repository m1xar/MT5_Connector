from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"

EXEC_SIDE_BUY = "BUY"
EXEC_SIDE_SELL = "SELL"

STATUS_WIN = "win"
STATUS_LOSE = "lose"

ORDER_TYPE_MARKET = "MARKET"
ORDER_TYPE_LIMIT = "LIMIT"
ORDER_TYPE_STOP = "STOP"
ORDER_TYPE_STOP_LIMIT = "STOP_LIMIT"

ORDER_STATUS_FILLED = "FILLED"
ORDER_STATUS_ACCEPTED = "ACCEPTED"
ORDER_STATUS_REJECTED = "REJECTED"
ORDER_STATUS_EXPIRED = "EXPIRED"
ORDER_STATUS_CANCELLED = "CANCELLED"

TRANSACTION_TYPE_DEPOSIT = "DEPOSIT"
TRANSACTION_TYPE_WITHDRAWAL = "WITHDRAWAL"


class FXBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_timedelta="iso8601")


class FXTrade(FXBase):
    order_id: str = Field(default="", alias="OrderID")
    side: str = Field(default="", alias="Side")
    price: float = Field(default=0.0, alias="Price")
    amount: float = Field(default=0.0, alias="Amount")
    commission: float = Field(default=0.0, alias="Commission")
    profit: float = Field(default=0.0, alias="Profit")
    done_at: Optional[datetime] = Field(default=None, alias="DoneAt")


class FXOrder(FXBase):
    id: str = Field(default="", alias="ID")
    position_id: str = Field(default="", alias="PositionID")
    type: str = Field(default=ORDER_TYPE_MARKET, alias="Type")
    status: str = Field(default=ORDER_STATUS_FILLED, alias="Status")
    side: str = Field(default="", alias="Side")
    amount: float = Field(default=0.0, alias="Amount")
    amount_filled: float = Field(default=0.0, alias="AmountFilled")
    average_price: float = Field(default=0.0, alias="AveragePrice")
    stop_price: float = Field(default=0.0, alias="StopPrice")
    original_price: float = Field(default=0.0, alias="OriginalPrice")
    updated_at: Optional[datetime] = Field(default=None, alias="UpdatedAt")
    trade: FXTrade = Field(default_factory=FXTrade, alias="Trade")


class FXPosition(FXBase):
    id: str = Field(default="", alias="ID")
    side: str = Field(default="", alias="Side")
    pair: str = Field(default="", alias="Pair")
    amount: float = Field(default=0.0, alias="Amount")
    entry_price: float = Field(default=0.0, alias="EntryPrice")
    exit_price: float = Field(default=0.0, alias="ExitPrice")
    pnl: float = Field(default=0.0, alias="Pnl")
    net_pnl: float = Field(default=0.0, alias="NetPnl")
    commission: float = Field(default=0.0, alias="Commission")
    swap: float = Field(default=0.0, alias="Swap")
    mae: Optional[float] = Field(default=None, alias="MAE")
    mfe: Optional[float] = Field(default=None, alias="MFE")
    rr: Optional[float] = Field(default=None, alias="RR")
    rr_planned: Optional[float] = Field(default=None, alias="RRPlanned")
    tp: Optional[float] = Field(default=None, alias="TP")
    sl: Optional[float] = Field(default=None, alias="SL")
    liquidation_price: float = Field(default=0.0, alias="LiquidationPrice")
    multiplier: int = Field(default=1, alias="Multiplier")
    isolated: bool = Field(default=False, alias="Isolated")
    closed: bool = Field(default=False, alias="Closed")
    status: Optional[str] = Field(default=None, alias="Status")
    # MT5 stamps every time in the trade server's clock, and that is what the
    # unsuffixed fields carry - they are what the terminal itself would show.
    # The *Utc pair is the same instant with the server's offset taken off, and
    # is None while that offset is unknown.
    created_at: Optional[datetime] = Field(default=None, alias="CreatedAt")
    closed_at: Optional[datetime] = Field(default=None, alias="ClosedAt")
    created_at_utc: Optional[datetime] = Field(default=None, alias="CreatedAtUtc")
    closed_at_utc: Optional[datetime] = Field(default=None, alias="ClosedAtUtc")
    orders: List[FXOrder] = Field(default_factory=list, alias="Orders")
    balance_init: float = Field(default=0.0, alias="BalanceInit")


class FXOpenPosition(FXBase):

    id: str = Field(default="", alias="ID")
    pair: str = Field(default="", alias="Pair")
    amount: float = Field(default=0.0, alias="Amount")
    side: str = Field(default="", alias="Side")
    entry_price: float = Field(default=0.0, alias="EntryPrice")
    current_price: float = Field(default=0.0, alias="CurrentPrice")
    open_time: Optional[datetime] = Field(default=None, alias="OpenTime")
    open_time_utc: Optional[datetime] = Field(default=None, alias="OpenTimeUtc")
    orders: List[FXOrder] = Field(default_factory=list, alias="Orders")


class FXAccountInfo(FXBase):
    balance: float = Field(default=0.0, alias="Balance")
    leverage: int = Field(default=0, alias="Leverage")
    currency: str = Field(default="", alias="Currency")


class UserBalanceSnapshot(FXBase):
    created_at: Optional[datetime] = Field(default=None, alias="CreatedAt")
    created_at_utc: Optional[datetime] = Field(default=None, alias="CreatedAtUtc")
    balance: float = Field(default=0.0, alias="Balance")


class Transaction(FXBase):
    time: Optional[datetime] = Field(default=None, alias="Time")
    time_utc: Optional[datetime] = Field(default=None, alias="TimeUtc")
    type: str = Field(default="", alias="Type")
    amount: float = Field(default=0.0, alias="Amount")
