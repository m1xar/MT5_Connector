from __future__ import annotations

import importlib

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from utils.config import settings


def _async_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+psycopg_async://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg_async://", 1)
    return url


async_engine = create_async_engine(
    _async_url(settings.database_url),
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    connect_args={"options": "-c TimeZone=UTC"} if "postgresql" in settings.database_url else {},
)
async_session_factory = async_sessionmaker(async_engine, expire_on_commit=False)


def _sync_init(connection) -> None:
    SQLModel.metadata.create_all(connection)
    inspector = inspect(connection)
    for table in SQLModel.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing:
                connection.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                    f"{column.type.compile(connection.dialect)}"
                )


async def init_db() -> None:
    importlib.import_module("domain.models")
    async with async_engine.begin() as connection:
        await connection.run_sync(_sync_init)


async def dispose_db() -> None:
    await async_engine.dispose()
