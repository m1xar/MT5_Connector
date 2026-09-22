from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain.models import MT5Transaction, new_id, utc_now

_BATCH = 1000


class TransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_many(self, account_id: str, transactions: List[dict]) -> int:
        if not transactions:
            return 0
        now = utc_now()
        rows: dict[str, dict] = {}
        for transaction in transactions:
            rows.setdefault(transaction["fingerprint"], {
                "id": new_id(), "account_id": account_id, "fingerprint": transaction["fingerprint"],
                "time": transaction["time"], "type": transaction["type"], "amount": transaction["amount"],
                "synced_at": now,
            })
        added = 0
        batch = list(rows.values())
        for start in range(0, len(batch), _BATCH):
            result = await self.session.execute(
                pg_insert(MT5Transaction).values(batch[start:start + _BATCH])
                .on_conflict_do_nothing(index_elements=["account_id", "fingerprint"])
                .returning(MT5Transaction.fingerprint)
            )
            added += len(result.all())
        return added

    async def delete_for_account(self, account_id: str) -> None:
        await self.session.execute(sa_delete(MT5Transaction).where(MT5Transaction.account_id == account_id))
        await self.session.flush()

    async def list(
        self, account_id: str, after: Optional[datetime] = None, limit: int = 500, offset: int = 0
    ) -> List[MT5Transaction]:
        statement = select(MT5Transaction).where(MT5Transaction.account_id == account_id)
        if after is not None:
            statement = statement.where(MT5Transaction.time >= after)
        statement = statement.order_by(MT5Transaction.time.desc().nullslast()).offset(offset).limit(limit)
        result = await self.session.execute(statement)
        return list(result.scalars().all())
