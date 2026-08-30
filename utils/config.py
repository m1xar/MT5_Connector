from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

_ENV_PREFIX = "MT5_API_"


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://mt5_api:mt5_api_pass@localhost:5432/mt5_api"

    # One terminal path per pool worker, ';'-separated. The number of paths is
    # the pool size.
    terminal_paths: str = ""
    terminal_portable: bool = False
    terminal_init_timeout_ms: int = 30000
    terminal_login_timeout_ms: int = 30000
    # A newly added account has never connected, and its first sync also has to
    # cold-start a terminal - measured at 100s+ when several come up at once.
    # Give it room before calling the account broken.
    terminal_initial_connect_timeout_ms: int = 120000
    # The terminal keeps downloading history after login returns; a sync that
    # reads through that window sees an empty account.
    history_settle_timeout_seconds: float = 30.0

    sync_interval_minutes: int = 15
    sync_scheduler_tick_seconds: int = 60
    sync_task_timeout_seconds: float = 900.0
    hard_sync_wait_timeout_seconds: float = 900.0
    max_task_retries: int = 2
    account_error_threshold: int = 3
    worker_start_timeout_seconds: float = 120.0
    enrich_mae_mfe: bool = True

    api_token: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8030
    cors_allow_origin: str = "*"

    log_level: str = "INFO"
    log_json: bool = True

    @property
    def terminals(self) -> list[str]:
        raw = self.terminal_paths.replace("\n", ";")
        return [part.strip() for part in raw.split(";") if part.strip()]

    def problems(self) -> list[str]:
        """Configuration that will not work, worth saying at startup.

        A pool that points at a path which is not there does not fail until the
        first sync lands on that worker, which is a long way from the cause.
        """
        issues: list[str] = []
        if not self.terminals:
            issues.append("MT5_API_TERMINAL_PATHS is empty; no terminal can be used")
        for path in self.terminals:
            if not Path(path).is_file():
                issues.append(f"terminal not found: {path}")
        if not self.api_token:
            issues.append(
                "MT5_API_API_TOKEN is empty, so every route is unauthenticated"
            )
        return issues


def _load_settings() -> Settings:
    """Read the environment, accepting both `MT5_API_X` and a bare `X`."""
    load_dotenv()
    values: dict[str, object] = {}
    for field in Settings.model_fields:
        for name in (f"{_ENV_PREFIX}{field.upper()}", field.upper()):
            if name in os.environ:
                values[field] = os.environ[name]
                break
    return Settings(**values)


settings = _load_settings()
