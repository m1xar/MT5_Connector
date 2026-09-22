from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .protocol import SyncResult

_SUSPECT_STRIKES = 2
_QUARANTINE_STRIKES = 3
_QUARANTINE_HARD_STRIKES = 2
_SOFT_QUARANTINE_STRIKES = 5
_SOFT_QUARANTINE_SERVERS = 2
_SOFT_QUARANTINE_SPAN = timedelta(minutes=15)


class TerminalState(str, Enum):
    healthy = "healthy"
    suspect = "suspect"
    quarantined = "quarantined"
    recloning = "recloning"


@dataclass
class TerminalHealth:
    terminal_path: str
    state: TerminalState = TerminalState.healthy
    strikes: int = 0
    hard_strikes: int = 0
    failed_servers: set[str] = field(default_factory=set)
    first_strike_at: datetime | None = None
    last_success_at: datetime | None = None
    last_launch_at: datetime | None = None
    last_pid: int | None = None
    build: str | None = None
    build_stat: tuple[int, float] | None = None
    quarantined_at: datetime | None = None
    quarantine_reason: str | None = None
    reclones: int = 0
    last_reclone_at: datetime | None = None
    reclone_error: str | None = None

    @property
    def usable(self) -> bool:
        return self.state in (TerminalState.healthy, TerminalState.suspect)

    def _reset_strikes(self) -> None:
        self.strikes = self.hard_strikes = 0
        self.failed_servers.clear()
        self.first_strike_at = None


@dataclass
class Tally:
    syncs_ok: int = 0
    syncs_failed: int = 0
    quarantines: int = 0
    reclones: int = 0
    replaced: int = 0
    liveupdate_killed: int = 0

    def reset(self) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, 0)


class HealthBoard:
    def __init__(self, terminal_paths: list[str], *, warmup_seconds: float) -> None:
        self.terminals = {path: TerminalHealth(path) for path in terminal_paths}
        self._warmup_seconds = warmup_seconds
        self._warm_until = 0.0
        self.tally = Tally()

    def start_warmup(self) -> None:
        self._warm_until = time.monotonic() + self._warmup_seconds

    @property
    def warming_up(self) -> bool:
        return time.monotonic() < self._warm_until

    def state(self, path: str) -> TerminalState:
        return self.terminals[path].state

    def record(self, path: str, server: str, result: SyncResult) -> TerminalState:
        health = self.terminals[path]
        now = datetime.now(timezone.utc)
        if result.ok:
            self.tally.syncs_ok += 1
        else:
            self.tally.syncs_failed += 1
        report = result.terminal
        if report is not None:
            if report.launched:
                health.last_launch_at, health.last_pid = now, report.pid
            elif report.attached:
                health.last_pid = report.pid
            if report.replaced:
                self.tally.replaced += 1
        if result.ok or (report is not None and report.initialized):
            self._success(health, now)
            return health.state
        if report is None or not (report.hard or report.soft):
            return health.state
        if report.soft and self.warming_up:
            return health.state
        if not health.usable:
            return health.state
        health.strikes += 1
        health.hard_strikes += int(report.hard)
        health.failed_servers.add(server)
        health.first_strike_at = health.first_strike_at or now
        reason = self._quarantine_reason(health, now)
        if reason:
            self.quarantine(path, reason, now)
        elif health.strikes >= _SUSPECT_STRIKES:
            health.state = TerminalState.suspect
        return health.state

    def _success(self, health: TerminalHealth, now: datetime) -> None:
        health.last_success_at = now
        health._reset_strikes()
        if health.state is TerminalState.suspect:
            health.state = TerminalState.healthy

    @staticmethod
    def _quarantine_reason(health: TerminalHealth, now: datetime) -> str | None:
        if health.strikes >= _QUARANTINE_STRIKES and health.hard_strikes >= _QUARANTINE_HARD_STRIKES:
            return f"{health.hard_strikes} of the last {health.strikes} cold starts died"
        if (
            health.strikes >= _SOFT_QUARANTINE_STRIKES
            and len(health.failed_servers) >= _SOFT_QUARANTINE_SERVERS
            and health.first_strike_at is not None
            and now - health.first_strike_at >= _SOFT_QUARANTINE_SPAN
        ):
            return f"{health.strikes} cold starts never answered across {len(health.failed_servers)} servers"
        return None

    def quarantine(self, path: str, reason: str, now: datetime | None = None) -> None:
        health = self.terminals[path]
        health.state = TerminalState.quarantined
        health.quarantined_at = now or datetime.now(timezone.utc)
        health.quarantine_reason = reason
        self.tally.quarantines += 1

    def placeable(self) -> list[str]:
        for states in ((TerminalState.healthy,), (TerminalState.healthy, TerminalState.suspect)):
            paths = [path for path, health in self.terminals.items() if health.state in states]
            if paths:
                return paths
        return list(self.terminals)

    def keepable(self) -> list[str]:
        return [path for path, health in self.terminals.items() if health.usable]

    def due_for_reclone(self, min_interval_seconds: float) -> str | None:
        now = datetime.now(timezone.utc)
        for path, health in self.terminals.items():
            if health.state is not TerminalState.quarantined:
                continue
            if health.last_reclone_at is None or (now - health.last_reclone_at).total_seconds() >= min_interval_seconds:
                return path
        return None

    def mark_recloning(self, path: str) -> None:
        health = self.terminals[path]
        health.state = TerminalState.recloning
        health.last_reclone_at = datetime.now(timezone.utc)
        health.reclones += 1
        health.reclone_error = None

    def mark_recloned(self, path: str) -> None:
        health = self.terminals[path]
        health.state = TerminalState.healthy
        health.quarantined_at = health.quarantine_reason = None
        health.build = health.build_stat = None
        health._reset_strikes()
        self.tally.reclones += 1

    def mark_reclone_failed(self, path: str, error: str) -> None:
        health = self.terminals[path]
        health.state = TerminalState.quarantined
        health.reclone_error = error

    def refresh_builds(self) -> None:
        for health in self.terminals.values():
            health.build, health.build_stat = build_of(health.terminal_path, health.build, health.build_stat)

    def quorum(self) -> tuple[str, str] | None:
        builds = Counter(
            health.build for health in self.terminals.values() if health.state is TerminalState.healthy and health.build
        )
        if not builds:
            return None
        build, votes = builds.most_common(1)[0]
        if votes < 2:
            return None
        source = next(
            health.terminal_path for health in self.terminals.values()
            if health.build == build and health.state is TerminalState.healthy
        )
        return build, source

    def snapshot(self) -> list[TerminalHealth]:
        return list(self.terminals.values())


def build_of(exe_path: str, known: str | None, known_stat: tuple[int, float] | None) -> tuple[str | None, tuple[int, float] | None]:
    try:
        stat = Path(exe_path).stat()
    except OSError:
        return None, None
    current = (stat.st_size, stat.st_mtime)
    if known is not None and known_stat == current:
        return known, known_stat
    digest = hashlib.md5()
    try:
        with open(exe_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None, None
    return digest.hexdigest(), current
