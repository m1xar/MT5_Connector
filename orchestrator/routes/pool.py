from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from pool.manager import PoolManager
from schemas.pool import HealthResponse, PoolStatusResponse, pool_status_to_response

from ..deps import COMMON_RESPONSES, get_pool, verify_auth

router = APIRouter(tags=["Pool"])


@router.get(
    "/pool/status",
    response_model=PoolStatusResponse,
    summary="Terminal pool state",
    description="Per-worker state plus how deep the sync queue is.",
    responses=COMMON_RESPONSES,
    dependencies=[Depends(verify_auth)],
)
async def get_pool_status(terminal_pool: PoolManager = Depends(get_pool)):
    return pool_status_to_response(terminal_pool.status())


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness",
    description=(
        "Public. `degraded` means at least one terminal is not usable; "
        "`stalled` means the dispatcher has stopped, so nothing will be "
        "synced at all no matter how healthy the terminals look."
    ),
)
async def healthz(request: Request):
    pool: PoolManager | None = getattr(request.app.state, "pool", None)
    if pool is None:
        return HealthResponse(status="starting", healthy_workers=0, worker_count=0)
    worker_count = len(pool.status().workers)
    healthy = pool.healthy_workers
    if not pool.dispatcher_alive:
        status = "stalled"
    elif healthy == worker_count:
        status = "ok"
    else:
        status = "degraded"
    return HealthResponse(status=status, healthy_workers=healthy, worker_count=worker_count)
