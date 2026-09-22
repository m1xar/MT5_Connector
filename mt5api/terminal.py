from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

from utils.logging import log_event
from utils.procs import pids_of, terminate_all

from .proxy import (
    Marker,
    Proxy,
    marker_for,
    probe,
    read_marker,
    remove_startup_ini,
    write_marker,
    write_startup_ini,
)
from .report import StartReport

logger = logging.getLogger(__name__)

RES_S_OK = 1
IPC_ERROR_CEILING = -10000
AUTHORIZATION_FAILED = -6

_START_LOCK_HOLD_SECONDS = 20.0
_LAUNCH_WAIT_SECONDS = 15.0
_LAUNCH_SETTLE_SECONDS = 3.0
_ATTACH_FAILURES_BEFORE_KILL = 2


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
    def __init__(
        self, message: str, code: int | None = None, *, terminal_lost: bool = False, proxy_dead: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.terminal_lost = terminal_lost
        self.proxy_dead = proxy_dead


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
        proxy: Proxy | None = None,
        managed: bool = False,
        proxy_probe_timeout_seconds: float = 10.0,
        cold_start_timeout_ms: int | None = None,
    ) -> None:
        self.path = path
        self.init_timeout_ms = init_timeout_ms
        self.login_timeout_ms = login_timeout_ms
        self.portable = portable
        self.start_gate = start_gate
        self.proxy = proxy
        self.managed = managed
        self.proxy_probe_timeout_seconds = proxy_probe_timeout_seconds
        self.cold_start_timeout_ms = cold_start_timeout_ms
        self._mt5: Any | None = None
        self._current_login: tuple[int, str, str] | None = None
        self._attach_failures = 0
        self.report: StartReport | None = None

    def take_report(self) -> StartReport | None:
        report, self.report = self.report, None
        return report

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
        if self.managed:
            if self._prepare(login, password, server) and self.cold_start_timeout_ms:
                timeout_ms = max(timeout_ms, self.cold_start_timeout_ms)
        try:
            ok = module.initialize(
                path=self.path, login=login, password=password, server=server,
                timeout=timeout_ms, portable=self.portable,
            )
        finally:
            remove_startup_ini(self.path)
        if not ok:
            code, description = module.last_error()
            self._note_initialize_failure(code)
            raise self.failure(f"initialize failed for {login}@{server}: {description}{_diagnose(code, server)}", code)
        if self.report is not None:
            self.report.initialized = True
        self._attach_failures = 0
        self._mt5 = module
        self._current_login = (login, password, server)
        log_event(
            logger, "info", "terminal.initialize.completed",
            path=self.path, portable=self.portable, login=login, server=server, timeout_ms=timeout_ms,
            proxy=marker_for(self.proxy) if self.managed else None,
        )

    def _note_initialize_failure(self, code: int | None) -> None:
        report = self.report
        if report is None:
            return
        report.ipc_failed, report.error_code = is_ipc(code), code
        if report.attached and is_ipc(code):
            self._attach_failures += 1
        if not pids_of(self.path):
            report.died = True

    def _prepare(self, login: int, password: str, server: str) -> bool:
        report = self.report = StartReport()
        wanted = marker_for(self.proxy)
        running = pids_of(self.path)
        if running:
            marker = read_marker(self.path)
            blocker = self._attach_blocker(running, marker, wanted)
            if blocker is None:
                report.attached, report.pid = True, marker.pid
                log_event(logger, "info", "terminal.attach", path=self.path, pids=running, proxy=wanted)
                return False
            report.killed = blocker
            log_event(
                logger, "warning", "terminal.kill",
                path=self.path, pids=running, reason=blocker, running_with=marker, wanted=wanted,
            )
            terminate_all(self.path)
        self._attach_failures = 0
        if self.proxy is not None and not probe(self.proxy, self.proxy_probe_timeout_seconds):
            raise TerminalError(f"proxy {self.proxy.endpoint} did not answer the probe", proxy_dead=True)
        ini = write_startup_ini(self.path, login, password, server, self.proxy)
        arguments = [self.path, *(["/portable"] if self.portable else []), f"/config:{ini}"]
        try:
            process = subprocess.Popen(arguments, close_fds=True)
        except OSError as exc:
            report.died = True
            raise TerminalError(f"terminal would not start: {exc}") from exc
        deadline = time.monotonic() + _LAUNCH_WAIT_SECONDS
        while process.pid not in pids_of(self.path) and time.monotonic() < deadline:
            time.sleep(0.5)
        time.sleep(_LAUNCH_SETTLE_SECONDS)
        alive = pids_of(self.path)
        if process.pid not in alive:
            if alive:
                report.replaced = True
                terminate_all(self.path)
                raise TerminalError(f"terminal {process.pid} handed over to {alive} without our config")
            report.died = True
            raise TerminalError(f"terminal {process.pid} exited within {_LAUNCH_SETTLE_SECONDS:.0f}s of launch")
        write_marker(self.path, self.proxy, process.pid)
        report.launched, report.pid = True, process.pid
        log_event(logger, "info", "terminal.launched", path=self.path, proxy=wanted, pid=process.pid)
        return True

    def _attach_blocker(self, running: list[int], marker: Marker | None, wanted: str) -> str | None:
        if marker is None or marker.proxy != wanted:
            return "proxy_mismatch"
        if marker.pid not in running:
            return "pid_mismatch"
        if self._attach_failures >= _ATTACH_FAILURES_BEFORE_KILL:
            return "zombie"
        return None

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
        proxy_dead = False
        if is_ipc(code) and self._mt5 is not None:
            lost = self._mt5.terminal_info() is None
            log_event(logger, "warning", "terminal.probe", path=self.path, error_code=code, terminal_lost=lost)
        if is_ipc(code) and self.proxy is not None:
            proxy_dead = not probe(self.proxy, self.proxy_probe_timeout_seconds)
            log_event(
                logger, "warning", "terminal.proxy.probe",
                path=self.path, proxy=self.proxy.endpoint, error_code=code, proxy_dead=proxy_dead,
            )
            if proxy_dead:
                lost = True
                message = f"{message} - proxy {self.proxy.endpoint} is not answering"
        return TerminalError(message, code=code, terminal_lost=lost, proxy_dead=proxy_dead)

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

    def reset(self, *, kill: bool = False) -> None:
        log_event(logger, "warning", "terminal.reset", path=self.path, kill=kill)
        self.shutdown()
        if kill:
            terminate_all(self.path)
