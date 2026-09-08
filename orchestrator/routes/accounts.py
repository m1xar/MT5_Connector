from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import async_session_factory
from domain.enums import AccountStatus, SyncKind
from pool.manager import PoolManager
from repositories.account_repo import AccountRepository
from schemas.account import (
    AccountCreateRequest,
    AccountListResponse,
    AccountResponse,
    AccountUpdateRequest,
    account_to_response,
)
from services.account_service import AccountExistsError, AccountService
from services.sync_service import SyncService
from utils.config import settings
from utils.logging import log_event

from ..deps import COMMON_RESPONSES, ErrorResponse, get_pool, get_session, get_sync_service, load_account, verify_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Accounts"], responses=COMMON_RESPONSES, dependencies=[Depends(verify_auth)])


@router.post(
    "/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an MT5 account",
    description=(
        "The server name must match what the terminals have configured, otherwise "
        "login will fail on every worker.\n\n"
        "Registering an account pins it to the terminal that currently has the "
        "fewest active accounts - every sync of this account, now and later, "
        "runs on that one terminal, and the response reports it as `terminal`. "
        "It then queues an **initial sync** ahead of everything else on that "
        "terminal, with a longer connect timeout than a routine sync "
        "gets, and only one attempt at it. If that attempt fails the account is "
        "marked `error_connection` straight away - it has never connected, so "
        "there is nothing for the failure to be a blip in. The request blocks "
        "on that sync and returns the settled account, or **502** with the "
        "account id and the error if it did not complete.\n\n"
        "Use the **investor password**. A master password logs in just as well, "
        "but a broker that sees the owner's own terminal connected withholds "
        "the deal history from the second session, and the sync would record an "
        "account with money and no trades. When that happens the account is not "
        "kept and the response is **422** `history_withheld`."
    ),
    responses={
        **COMMON_RESPONSES,
        409: {"model": ErrorResponse, "description": "Already exists"},
        422: {"description": "Logged in, but the broker withheld the deal history - register with the investor password"},
    },
)
async def create_account(
    data: AccountCreateRequest,
    session: AsyncSession = Depends(get_session),
    service: SyncService = Depends(get_sync_service),
    terminal_pool: PoolManager = Depends(get_pool),
):
    try:
        account = await AccountService(session).create(
            login=data.login, password=data.password, server=data.server,
            terminal_paths=terminal_pool.terminal_paths,
        )
    except AccountExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    future = await service.request(
        session, account, SyncKind.initial, connect_timeout_ms=settings.terminal_initial_connect_timeout_ms
    )
    await session.close()

    try:
        await asyncio.wait_for(asyncio.shield(future), timeout=settings.hard_sync_wait_timeout_seconds)
    except asyncio.TimeoutError:
        log_event(logger, "warning", "account.initial_sync.wait_timeout", account_id=account.account_id)

    if future.done() and future.result().ok and future.result().history_withheld:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "history_withheld",
                "detail": (
                    f"{data.login}@{data.server} logged in, but the broker returned no deal history "
                    "for an account with open positions / a non-round balance. "
                    "Register it with the investor password."
                ),
            },
        )

    async with async_session_factory() as fresh:
        account = await load_account(fresh, account.account_id)

    if account.status is not AccountStatus.active or not account.last_synced_at:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "account_id": account.account_id,
                "status": account.status.value,
                "error": account.last_error or "the initial sync did not complete",
            },
        )
    return account_to_response(account)


@router.get("/accounts", response_model=AccountListResponse, summary="List accounts")
async def list_accounts(
    enabled: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    accounts = await AccountRepository(session).list(enabled=enabled, limit=limit, offset=offset)
    responses = [account_to_response(account) for account in accounts]
    return AccountListResponse(accounts=responses, count=len(responses))


@router.get("/accounts/{account_id}", response_model=AccountResponse, summary="Get one account")
async def get_account(account_id: str, session: AsyncSession = Depends(get_session)):
    return account_to_response(await load_account(session, account_id))


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountResponse,
    summary="Update an account",
    description=(
        "Disabling an account removes it from scheduled syncs but keeps its data. "
        "Supplying a new password clears the failure count, restores `active` and "
        "lifts a `history_withheld_until` pause, so a fixed account is picked up "
        "again - this is how an account registered with a master password is "
        "moved to its investor password."
    ),
)
async def update_account(account_id: str, data: AccountUpdateRequest, session: AsyncSession = Depends(get_session)):
    account = await load_account(session, account_id)
    updated = await AccountService(session).update(account, password=data.password, enabled=data.enabled)
    return account_to_response(updated)


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an account and its synced data",
)
async def delete_account(account_id: str, session: AsyncSession = Depends(get_session)):
    await AccountService(session).delete(await load_account(session, account_id))
