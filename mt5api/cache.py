from __future__ import annotations

import logging
from pathlib import Path

from utils.logging import log_event

logger = logging.getLogger(__name__)


def prune_price_cache(terminal_path: str) -> tuple[float, int]:
    root = Path(terminal_path).parent / "Bases"
    if not root.is_dir():
        return 0.0, 0

    freed = 0.0
    locked = 0
    for server_dir in root.iterdir():
        history = server_dir / "history"
        if not history.is_dir():
            continue
        for symbol_dir in history.iterdir():
            if not symbol_dir.is_dir():
                continue
            files = list(symbol_dir.glob("*.hcc")) + [
                item for item in (symbol_dir / "cache").glob("*") if item.is_file()
            ]
            for path in files:
                try:
                    size = path.stat().st_size
                    path.unlink()
                except OSError:
                    locked += 1
                    continue
                freed += size / 1048576

    if freed or locked:
        log_event(logger, "info", "cache.pruned", terminal=root.parent.name, freed_mb=round(freed, 1), in_use=locked)
    return round(freed, 1), locked
