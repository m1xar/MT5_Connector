from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class StartReport:
    attached: bool = False
    launched: bool = False
    pid: int | None = None
    killed: str | None = None
    died: bool = False
    replaced: bool = False
    initialized: bool = False
    ipc_failed: bool = False
    error_code: int | None = None

    @property
    def hard(self) -> bool:
        return self.died or self.killed == "zombie"

    @property
    def soft(self) -> bool:
        return self.ipc_failed and not self.hard and not self.initialized
