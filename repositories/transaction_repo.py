from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain import fx
from domain.models import MT5Transaction, utc_now
from utils.logging import hash_value


def transaction_fingerprint(transaction: fx.Transaction) -> str:
    stamp = transaction.time.isoformat() if transaction.time else ""
    return hash_value(f"{stamp}|{transaction.type}|{transaction.amount:.8f}")


class TransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_many(self, account_id: str, transactions: List[fx.Transaction]) -> int:
        if not transactions:
            return 0

        result = await self.session.execute(
            select(MT5Transaction.fingerprint).where(MT5Transaction.account_id == account_id)
        )
        known = set(result.scalars().all())

        now = utc_now()
        added = 0
        for transaction in transactions:
            fingerprint = transaction_fingerprint(transaction)
            if fingerprint in known:
                continue
            known.add(fingerprint)
            self.session.add(
                MT5Transaction(
                    account_id=account_id,
                    fingerprint=fingerprint,
                    time=transaction.time,
                    type=transaction.type,
                    amount=transaction.amount,
                    synced_at=now,
                )
            )
            added += 1
        await self.session.flush()
        return added

    async def delete_for_account(self, account_id: str) -> None:
        await self.session.execute(
            sa_delete(MT5Transaction).where(MT5Transaction.account_id == account_id)
        )
        await self.session.flush()

    async def list(
        self,
        account_id: str,
        after: Optional[datetime] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[MT5Transaction]:
        statement = select(MT5Transaction).where(MT5Transaction.account_id == account_id)
        if after is not None:
            statement = statement.where(MT5Transaction.time >= after)
        statement = (
            statement.order_by(MT5Transaction.time.desc().nullslast()).offset(offset).limit(limit)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())
