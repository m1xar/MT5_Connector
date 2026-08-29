from __future__ import annotations

import threading
from typing import Any

from utils.logging import get_logger, log_event

logger = get_logger(__name__)

RES_S_OK = 1


class TerminalError(RuntimeError):

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _import_mt5() -> Any:
    try:
        import MetaTrader5
    except ImportError as exc:
        raise TerminalError(
            "MetaTrader5 package is unavailable; the sync workers only run on Windows"
        ) from exc
    return MetaTrader5


class MT5Terminal:

    def __init__(
        self,
        path: str,
        *,
        init_timeout_ms: int = 60000,
        login_timeout_ms: int = 60000,
    ) -> None:
        self.path = path
        self.init_timeout_ms = init_timeout_ms
        self.login_timeout_ms = login_timeout_ms
        self._mt5: Any | None = None
        self._lock = threading.Lock()
        self._current_login: tuple[int, str] | None = None

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            raise TerminalError("terminal is not initialized")
        return self._mt5

    def initialize(self) -> None:
        module = _import_mt5()
        if not module.initialize(path=self.path, timeout=self.init_timeout_ms, portable=False):
            code, description = module.last_error()
            raise TerminalError(f"initialize failed: {description}", code=code)
        self._mt5 = module
        self._current_login = None
        log_event(logger, "info", "terminal.initialize.completed", path=self.path)

    def login(self, login: int, password: str, server: str) -> None:
        with self._lock:
            if self._current_login == (login, server):
                log_event(logger, "debug", "terminal.login.reused", login=login, server=server)
                return
            self._current_login = None
            ok = self.mt5.login(
                login,
                password=password,
                server=server,
                timeout=self.login_timeout_ms,
            )
            if not ok:
                code, description = self.mt5.last_error()
                raise TerminalError(
                    f"login failed for {login}@{server}: {description}", code=code
                )
            self._current_login = (login, server)
            log_event(logger, "info", "terminal.login.completed", login=login, server=server)

    def forget_login(self) -> None:
        self._current_login = None

    def shutdown(self) -> None:
        if self._mt5 is None:
            return
        try:
            self._mt5.shutdown()
        except Exception:
            log_event(logger, "warning", "terminal.shutdown.failed", path=self.path)
        finally:
            self._mt5 = None
            self._current_login = None

    def check_call(self, result: Any, what: str) -> Any:
        if result is not None:
            return result
        code, description = self.mt5.last_error()
        if code == RES_S_OK:
            return ()
        raise TerminalError(f"{what} failed: {description}", code=code)
