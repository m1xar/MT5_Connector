from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Any, Callable, Sequence

from mt5api.proxy import Proxy
from repositories.proxy_repo import TerminalProxyRepository
from utils.logging import log_event

logger = logging.getLogger(__name__)

_API = "https://proxy.webshare.io/api/v2"


class WebshareUnavailable(RuntimeError):
    pass


class WebshareClient:
    def __init__(self, api_key: str, *, timeout_seconds: float = 15.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        request = urllib.request.Request(
            _API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except Exception as exc:
            raise WebshareUnavailable(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        return json.loads(raw) if raw else None

    def list_proxies(self) -> list[Proxy]:
        proxies: list[Proxy] = []
        page = 1
        while True:
            data = self._request("GET", f"/proxy/list/?mode=direct&page={page}&page_size=100")
            proxies += [Proxy(row["proxy_address"], int(row["port"])) for row in data["results"] if row.get("valid")]
            if not data.get("next"):
                return proxies
            page += 1

    def my_ip(self) -> str:
        return self._request("GET", "/proxy/ipauthorization/whatsmyip/")["ip_address"]

    def authorizations(self) -> list[dict]:
        return self._request("GET", "/proxy/ipauthorization/")["results"]

    def authorize(self, ip: str) -> None:
        self._request("POST", "/proxy/ipauthorization/", {"ip_address": ip})

    def revoke(self, authorization_id: int) -> None:
        self._request("DELETE", f"/proxy/ipauthorization/{authorization_id}/")


class ProxyRegistry:
    def __init__(self, client: WebshareClient, session_factory: Callable[[], Any]) -> None:
        self.client = client
        self.session_factory = session_factory

    async def load(self, terminal_paths: Sequence[str]) -> dict[str, Proxy | None]:
        async with self.session_factory() as session:
            repo = TerminalProxyRepository(session)
            assigned = await repo.all()
            try:
                available = await self._available()
            except WebshareUnavailable as exc:
                log_event(logger, "warning", "proxy.api.unavailable", error=str(exc))
                return {path: assigned.get(path) for path in terminal_paths}

            valid = {proxy.endpoint for proxy in available}
            taken: set[str] = set()
            result: dict[str, Proxy | None] = {}
            for path in terminal_paths:
                current = assigned.get(path)
                if current and current.endpoint in valid and current.endpoint not in taken:
                    result[path] = current
                    taken.add(current.endpoint)
            for path in terminal_paths:
                if path in result:
                    continue
                proxy = next((candidate for candidate in available if candidate.endpoint not in taken), None)
                result[path] = proxy
                if proxy:
                    taken.add(proxy.endpoint)
                await repo.set(path, proxy)
                log_event(
                    logger, "info" if proxy else "warning", "proxy.assigned",
                    terminal_path=path, proxy=proxy.endpoint if proxy else None,
                    previous=assigned[path].endpoint if path in assigned else None,
                )
            for path in set(assigned) - set(terminal_paths):
                await repo.set(path, None)
            await session.commit()
            log_event(
                logger, "info", "proxy.loaded",
                terminals=len(terminal_paths), proxied=sum(1 for proxy in result.values() if proxy),
                available=len(available),
            )
            return result

    async def rotate(self, terminal_path: str, dead: Proxy | None) -> Proxy | None:
        async with self.session_factory() as session:
            repo = TerminalProxyRepository(session)
            assigned = await repo.all()
            try:
                available = await self._available()
            except WebshareUnavailable as exc:
                await repo.set(terminal_path, None)
                await session.commit()
                log_event(
                    logger, "warning", "proxy.unproxied",
                    terminal_path=terminal_path, dead=dead.endpoint if dead else None, error=str(exc),
                )
                return None
            taken = {proxy.endpoint for path, proxy in assigned.items() if path != terminal_path}
            if dead:
                taken.add(dead.endpoint)
            proxy = next((candidate for candidate in available if candidate.endpoint not in taken), None)
            await repo.set(terminal_path, proxy)
            await session.commit()
            log_event(
                logger, "info" if proxy else "warning", "proxy.rotated",
                terminal_path=terminal_path, dead=dead.endpoint if dead else None,
                proxy=proxy.endpoint if proxy else None,
            )
            return proxy

    async def _available(self) -> list[Proxy]:
        await asyncio.to_thread(self._ensure_ip_authorized)
        return await asyncio.to_thread(self.client.list_proxies)

    def _ensure_ip_authorized(self) -> None:
        ip = self.client.my_ip()
        existing = self.client.authorizations()
        if any(row["ip_address"] == ip for row in existing):
            return
        for row in existing:
            self.client.revoke(row["id"])
        self.client.authorize(ip)
        log_event(
            logger, "warning", "proxy.ip_authorized",
            ip=ip, replaced=[row["ip_address"] for row in existing],
        )
