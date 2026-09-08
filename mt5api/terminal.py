from __future__ import annotations

import logging
import time
from typing import Any

from utils.logging import log_event

logger = logging.getLogger(__name__)

RES_S_OK = 1
IPC_ERROR_CEILING = -10000
AUTHORIZATION_FAILED = -6

_START_LOCK_HOLD_SECONDS = 20.0


def is_ipc(code: int | None) -> bool:
    return code is not None and code <= IPC_ERROR_CEILING


def _diagnose(code: int | None, server: str) -> str:
    if is_ipc(code):
        return (
            f" - the terminal never answered within the timeout, so it never got "
            f"as far as logging in: usually {server!r} is not one of the servers "
            f"this terminal has been configured with, otherwise the terminal "
            f"could not reach it"
        )
    if code == AUTHORIZATION_FAILED:
        return " - the server answered and rejected these credentials"
    return ""


class TerminalError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, *, terminal_lost: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.terminal_lost = terminal_lost


class StartGate:
    def __init__(self, ctx: Any) -> None:
        self._lock = ctx.Lock()
        self._since = ctx.Value("d", 0.0)

    def acquire(self) -> bool:
        while True:
            if self._lock.acquire(timeout=1.0):
                self._since.value = time.time()
                return True
            since = self._since.value
            if since and time.time() - since >= _START_LOCK_HOLD_SECONDS:
                self._since.value = time.time()
                return False

    def release(self, acquired: bool) -> None:
        if acquired:
            self._since.value = 0.0
            self._lock.release()


class MT5Terminal:
    def __init__(
        self,
        path: str,
        *,
        init_timeout_ms: int = 30000,
        login_timeout_ms: int = 30000,
        portable: bool = False,
        start_gate: StartGate | None = None,
    ) -> None:
        self.path = path
        self.init_timeout_ms = init_timeout_ms
        self.login_timeout_ms = login_timeout_ms
        self.portable = portable
        self.start_gate = start_gate
        self._mt5: Any | None = None
        self._current_login: tuple[int, str, str] | None = None

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            raise TerminalError("terminal is not initialized")
        return self._mt5

    def connect(self, login: int, password: str, server: str, *, timeout_ms: int | None = None) -> None:
        if self._mt5 is not None:
            if self._current_login != (login, password, server):
                self._login(login, password, server, timeout_ms or self.login_timeout_ms)
            return
        if self.start_gate is None:
            self._start(login, password, server, timeout_ms or self.init_timeout_ms)
            return
        waited = time.monotonic()
        acquired = self.start_gate.acquire()
        if not acquired:
            log_event(
                logger, "info", "terminal.start.unserialised",
                path=self.path, waited_s=round(time.monotonic() - waited, 1),
            )
        try:
            self._start(login, password, server, timeout_ms or self.init_timeout_ms)
        finally:
            self.start_gate.release(acquired)

    def _start(self, login: int, password: str, server: str, timeout_ms: int) -> None:
        try:
            import MetaTrader5 as module
        except ImportError as exc:
            raise TerminalError("MetaTrader5 package is unavailable; the workers only run on Windows") from exc
        ok = module.initialize(
            path=self.path, login=login, password=password, server=server,
            timeout=timeout_ms, portable=self.portable,
        )
        if not ok:
            code, description = module.last_error()
            raise self.failure(f"initialize failed for {login}@{server}: {description}{_diagnose(code, server)}", code)
        self._mt5 = module
        self._current_login = (login, password, server)
        log_event(
            logger, "info", "terminal.initialize.completed",
            path=self.path, portable=self.portable, login=login, server=server, timeout_ms=timeout_ms,
        )

    def _login(self, login: int, password: str, server: str, timeout_ms: int) -> None:
        self._current_login = None
        if not self.mt5.login(login, password=password, server=server, timeout=timeout_ms):
            code, description = self.mt5.last_error()
            raise self.failure(f"login failed for {login}@{server}: {description}{_diagnose(code, server)}", code)
        self._current_login = (login, password, server)
        log_event(logger, "info", "terminal.login.completed", login=login, server=server)

    def forget_login(self) -> None:
        self._current_login = None

    def failure(self, message: str, code: int | None) -> TerminalError:
        lost = False
        if is_ipc(code) and self._mt5 is not None:
            lost = self._mt5.terminal_info() is None
            log_event(logger, "warning", "terminal.probe", path=self.path, error_code=code, terminal_lost=lost)
        return TerminalError(message, code=code, terminal_lost=lost)

    def check_call(self, result: Any, what: str) -> Any:
        if result is not None:
            return result
        code, description = self.mt5.last_error()
        if code == RES_S_OK:
            return ()
        raise self.failure(f"{what} failed: {description}", code)

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

    def reset(self) -> None:
        log_event(logger, "warning", "terminal.reset", path=self.path)
        self.shutdown()
