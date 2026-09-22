from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import delete as sa_delete, func, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain.models import MT5OpenPosition, MT5Position, new_id, utc_now
from domain.rows import OPEN_POSITION_FIELDS, POSITION_FIELDS

_POSITION_FIELDS = POSITION_FIELDS
_KEPT_WHEN_ABSENT = frozenset({"mae", "mfe"})
_BATCH = 1000


def _batches(rows: list[dict]):
    for start in range(0, len(rows), _BATCH):
        yield rows[start:start + _BATCH]


class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_many(self, account_id: str, positions: List[dict]) -> int:
        if not positions:
            return 0
        now = utc_now()
        rows = [
            {
                **{name: position[name] for name in _POSITION_FIELDS},
                "id": new_id(), "account_id": account_id, "external_id": position["external_id"],
                "orders": position["orders"], "synced_at": now,
            }
            for position in positions
        ]
        for batch in _batches(rows):
            statement = pg_insert(MT5Position).values(batch)
            excluded = statement.excluded
            changes = {name: getattr(excluded, name) for name in _POSITION_FIELDS if name not in _KEPT_WHEN_ABSENT}
            changes.update({
                name: func.coalesce(getattr(excluded, name), getattr(MT5Position, name)) for name in _KEPT_WHEN_ABSENT
            })
            changes.update(orders=excluded.orders, synced_at=excluded.synced_at)
            await self.session.execute(
                statement.on_conflict_do_update(index_elements=["account_id", "external_id"], set_=changes)
            )
        return len(rows)

    async def measured_external_ids(self, account_id: str) -> set[str]:
        result = await self.session.execute(
            select(MT5Position.external_id)
            .where(MT5Position.account_id == account_id)
            .where(MT5Position.mae.is_not(None))
        )
        return set(result.scalars().all())

    async def list(
        self, account_id: str, closed_after: Optional[datetime] = None, limit: int = 500, offset: int = 0
    ) -> List[MT5Position]:
        statement = select(MT5Position).where(MT5Position.account_id == account_id)
        if closed_after is not None:
            statement = statement.where(MT5Position.closed_at >= closed_after)
        statement = statement.order_by(MT5Position.closed_at.desc().nullslast()).offset(offset).limit(limit)
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
        await self.session.execute(sa_delete(MT5Position).where(MT5Position.account_id == account_id))
        await self.session.flush()


class OpenPositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def replace_all(self, account_id: str, positions: List[dict]) -> int:
        await self.session.execute(sa_delete(MT5OpenPosition).where(MT5OpenPosition.account_id == account_id))
        now = utc_now()
        rows = [
            {
                **{name: position[name] for name in OPEN_POSITION_FIELDS},
                "id": new_id(), "account_id": account_id, "external_id": position["external_id"],
                "orders": position["orders"], "synced_at": now,
            }
            for position in positions
        ]
        for batch in _batches(rows):
            await self.session.execute(insert(MT5OpenPosition).values(batch))
        return len(rows)

    async def delete_for_account(self, account_id: str) -> None:
        await self.session.execute(sa_delete(MT5OpenPosition).where(MT5OpenPosition.account_id == account_id))
        await self.session.flush()

    async def list(self, account_id: str) -> List[MT5OpenPosition]:
        result = await self.session.execute(
            select(MT5OpenPosition)
            .where(MT5OpenPosition.account_id == account_id)
            .order_by(MT5OpenPosition.open_time.desc().nullslast())
        )
        return list(result.scalars().all())
