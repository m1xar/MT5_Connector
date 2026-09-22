from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import urllib.request

from .logging import log_event

logger = logging.getLogger(__name__)

_DEDUPE_SECONDS = 600.0


class Telegram:
    def __init__(self, token: str, chat_id: str, *, timeout_seconds: float = 10.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds
        self._recent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps({"chat_id": self.chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.status == 200
        except Exception as exc:
            log_event(logger, "warning", "alert.failed", error=f"{type(exc).__name__}: {exc}")
            return False

    def notify(self, text: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        self._recent = {key: at for key, at in self._recent.items() if now - at < _DEDUPE_SECONDS}
        if text in self._recent:
            return
        self._recent[text] = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.send(text)
            return
        loop.run_in_executor(None, self.send, text)


def host_uptime_seconds() -> float | None:
    if sys.platform == "win32":
        import ctypes

        return ctypes.windll.kernel32.GetTickCount64() / 1000
    try:
        with open("/proc/uptime") as handle:
            return float(handle.read().split()[0])
    except OSError:
        return None


def describe_duration(seconds: float) -> str:
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
