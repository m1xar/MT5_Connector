from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import async_session_factory, dispose_db, init_db
from domain.enums import SyncKind, SyncStatus
from domain.models import MT5Account
from pool.manager import PoolManager
from schemas.account import (
    AccountCreateRequest,
    AccountListResponse,
    AccountResponse,
    AccountUpdateRequest,
    account_to_response,
)
from schemas.common import ErrorResponse
from schemas.pool import HealthResponse, PoolStatusResponse, pool_status_to_response
from schemas.sync import (
    AccountInfoResponse,
    BalanceSnapshotListResponse,
    OpenPositionListResponse,
    PositionListResponse,
    SyncQueuedResponse,
    SyncResultResponse,
    TransactionListResponse,
)
from services.account_service import AccountExistsError, AccountService
from services.query_service import QueryService
from services.sync_service import SyncService
from utils.config import settings, terminal_path_list
from utils.logging import configure_logging, get_logger, log_event
from utils.id import new_id

from .auth import verify_auth
from .background import cancel_background_tasks, scheduler_loop, track_bg_task

configure_logging(settings.log_level, settings.log_json)
logger = get_logger(__name__)

_COMMON_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Invalid or missing bearer token"},
    404: {"model": ErrorResponse, "description": "Account not found"},
}

pool: PoolManager | None = None
sync_service: SyncService | None = None


def get_pool() -> PoolManager:
    if pool is None:
        raise HTTPException(status_code=503, detail="Terminal pool is not ready")
    return pool


def get_sync_service() -> SyncService:
    if sync_service is None:
        raise HTTPException(status_code=503, detail="Sync service is not ready")
    return sync_service


async def get_session():
    async with async_session_factory() as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, sync_service

    await init_db()

    paths = terminal_path_list()
    if not paths:
        log_event(logger, "warning", "app.startup.no_terminals")

    pool = PoolManager(
        paths,
        task_timeout_seconds=settings.sync_task_timeout_seconds,
        worker_start_timeout_seconds=settings.worker_start_timeout_seconds,
        max_task_retries=settings.max_task_retries,
        init_timeout_ms=settings.terminal_init_timeout_ms,
        login_timeout_ms=settings.terminal_login_timeout_ms,
        log_level=settings.log_level,
        log_json=settings.log_json,
        enrich_mae_mfe=settings.enrich_mae_mfe,
    )
    sync_service = SyncService(pool)
    pool.on_result = sync_service.persist

    await pool.start()
    track_bg_task(asyncio.create_task(scheduler_loop(sync_service), name="sync-scheduler"))
    log_event(logger, "info", "app.startup.completed", terminals=len(paths))

    try:
        yield
    finally:
        await cancel_background_tasks()
        await pool.stop()
        await dispose_db()
        log_event(logger, "info", "app.shutdown.completed")


