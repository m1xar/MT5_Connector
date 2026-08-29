from __future__ import annotations

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
    connect_args={"options": "-c TimeZone=UTC"} if "postgresql" in settings.database_url else {},
)
async_session_factory = async_sessionmaker(async_engine, expire_on_commit=False)


def _sync_init(connection) -> None:
    SQLModel.metadata.create_all(connection)


async def init_db() -> None:
    import domain.models

    async with async_engine.begin() as connection:
        await connection.run_sync(_sync_init)


async def dispose_db() -> None:
    await async_engine.dispose()
