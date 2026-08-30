from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import SyncKind
from repositories.account_repo import AccountRepository
from schemas.account import (
    AccountCreateRequest,
    AccountListResponse,
    AccountResponse,
    AccountUpdateRequest,
    account_to_response,
)
from schemas.common import ErrorResponse
from services.account_service import AccountExistsError, AccountService
from services.sync_service import SyncService
from utils.config import settings
from utils.logging import get_logger, log_event

from ..auth import verify_auth
from ..runtime import COMMON_RESPONSES, get_session, get_sync_service, load_account

logger = get_logger(__name__)

router = APIRouter(tags=["Accounts"], responses=COMMON_RESPONSES)


@router.post(
    "/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an MT5 account",
    description=(
        "The server name must match what the terminals have configured, otherwise "
        "login will fail on every worker.\n\n"
        "Registering an account queues an **initial sync** ahead of everything "
        "else in the pool, with a longer connect timeout than a routine sync "
        "gets, and only one attempt at it. If that attempt fails the account is "
        "marked `error_connection` straight away - it has never connected, so "
        "there is nothing for the failure to be a blip in. Pass `wait=true` to "
        "block on that sync and get the settled status back."
    ),
    responses={**COMMON_RESPONSES, 409: {"model": ErrorResponse, "description": "Already exists"}},
)
async def create_account(
    data: AccountCreateRequest,
    wait: bool = Query(
        default=False,
        description="Block until the initial sync settles and return its outcome",
    ),
    session: AsyncSession = Depends(get_session),
    service: SyncService = Depends(get_sync_service),
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

    # Nothing about this account has been proven yet, so prove it now: the
    # initial sync jumps the queue on a longer connect timeout.
    _sync_run_id, future = await service.request(
        session,
        account,
        SyncKind.initial,
        connect_timeout_ms=settings.terminal_initial_connect_timeout_ms,
    )

    if wait:
        try:
            await asyncio.wait_for(
                asyncio.shield(future),
                timeout=settings.hard_sync_wait_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log_event(
                logger,
                "warning",
                "account.initial_sync.wait_timeout",
                account_id=account.account_id,
            )
        # The sync was persisted from its own session; re-read to report it.
        await session.refresh(account)

    return account_to_response(account)


@router.get("/accounts", response_model=AccountListResponse, summary="List accounts")
async def list_accounts(
    owner_id: str | None = None,
    enabled: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    accounts = await AccountRepository(session).list(
        owner_id=owner_id, enabled=enabled, limit=limit, offset=offset
    )
    responses = [account_to_response(account) for account in accounts]
    return AccountListResponse(accounts=responses, count=len(responses))


@router.get("/accounts/{account_id}", response_model=AccountResponse, summary="Get one account")
async def get_account(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    return account_to_response(await load_account(session, account_id))


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountResponse,
    summary="Update an account",
    description=(
        "Disabling an account removes it from scheduled syncs but keeps its data. "
        "Supplying a new password clears the failure count and restores `active`, "
        "so a fixed account is picked up again."
    ),
)
async def update_account(
    account_id: str,
    data: AccountUpdateRequest,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    account = await load_account(session, account_id)
    updated = await AccountService(session).update(
        account,
        password=data.password,
        broker=data.broker,
        label=data.label,
        enabled=data.enabled,
    )
    return account_to_response(updated)


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an account and its synced data",
)
async def delete_account(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    _auth: str | None = Depends(verify_auth),
):
    await AccountService(session).delete(await load_account(session, account_id))