app = FastAPI(
    title="MT5 Sync API",
    version="0.1.0",
    description=(
        "Syncs MT5 trading accounts into standardized FX models through a pool "
        "of MetaTrader 5 terminals."
    ),
    openapi_tags=[
        {"name": "Accounts", "description": "Registered MT5 trading accounts."},
        {"name": "Sync", "description": "Queue a sync or force one to the front of the queue."},
        {"name": "Data", "description": "Synced trading data, served from the database."},
        {"name": "Pool", "description": "Terminal pool health and queue depth."},
    ],
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.cors_allow_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    from utils.logging import reset_context, set_request_context

    tokens = set_request_context(request_id=request.headers.get("x-request-id") or new_id())
    try:
        return await call_next(request)
    finally:
        reset_context(tokens)


async def _load_account(session: AsyncSession, account_id: str) -> MT5Account:
    from repositories.account_repo import AccountRepository

    account = await AccountRepository(session).get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.post(
    "/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Accounts"],
    summary="Register an MT5 account",
    description=(
        "The server name must match what the terminals have configured, otherwise "
        "login will fail on every worker."
    ),
    responses={**_COMMON_RESPONSES, 409: {"model": ErrorResponse, "description": "Already exists"}},
)
async def create_account(
    data: AccountCreateRequest,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    try:
        account = await AccountService(session).create(
            login=data.login,
            password=data.password,
            server=data.server,
            broker=data.broker,
            label=data.label,
            owner_id=data.owner_id,
        )
    except AccountExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return account_to_response(account)


@app.get(
    "/accounts",
    response_model=AccountListResponse,
    tags=["Accounts"],
    summary="List accounts",
    responses=_COMMON_RESPONSES,
)
async def list_accounts(
    owner_id: str | None = None,
    enabled: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    from repositories.account_repo import AccountRepository

    accounts = await AccountRepository(session).list(
        owner_id=owner_id, enabled=enabled, limit=limit, offset=offset
    )
    responses = [account_to_response(account) for account in accounts]
    return AccountListResponse(accounts=responses, count=len(responses))


@app.get(
    "/accounts/{account_id}",
    response_model=AccountResponse,
    tags=["Accounts"],
    summary="Get one account",
    responses=_COMMON_RESPONSES,
)
async def get_account(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    return account_to_response(await _load_account(session, account_id))


@app.patch(
    "/accounts/{account_id}",
    response_model=AccountResponse,
    tags=["Accounts"],
    summary="Update an account",
    description="Disabling an account removes it from scheduled syncs but keeps its data.",
    responses=_COMMON_RESPONSES,
)
async def update_account(
    account_id: str,
    data: AccountUpdateRequest,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    account = await _load_account(session, account_id)
    updated = await AccountService(session).update(
        account,
        password=data.password,
        broker=data.broker,
        label=data.label,
        enabled=data.enabled,
    )
    return account_to_response(updated)


@app.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Accounts"],
    summary="Delete an account and its synced data",
    responses=_COMMON_RESPONSES,
)
async def delete_account(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    account = await _load_account(session, account_id)
    await AccountService(session).delete(account)


@app.post(
    "/accounts/{account_id}/sync",
    tags=["Sync"],
    summary="Sync an account",
    description=(
        "With `wait=true` (a hard sync) the account jumps to the front of the "
        "terminal queue and the freshly pulled data is returned in the response, "
        "already written to the database. With `wait=false` the sync is queued at "
        "normal priority and 202 is returned immediately."
    ),
    responses={
        **_COMMON_RESPONSES,
        504: {"model": ErrorResponse, "description": "Hard sync exceeded its wait timeout"},
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
    account = await _load_account(session, account_id)
    kind = SyncKind.hard if wait else SyncKind.scheduled
    run, future = await service.request(session, account, kind)

    if not wait:
        return SyncQueuedResponse(
            sync_run_id=run.sync_run_id,
            account_id=account_id,
            status=SyncStatus.queued,
            queue_depth=terminal_pool.status().queue_depth,
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


@app.get(
    "/accounts/{account_id}/positions",
    response_model=PositionListResponse,
    tags=["Data"],
    summary="Closed positions",
    description="Reconstructed from MT5 deals. `days=0` returns everything.",
    responses=_COMMON_RESPONSES,
)
async def get_positions(
    account_id: str,
    days: int = Query(default=0, ge=0, description="Window on ClosedAt; 0 means all"),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    await _load_account(session, account_id)
    positions = await QueryService(session).positions(
        account_id, days=days, limit=limit, offset=offset
    )
    return PositionListResponse(
        account_id=account_id, count=len(positions), positions=positions
    )


@app.get(
    "/accounts/{account_id}/open-positions",
    response_model=OpenPositionListResponse,
    tags=["Data"],
    summary="Open positions",
    description="Replaced wholesale on every sync, so it is only as live as the last sync.",
    responses=_COMMON_RESPONSES,
)
async def get_open_positions(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    await _load_account(session, account_id)
    open_positions = await QueryService(session).open_positions(account_id)
    return OpenPositionListResponse(
        account_id=account_id, count=len(open_positions), open_positions=open_positions
    )


@app.get(
    "/accounts/{account_id}/balance-snapshots",
    response_model=BalanceSnapshotListResponse,
    tags=["Data"],
    summary="Balance curve",
    description=(
        "Derived from the full position history on every request rather than stored, "
        "so the opening balance is always correct."
    ),
    responses=_COMMON_RESPONSES,
)
async def get_balance_snapshots(
    account_id: str,
    days: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    await _load_account(session, account_id)
    snapshots = await QueryService(session).balance_snapshots(account_id, days=days)
    return BalanceSnapshotListResponse(
        account_id=account_id, count=len(snapshots), snapshots=snapshots
    )


@app.get(
    "/accounts/{account_id}/transactions",
    response_model=TransactionListResponse,
    tags=["Data"],
    summary="Deposits and withdrawals",
    responses=_COMMON_RESPONSES,
)
async def get_transactions(
    account_id: str,
    days: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    await _load_account(session, account_id)
    transactions = await QueryService(session).transactions(
        account_id, days=days, limit=limit, offset=offset
    )
    return TransactionListResponse(
        account_id=account_id, count=len(transactions), transactions=transactions
    )


@app.get(
    "/accounts/{account_id}/info",
    response_model=AccountInfoResponse,
    tags=["Data"],
    summary="Account balance, leverage and currency",
    responses=_COMMON_RESPONSES,
)
async def get_account_info(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    from domain import fx

    account = await _load_account(session, account_id)
    return AccountInfoResponse(
        account_id=account_id,
        account_info=fx.FXAccountInfo(
            balance=account.balance,
            leverage=account.leverage,
            currency=account.currency,
        ),
        equity=account.equity,
        last_synced_at=account.last_synced_at.isoformat() if account.last_synced_at else None,
        status=account.status.value,
    )


@app.get(
    "/pool/status",
    response_model=PoolStatusResponse,
    tags=["Pool"],
    summary="Terminal pool state",
    description="Per-worker state plus how deep the sync queue is.",
    responses=_COMMON_RESPONSES,
)
async def get_pool_status(
    terminal_pool: PoolManager = Depends(get_pool),
    _auth: str | None = Depends(verify_auth),
):
    return pool_status_to_response(terminal_pool.status())


@app.get(
    "/healthz",
    response_model=HealthResponse,
    tags=["Pool"],
    summary="Liveness",
    description="Public. `degraded` means at least one terminal is not usable.",
)
async def healthz():
    if pool is None:
        return HealthResponse(status="starting", healthy_workers=0, worker_count=0)
    status_snapshot = pool.status()
    healthy = pool.healthy_workers
    return HealthResponse(
        status="ok" if healthy == len(status_snapshot.workers) else "degraded",
        healthy_workers=healthy,
        worker_count=len(status_snapshot.workers),
    )


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
