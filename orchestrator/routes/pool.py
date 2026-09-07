from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pool.manager import PoolManager
from repositories.account_repo import AccountRepository
from schemas.pool import HealthResponse, PoolStatusResponse, pool_status_to_response

from ..deps import COMMON_RESPONSES, get_pool, get_session, verify_auth

router = APIRouter(tags=["Pool"])


@router.get(
    "/pool/status",
    response_model=PoolStatusResponse,
    summary="Terminal pool state",
    description="Per-worker state, how deep each terminal's own queue is, and how many accounts are pinned to it.",
    responses={401: COMMON_RESPONSES[401]},
    dependencies=[Depends(verify_auth)],
)
async def get_pool_status(
    terminal_pool: PoolManager = Depends(get_pool), session: AsyncSession = Depends(get_session)
):
    assigned = await AccountRepository(session).counts_by_terminal()
    return pool_status_to_response(terminal_pool.status(), assigned)


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness",
    description=(
        "Public. `degraded` means at least one terminal is not usable; "
        "`stalled` means a terminal's dispatcher has died, so nothing queued "
        "for that terminal will be synced no matter how healthy it looks."
    ),
)
async def healthz(request: Request):
    pool: PoolManager | None = getattr(request.app.state, "pool", None)
    if pool is None:
        return HealthResponse(status="starting", healthy_workers=0, worker_count=0)
    worker_count = len(pool.terminal_paths)
    healthy = pool.healthy_workers
    if not pool.dispatcher_alive:
        status = "stalled"
    elif healthy == worker_count:
        status = "ok"
    else:
        status = "degraded"
    return HealthResponse(status=status, healthy_workers=healthy, worker_count=worker_count)
