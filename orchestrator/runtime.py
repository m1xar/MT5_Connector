from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import async_session_factory
from domain.models import MT5Account
from pool.manager import PoolManager
from repositories.account_repo import AccountRepository
from schemas.common import ErrorResponse
from services.sync_service import SyncService

# Every authenticated route can answer these, so they are declared once and
# spread into each route's own `responses`.
COMMON_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Invalid or missing bearer token"},
    404: {"model": ErrorResponse, "description": "Account not found"},
}


def get_pool(request: Request) -> PoolManager:
    pool: PoolManager | None = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Terminal pool is not ready")
    return pool


def get_sync_service(request: Request) -> SyncService:
    service: SyncService | None = getattr(request.app.state, "sync_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Sync service is not ready")
    return service


async def get_session():
    async with async_session_factory() as session:
        yield session


async def load_account(session: AsyncSession, account_id: str) -> MT5Account:
    account = await AccountRepository(session).get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account
