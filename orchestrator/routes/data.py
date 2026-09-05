from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import MT5Account
from mt5api.clock import ServerClock
from schemas.sync import (
    BalanceSnapshotListResponse,
    OpenPositionListResponse,
    PositionListResponse,
    TransactionListResponse,
)
from services.query_service import QueryService

from ..deps import COMMON_RESPONSES, get_session, load_account, verify_auth

router = APIRouter(tags=["Data"], responses=COMMON_RESPONSES, dependencies=[Depends(verify_auth)])

_DAYS = Query(default=0, ge=0, description="Window in days; 0 means everything")


def _query(session: AsyncSession, account: MT5Account) -> QueryService:
    return QueryService(session, ServerClock.from_rows(account.server_clock))


@router.get(
    "/accounts/{account_id}/positions",
    response_model=PositionListResponse,
    summary="Closed positions",
    description="Reconstructed from MT5 deals.",
)
async def get_positions(
    account_id: str,
    days: int = _DAYS,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    account = await load_account(session, account_id)
    positions = await _query(session, account).positions(account_id, days=days, limit=limit, offset=offset)
    return PositionListResponse(account_id=account_id, count=len(positions), positions=positions)


@router.get(
    "/accounts/{account_id}/open-positions",
    response_model=OpenPositionListResponse,
    summary="Open positions",
    description="Replaced wholesale on every sync, so only as live as the last one.",
)
async def get_open_positions(account_id: str, session: AsyncSession = Depends(get_session)):
    account = await load_account(session, account_id)
    open_positions = await _query(session, account).open_positions(account_id)
    return OpenPositionListResponse(account_id=account_id, count=len(open_positions), open_positions=open_positions)


@router.get(
    "/accounts/{account_id}/balance-snapshots",
    response_model=BalanceSnapshotListResponse,
    summary="Balance curve",
    description="Derived from the full position history on every request rather than stored, so the opening balance is always correct.",
)
async def get_balance_snapshots(account_id: str, days: int = _DAYS, session: AsyncSession = Depends(get_session)):
    account = await load_account(session, account_id)
    snapshots = await _query(session, account).balance_snapshots(account_id, days=days)
    return BalanceSnapshotListResponse(account_id=account_id, count=len(snapshots), snapshots=snapshots)


@router.get(
    "/accounts/{account_id}/transactions",
    response_model=TransactionListResponse,
    summary="Deposits and withdrawals",
)
async def get_transactions(
    account_id: str,
    days: int = _DAYS,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    account = await load_account(session, account_id)
    transactions = await _query(session, account).transactions(account_id, days=days, limit=limit, offset=offset)
    return TransactionListResponse(account_id=account_id, count=len(transactions), transactions=transactions)
