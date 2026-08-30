from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import SyncKind, SyncStatus
from pool.manager import PoolManager
from schemas.sync import SyncQueuedResponse, SyncResultResponse
from services.sync_service import SyncService
from utils.config import settings

from ..auth import verify_auth
from ..runtime import COMMON_RESPONSES, get_pool, get_session, get_sync_service, load_account

router = APIRouter(tags=["Sync"], responses=COMMON_RESPONSES)


@router.post(
    "/accounts/{account_id}/sync",
    summary="Sync an account",
    description=(
        "With `wait=true` (a hard sync) the account jumps to the front of the "
        "terminal queue and the freshly pulled data comes back in the response, "
        "already written to the database. With `wait=false` the sync is queued at "
        "normal priority and 202 comes back immediately."
    ),
    responses={
        **COMMON_RESPONSES,
        504: {"description": "Hard sync exceeded its wait timeout"},
    },
)
async def sync_account(
    account_id: str,
    wait: bool = Query(default=True, description="Block until the sync finishes"),
    session: AsyncSession = Depends(get_session),
    service: SyncService = Depends(get_sync_service),
    terminal_pool: PoolManager = Depends(get_pool),
    _auth: str | None = Depends(verify_auth),
):
    account = await load_account(session, account_id)
    kind = SyncKind.hard if wait else SyncKind.scheduled
    run, future = await service.request(session, account, kind)

    if not wait:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=SyncQueuedResponse(
                sync_run_id=run.sync_run_id,
                account_id=account_id,
                status=SyncStatus.queued,
                queue_depth=terminal_pool.status().queue_depth,
            ).model_dump(mode="json"),
        )

    try:
        result = await asyncio.wait_for(
            asyncio.shield(future),
            timeout=settings.hard_sync_wait_timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Hard sync still running after "
                f"{settings.hard_sync_wait_timeout_seconds}s; poll the account instead"
            ),
        )

    payload = result.payload
    return SyncResultResponse(
        sync_run_id=result.sync_run_id,
        account_id=result.account_id,
        ok=result.ok,
        error=result.error,
        duration_ms=result.duration_ms,
        worker_id=result.worker_id,
        account_info=payload.account_info if payload else None,
        positions=payload.positions if payload else [],
        open_positions=payload.open_positions if payload else [],
        transactions=payload.transactions if payload else [],
    )
