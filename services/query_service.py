from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from domain import fx
from domain.models import MT5OpenPosition, MT5Position, MT5Transaction
from mt5api.builders.balance_snapshots import build_balance_snapshots
from mt5api.helpers.timeutil import cutoff_from_days, to_real_utc
from repositories.position_repo import OpenPositionRepository, PositionRepository
from repositories.transaction_repo import TransactionRepository


def position_to_fx(row: MT5Position, offset_minutes: Optional[int] = None) -> fx.FXPosition:
    return fx.FXPosition(
        id=row.external_id,
        side=row.side,
        pair=row.pair,
        amount=row.amount,
        entry_price=row.entry_price,
        exit_price=row.exit_price,
        pnl=row.pnl,
        net_pnl=row.net_pnl,
        commission=row.commission,
        swap=row.swap,
        mae=row.mae,
        mfe=row.mfe,
        rr=row.rr,
        rr_planned=row.rr_planned,
        tp=row.tp,
        sl=row.sl,
        liquidation_price=row.liquidation_price,
        multiplier=row.multiplier,
        isolated=row.isolated,
        closed=row.closed,
        status=row.status,
        created_at=row.created_at,
        closed_at=row.closed_at,
        created_at_utc=to_real_utc(row.created_at, offset_minutes),
        closed_at_utc=to_real_utc(row.closed_at, offset_minutes),
        orders=[fx.FXOrder.model_validate(order) for order in (row.orders or [])],
        balance_init=row.balance_init,
    )


def open_position_to_fx(
    row: MT5OpenPosition, offset_minutes: Optional[int] = None
) -> fx.FXOpenPosition:
    return fx.FXOpenPosition(
        id=row.external_id,
        pair=row.pair,
        amount=row.amount,
        side=row.side,
        entry_price=row.entry_price,
        current_price=row.current_price,
        open_time=row.open_time,
        open_time_utc=to_real_utc(row.open_time, offset_minutes),
        orders=[fx.FXOrder.model_validate(order) for order in (row.orders or [])],
    )


def transaction_to_fx(
    row: MT5Transaction, offset_minutes: Optional[int] = None
) -> fx.Transaction:
    return fx.Transaction(
        time=row.time,
        time_utc=to_real_utc(row.time, offset_minutes),
        type=row.type,
        amount=row.amount,
    )


class QueryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def positions(
        self,
        account_id: str,
        days: int | None = None,
        limit: int = 500,
        offset: int = 0,
        server_offset_minutes: int | None = None,
    ) -> List[fx.FXPosition]:
        rows = await PositionRepository(self.session).list(
            account_id, closed_after=cutoff_from_days(days), limit=limit, offset=offset
        )
        return [position_to_fx(row, server_offset_minutes) for row in rows]

    async def open_positions(
        self, account_id: str, server_offset_minutes: int | None = None
    ) -> List[fx.FXOpenPosition]:
        rows = await OpenPositionRepository(self.session).list(account_id)
        return [open_position_to_fx(row, server_offset_minutes) for row in rows]

    async def transactions(
        self,
        account_id: str,
        days: int | None = None,
        limit: int = 500,
        offset: int = 0,
        server_offset_minutes: int | None = None,
    ) -> List[fx.Transaction]:
        rows = await TransactionRepository(self.session).list(
            account_id, after=cutoff_from_days(days), limit=limit, offset=offset
        )
        return [transaction_to_fx(row, server_offset_minutes) for row in rows]

    async def balance_snapshots(
        self,
        account_id: str,
        days: int | None = None,
        server_offset_minutes: int | None = None,
    ) -> List[fx.UserBalanceSnapshot]:
        rows = await PositionRepository(self.session).list_all(account_id)
        return build_balance_snapshots(
            [position_to_fx(row, server_offset_minutes) for row in rows],
            days=days,
            server_offset_minutes=server_offset_minutes,
        )
