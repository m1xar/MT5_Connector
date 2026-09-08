from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from db.session import async_session_factory, dispose_db, init_db
from pool.manager import PoolManager
from services.proxy_service import ProxyRegistry, WebshareClient
from services.sync_service import SyncService
from utils.config import settings
from utils.logging import bind_context, configure_logging, log_event, reset_context

from .routes import accounts, data, pool as pool_routes, sync

configure_logging(settings.log_level, settings.log_json)
logger = logging.getLogger(__name__)

TAGS = [
    {"name": "Accounts", "description": "Registered MT5 trading accounts."},
    {"name": "Sync", "description": "Queue a sync, or force one to the front of the queue."},
    {"name": "Data", "description": "Synced trading data, served from the database."},
    {"name": "Pool", "description": "Terminal pool health and queue depth."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    for problem in settings.problems():
        log_event(logger, "warning", "app.startup.config", problem=problem)

    paths = settings.terminals
    proxy_registry = None
    proxies = None
    if settings.webshare_api_key:
        proxy_registry = ProxyRegistry(WebshareClient(settings.webshare_api_key), async_session_factory)
        proxies = await proxy_registry.load(paths)
    terminal_pool = PoolManager(
        paths,
        task_timeout_seconds=settings.sync_task_timeout_seconds,
        worker_start_timeout_seconds=settings.worker_start_timeout_seconds,
        max_task_retries=settings.max_task_retries,
        init_timeout_ms=settings.terminal_init_timeout_ms,
        login_timeout_ms=settings.terminal_login_timeout_ms,
        terminal_portable=settings.terminal_portable,
        history_settle_timeout_seconds=settings.history_settle_timeout_seconds,
        prune_cache_after_sync=settings.prune_cache_after_sync,
        log_level=settings.log_level,
        log_json=settings.log_json,
        enrich_mae_mfe=settings.enrich_mae_mfe,
        proxies=proxies,
        proxy_registry=proxy_registry,
        proxy_probe_timeout_seconds=settings.proxy_probe_timeout_seconds,
        proxy_recheck_minutes=settings.proxy_recheck_minutes,
        cold_start_timeout_ms=settings.terminal_initial_connect_timeout_ms,
    )
    sync_service = SyncService(terminal_pool)
    terminal_pool.on_result = sync_service.persist
    app.state.pool = terminal_pool
    app.state.sync_service = sync_service

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=max(8, len(paths) * 2 + 8), thread_name_prefix="mt5-pipe")
    )

    await terminal_pool.start()
    scheduler = asyncio.create_task(sync_service.run_scheduler(), name="sync-scheduler")
    log_event(logger, "info", "app.startup.completed", terminals=len(paths))

    try:
        yield
    finally:
        scheduler.cancel()
        try:
            await scheduler
        except asyncio.CancelledError:
            pass
        await terminal_pool.stop()
        await dispose_db()
        app.state.pool = None
        app.state.sync_service = None
        log_event(logger, "info", "app.shutdown.completed")


def create_app() -> FastAPI:
    app = FastAPI(
        title="MT5 Sync API",
        version="0.1.0",
        description=(
            "Syncs MT5 trading accounts into standardized FX models through a pool "
            "of MetaTrader 5 terminals."
        ),
        openapi_tags=TAGS,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=[settings.cors_allow_origin], allow_methods=["*"], allow_headers=["*"]
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        tokens = bind_context(request_id=request.headers.get("x-request-id") or str(uuid.uuid4()))
        try:
            return await call_next(request)
        finally:
            reset_context(tokens)

    for router in (accounts.router, sync.router, data.router, pool_routes.router):
        app.include_router(router)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=settings.api_host, port=settings.api_port))
    if sys.platform != "win32":
        server.run()
        return

    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


if __name__ == "__main__":
    run()
