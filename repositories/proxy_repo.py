from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain.models import MT5TerminalProxy, utc_now
from mt5api.proxy import Proxy


class TerminalProxyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all(self) -> dict[str, Proxy]:
        result = await self.session.execute(select(MT5TerminalProxy))
        return {row.terminal_path: Proxy(row.address, row.port) for row in result.scalars().all()}

    async def set(self, terminal_path: str, proxy: Proxy | None) -> None:
        row = await self.session.get(MT5TerminalProxy, terminal_path)
        if proxy is None:
            if row is not None:
                await self.session.delete(row)
        elif row is None:
            self.session.add(MT5TerminalProxy(terminal_path=terminal_path, address=proxy.address, port=proxy.port))
        else:
            row.address, row.port, row.assigned_at = proxy.address, proxy.port, utc_now()
            self.session.add(row)
        await self.session.flush()
