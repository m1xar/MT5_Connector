from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from domain import fx
from schemas.sync import (
    AccountInfoResponse,
    BalanceSnapshotListResponse,
    OpenPositionListResponse,
    PositionListResponse,
    TransactionListResponse,
)
from domain.models import MT5Account
from mt5api.clock import ServerClock
from services.query_service import QueryService

from ..auth import verify_auth
from ..runtime import COMMON_RESPONSES, get_session, load_account

router = APIRouter(tags=["Data"], responses=COMMON_RESPONSES)

_DAYS = Query(default=0, ge=0, description="Window in days; 0 means everything")


def _clock(account: MT5Account) -> ServerClock:
    """The trade server's clock as this account last measured it."""
    return ServerClock(offset_minutes=account.server_utc_offset_minutes)


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
    _auth: str | None = Depends(verify_auth),
):
    account = await load_account(session, account_id)
    positions = await QueryService(session).positions(
        account_id,
        days=days,
        limit=limit,
        offset=offset,
        clock=_clock(account),
    )
    return PositionListResponse(
        account_id=account_id, count=len(positions), positions=positions
    )


@router.get(
    "/accounts/{account_id}/open-positions",
    response_model=OpenPositionListResponse,
    summary="Open positions",
    description="Replaced wholesale on every sync, so only as live as the last one.",
)
async def get_open_positions(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    account = await load_account(session, account_id)
    open_positions = await QueryService(session).open_positions(
        account_id, clock=_clock(account)
    )
    return OpenPositionListResponse(
        account_id=account_id, count=len(open_positions), open_positions=open_positions
    )


@router.get(
    "/accounts/{account_id}/balance-snapshots",
    response_model=BalanceSnapshotListResponse,
    summary="Balance curve",
    description=(
        "Derived from the full position history on every request rather than "
        "stored, so the opening balance is always correct."
    ),
)
async def get_balance_snapshots(
    account_id: str,
    days: int = _DAYS,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    account = await load_account(session, account_id)
    snapshots = await QueryService(session).balance_snapshots(
        account_id,
        days=days,
        clock=_clock(account),
    )
    return BalanceSnapshotListResponse(
        account_id=account_id, count=len(snapshots), snapshots=snapshots
    )


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
    _auth: str | None = Depends(verify_auth),
):
    account = await load_account(session, account_id)
    transactions = await QueryService(session).transactions(
        account_id,
        days=days,
        limit=limit,
        offset=offset,
        clock=_clock(account),
    )
    return TransactionListResponse(
        account_id=account_id, count=len(transactions), transactions=transactions
    )


@router.get(
    "/accounts/{account_id}/info",
    response_model=AccountInfoResponse,
    summary="Account balance, leverage and currency",
)
async def get_account_info(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    account = await load_account(session, account_id)
    return AccountInfoResponse(
        account_id=account_id,
        account_info=fx.FXAccountInfo(
            balance=account.balance,
            leverage=account.leverage,
            currency=account.currency,
        ),
        equity=account.equity,
        server_utc_offset_minutes=account.server_utc_offset_minutes,
        last_synced_at=(
            account.last_synced_at.isoformat() if account.last_synced_at else None
        ),
        status=account.status.value,
    )
