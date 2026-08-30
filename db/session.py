from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from utils.config import settings
from utils.logging import get_logger, log_event

logger = get_logger(__name__)


def _async_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+psycopg_async://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg_async://", 1)
    return url


async_engine = create_async_engine(
    _async_url(settings.database_url),
    pool_pre_ping=True,
    connect_args={"options": "-c TimeZone=UTC"} if "postgresql" in settings.database_url else {},
)
async_session_factory = async_sessionmaker(async_engine, expire_on_commit=False)


def _add_new_columns(connection) -> None:
    """Add columns the models have grown since their tables were created.

    `create_all` only ever creates whole tables, so a new field on a table that
    already exists is invisible to it and every query then fails on a column
    the database has never heard of. Adding is the only shape allowed here:
    nothing is dropped, renamed or retyped, so this cannot lose data, and a
    column left behind by an older version is simply ignored.
    """
    inspector = inspect(connection)
    for table in SQLModel.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            connection.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                f"{column.type.compile(connection.dialect)}"
            )
            log_event(
                logger,
                "info",
                "db.column.added",
                table=table.name,
                column=column.name,
            )


def _sync_init(connection) -> None:
    SQLModel.metadata.create_all(connection)
    _add_new_columns(connection)


async def init_db() -> None:
    import domain.models

    async with async_engine.begin() as connection:
        await connection.run_sync(_sync_init)


async def dispose_db() -> None:
    await async_engine.dispose()
