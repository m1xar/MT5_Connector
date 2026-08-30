from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain import fx
from domain.models import MT5OpenPosition, MT5Position, utc_now

_POSITION_FIELDS = (
    "side",
    "pair",
    "amount",
    "entry_price",
    "exit_price",
    "pnl",
    "net_pnl",
    "commission",
    "swap",
    "mae",
    "mfe",
    "rr",
    "rr_planned",
    "tp",
    "sl",
    "liquidation_price",
    "multiplier",
    "isolated",
    "closed",
    "status",
    "balance_init",
    "created_at",
    "closed_at",
)

# Only new positions get priced, so a sync carries no MAE/MFE for the ones it
# skipped. That absence must not wipe what was measured earlier.
_KEPT_WHEN_ABSENT = frozenset({"mae", "mfe"})


def _orders_json(position: fx.FXPosition | fx.FXOpenPosition) -> list[dict]:
    return [order.model_dump(by_alias=True, mode="json") for order in position.orders]


class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_many(self, account_id: str, positions: List[fx.FXPosition]) -> int:
        if not positions:
            return 0

        existing = await self._existing_by_external_id(account_id)
        now = utc_now()
        for position in positions:
            row = existing.get(position.id)
            if row is None:
                row = MT5Position(account_id=account_id, external_id=position.id)
                self.session.add(row)
            for field_name in _POSITION_FIELDS:
                value = getattr(position, field_name)
                if (
                    value is None
                    and field_name in _KEPT_WHEN_ABSENT
                    and getattr(row, field_name) is not None
                ):
                    continue
                setattr(row, field_name, value)
            row.orders = _orders_json(position)
            row.synced_at = now
        await self.session.flush()
        return len(positions)

    async def measured_external_ids(self, account_id: str) -> set[str]:
        """Positions that already carry an excursion, so need no candles."""
        result = await self.session.execute(
            select(MT5Position.external_id)
            .where(MT5Position.account_id == account_id)
            .where(MT5Position.mae != None)  # noqa: E711
        )
        return set(result.scalars().all())

    async def _existing_by_external_id(self, account_id: str) -> dict[str, MT5Position]:
        result = await self.session.execute(
            select(MT5Position).where(MT5Position.account_id == account_id)
        )
        return {row.external_id: row for row in result.scalars().all()}

    async def list(
        self,
        account_id: str,
        closed_after: Optional[datetime] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[MT5Position]:
        statement = select(MT5Position).where(MT5Position.account_id == account_id)
        if closed_after is not None:
            statement = statement.where(MT5Position.closed_at >= closed_after)
        statement = (
            statement.order_by(MT5Position.closed_at.desc().nullslast()).offset(offset).limit(limit)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def list_all(self, account_id: str) -> List[MT5Position]:
        result = await self.session.execute(
            select(MT5Position)
            .where(MT5Position.account_id == account_id)
            .order_by(MT5Position.created_at.asc().nullsfirst())
        )
        return list(result.scalars().all())

    async def delete_for_account(self, account_id: str) -> None:
        await self.session.execute(
            sa_delete(MT5Position).where(MT5Position.account_id == account_id)
        )
        await self.session.flush()


class OpenPositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def replace_all(self, account_id: str, positions: List[fx.FXOpenPosition]) -> int:
        await self.session.execute(
            sa_delete(MT5OpenPosition).where(MT5OpenPosition.account_id == account_id)
        )
        now = utc_now()
        for position in positions:
            self.session.add(
                MT5OpenPosition(
                    account_id=account_id,
                    external_id=position.id,
                    pair=position.pair,
                    amount=position.amount,
                    side=position.side,
                    entry_price=position.entry_price,
                    current_price=position.current_price,
                    open_time=position.open_time,
                    orders=_orders_json(position),
                    synced_at=now,
                )
            )
        await self.session.flush()
        return len(positions)

    async def delete_for_account(self, account_id: str) -> None:
        await self.session.execute(
            sa_delete(MT5OpenPosition).where(MT5OpenPosition.account_id == account_id)
        )
        await self.session.flush()

    async def list(self, account_id: str) -> List[MT5OpenPosition]:
        result = await self.session.execute(
            select(MT5OpenPosition)
            .where(MT5OpenPosition.account_id == account_id)
            .order_by(MT5OpenPosition.open_time.desc().nullslast())
        )
        return list(result.scalars().all())
