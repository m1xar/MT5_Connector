from __future__ import annotations

import threading
import time
from typing import Any

from utils.logging import get_logger, log_event

logger = get_logger(__name__)

RES_S_OK = 1

# How long a worker waits for its turn to start a terminal before going
# ahead regardless. Longer than a queue of cold starts, short enough that a
# lock orphaned by a killed worker cannot stall the pool for long.
_START_LOCK_WAIT_SECONDS = 300.0

# The MetaTrader5 package reports transport failures as -1000x. They mean the
# terminal never came up on the IPC channel at all, which in practice is what a
# server the terminal has not been configured with looks like: the terminal
# starts, cannot resolve the server, and never answers.
_IPC_ERROR_CEILING = -10000
_AUTHORIZATION_FAILED = -6


class TerminalError(RuntimeError):

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def is_ipc(self) -> bool:
        """True when the terminal never answered, so its state is unusable."""
        return self.code is not None and self.code <= _IPC_ERROR_CEILING


def _diagnose(code: int | None, server: str) -> str:
    if code is not None and code <= _IPC_ERROR_CEILING:
        return (
            f" - the terminal never answered within the timeout, so it never got "
            f"as far as logging in: usually {server!r} is not one of the servers "
            f"this terminal has been configured with, otherwise the terminal "
            f"could not reach it"
        )
    if code == _AUTHORIZATION_FAILED:
        return " - the server answered and rejected these credentials"
    return ""


def _import_mt5() -> Any:
    try:
        import MetaTrader5
    except ImportError as exc:
        raise TerminalError(
            "MetaTrader5 package is unavailable; the sync workers only run on Windows"
        ) from exc
    return MetaTrader5


class MT5Terminal:
    """One terminal process, bound to one account at a time.

    ``mt5.initialize()`` cannot bring a terminal up without credentials: a
    terminal with no stored account sits on its account wizard and never answers
    the IPC channel. So the terminal is started lazily, on the first task, using
    that task's credentials, and later tasks switch accounts with ``login()``.
    """

    def __init__(
        self,
        path: str,
        *,
        init_timeout_ms: int = 30000,
        login_timeout_ms: int = 30000,
        portable: bool = False,
        start_lock: Any | None = None,
    ) -> None:
        self.path = path
        self.init_timeout_ms = init_timeout_ms
        self.login_timeout_ms = login_timeout_ms
        self.portable = portable
        # Shared across every worker: bringing a terminal up is heavy, and
        # several doing it at once take longer than the timeout allows.
        self.start_lock = start_lock
        self._mt5: Any | None = None
        self._lock = threading.Lock()
        # Keyed on the password too: a rotated password has to force a
        # re-login, and a revoked one has to start failing, rather than the
        # terminal quietly riding on the session it already holds.
        self._current_login: tuple[int, str, str] | None = None

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            raise TerminalError("terminal is not initialized")
        return self._mt5

    def connect(
        self,
        login: int,
        password: str,
        server: str,
        *,
        timeout_ms: int | None = None,
    ) -> None:
        """Point the terminal at an account, starting the process if needed."""
        with self._lock:
            if self._mt5 is None:
                self._start_serialised(
                    login, password, server, timeout_ms or self.init_timeout_ms
                )
                return
            if self._current_login == (login, server, password):
                log_event(logger, "debug", "terminal.login.reused", login=login, server=server)
                return
            self._login(login, password, server, timeout_ms or self.login_timeout_ms)

    def _start_serialised(
        self, login: int, password: str, server: str, timeout_ms: int
    ) -> None:
        """Start the terminal, one instance at a time across the pool.

        A cold start costs about twelve seconds on its own. Six of them at
        once do not cost twelve seconds each - they contend for the same CPU
        and the same server lookups, and two of the six measured here blew
        through a 120s timeout while a third, started after them, finished
        in 11.6s. Queueing the starts is faster than racing them, and turns
        a spurious error_connection into a short wait.

        The lock is advisory: if it cannot be had in time - a worker killed
        mid-start never releases it - the start goes ahead anyway rather
        than wedging the pool.
        """
        if self.start_lock is None:
            self._start(login, password, server, timeout_ms)
            return

        waited = time.monotonic()
        acquired = self.start_lock.acquire(timeout=_START_LOCK_WAIT_SECONDS)
        if not acquired:
            log_event(
                logger,
                "warning",
                "terminal.start.lock_timeout",
                path=self.path,
                waited_s=round(time.monotonic() - waited, 1),
            )
        try:
            self._start(login, password, server, timeout_ms)
        finally:
            if acquired:
                self.start_lock.release()

    def _start(self, login: int, password: str, server: str, timeout_ms: int) -> None:
        module = _import_mt5()
        ok = module.initialize(
            path=self.path,
            login=login,
            password=password,
            server=server,
            timeout=timeout_ms,
            portable=self.portable,
        )
        if not ok:
            code, description = module.last_error()
            raise TerminalError(
                f"initialize failed for {login}@{server}: {description}"
                f"{_diagnose(code, server)}",
                code=code,
            )
        self._mt5 = module
        self._current_login = (login, server, password)
        log_event(
            logger,
            "info",
            "terminal.initialize.completed",
            path=self.path,
            portable=self.portable,
            login=login,
            server=server,
            timeout_ms=timeout_ms,
        )

    def _login(self, login: int, password: str, server: str, timeout_ms: int) -> None:
        self._current_login = None
        ok = self.mt5.login(login, password=password, server=server, timeout=timeout_ms)
        if not ok:
            code, description = self.mt5.last_error()
            raise TerminalError(
                f"login failed for {login}@{server}: {description}{_diagnose(code, server)}",
                code=code,
            )
        self._current_login = (login, server, password)
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

    def reset(self) -> None:
        """Drop the terminal so the next task starts a fresh one.

        Once the IPC channel has timed out every later call on the same module
        times out too, so a transport failure has to take the process with it.
        """
        log_event(logger, "warning", "terminal.reset", path=self.path)
        self.shutdown()

    def check_call(self, result: Any, what: str) -> Any:
        if result is not None:
            return result
        code, description = self.mt5.last_error()
        if code == RES_S_OK:
            return ()
        raise TerminalError(f"{what} failed: {description}", code=code)
