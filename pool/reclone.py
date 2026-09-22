from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mt5api.proxy import remove_marker
from utils.procs import pids_of, terminate_all

from .health import build_of

_EXECUTABLES = ("terminal64.exe", "metatester64.exe", "MetaEditor64.exe")
_SKIP_DIRS = {"logs", "tester", "mql5/logs", "mql5/files/temp"}
_EMPTY_DIRS = {"bases"}
_SKIP_FILES = {"config/accounts.dat", "config/dnsperf.dat"}
_SKIP_PREFIXES = ("config/mt5api-",)


class RecloneError(RuntimeError):
    pass


@dataclass(slots=True)
class RecloneReport:
    broken_dir: str
    build: str | None
    servers_dat_carried: bool
    seconds: float


def reclone_terminal(terminal_path: str, master_path: str, quorum: tuple[str, str] | None) -> RecloneReport:
    started = time.monotonic()
    terminal_dir = Path(terminal_path).parent
    master_dir = Path(master_path).parent
    if not master_dir.is_dir():
        raise RecloneError(f"no master at {master_dir}")
    for exe in _EXECUTABLES:
        terminate_all(str(terminal_dir / exe))
    alive = [pid for exe in _EXECUTABLES for pid in pids_of(str(terminal_dir / exe))]
    if alive:
        raise RecloneError(f"processes still alive after termination: {alive}")

    for old in terminal_dir.parent.glob(f"{terminal_dir.name}.broken-*"):
        shutil.rmtree(old, ignore_errors=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    broken = terminal_dir.with_name(f"{terminal_dir.name}.broken-{stamp}")
    os.rename(terminal_dir, broken)

    shutil.copytree(master_dir, terminal_dir, ignore=_ignored_in(master_dir))
    (terminal_dir / "Bases").mkdir(exist_ok=True)
    build = _install_quorum_build(master_path, terminal_dir, quorum)
    carried = _carry_servers_dat(broken, terminal_dir)
    remove_marker(terminal_path)
    return RecloneReport(str(broken), build, carried, round(time.monotonic() - started, 1))


def _ignored_in(master_dir: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).relative_to(master_dir)
        skipped: set[str] = set()
        for name in names:
            key = (relative / name).as_posix().lower()
            if key in _SKIP_DIRS or key in _SKIP_FILES or key.startswith(_SKIP_PREFIXES):
                skipped.add(name)
            elif key in _EMPTY_DIRS:
                skipped.add(name)
        return skipped

    return ignore


def _install_quorum_build(master_path: str, terminal_dir: Path, quorum: tuple[str, str] | None) -> str | None:
    master_build = build_of(master_path, None, None)[0]
    if quorum is None or quorum[0] == master_build:
        return master_build
    source_dir = Path(quorum[1]).parent
    for exe in _EXECUTABLES:
        if (source_dir / exe).is_file():
            shutil.copy2(source_dir / exe, terminal_dir / exe)
    return quorum[0]


def _carry_servers_dat(broken: Path, terminal_dir: Path) -> bool:
    old = _servers_dat(broken)
    new = _servers_dat(terminal_dir)
    if old is None or not old.is_file():
        return False
    if new is not None and new.is_file() and new.stat().st_size >= old.stat().st_size:
        return False
    target = new or terminal_dir / "Config" / "servers.dat"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old, target)
    return True


def _servers_dat(root: Path) -> Path | None:
    for config in root.iterdir() if root.is_dir() else []:
        if config.is_dir() and config.name.lower() == "config":
            return config / "servers.dat"
    return None
