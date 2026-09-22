from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

_ENV_PREFIX = "MT5_API_"


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://mt5_api:mt5_api_pass@localhost:5432/mt5_api"
    db_pool_size: int = 30
    db_max_overflow: int = 50

    terminal_paths: str = ""
    terminal_portable: bool = False
    master_terminal_path: str = ""
    terminal_init_timeout_ms: int = 30000
    terminal_login_timeout_ms: int = 30000
    terminal_initial_connect_timeout_ms: int = 120000
    history_settle_timeout_seconds: float = 10.0
    history_withheld_pause_minutes: int = 60
    prune_cache_after_sync: bool = True
    webshare_api_key: str = ""
    proxy_probe_timeout_seconds: float = 10.0
    proxy_recheck_minutes: int = 60

    sync_interval_minutes: int = 15
    sync_scheduler_tick_seconds: int = 60
    sync_task_timeout_seconds: float = 900.0
    hard_sync_wait_timeout_seconds: float = 900.0
    max_task_retries: int = 2
    account_error_threshold: int = 3
    worker_start_timeout_seconds: float = 120.0
    enrich_mae_mfe: bool = True
    enrich_max_positions_per_sync: int = 5000
    enrich_budget_seconds: float = 300.0

    api_token: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8030
    cors_allow_origin: str = "*"

    log_level: str = "INFO"
    log_json: bool = True

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_digest_hour_utc: int = 6

    @property
    def terminals(self) -> list[str]:
        raw = self.terminal_paths.replace("\n", ";")
        return [part.strip() for part in raw.split(";") if part.strip()]

    @property
    def master_terminal(self) -> str | None:
        if self.master_terminal_path:
            return self.master_terminal_path
        if not self.terminals:
            return None
        first = Path(self.terminals[0])
        return str(first.parent.parent / "master" / first.name)

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.terminals:
            issues.append("MT5_API_TERMINAL_PATHS is empty; no terminal can be used")
        for path in self.terminals:
            if not Path(path).is_file():
                issues.append(f"terminal not found: {path}")
        if not self.api_token:
            issues.append("MT5_API_API_TOKEN is empty, so every route is unauthenticated")
        if not self.webshare_api_key:
            issues.append("MT5_API_WEBSHARE_API_KEY is empty, so the terminals connect without proxies")
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            issues.append("MT5_API_TELEGRAM_BOT_TOKEN and MT5_API_TELEGRAM_CHAT_ID must be set together; alerts are off")
        return issues


def _load_settings() -> Settings:
    load_dotenv()
    values: dict[str, object] = {}
    for field in Settings.model_fields:
        for name in (f"{_ENV_PREFIX}{field.upper()}", field.upper()):
            if name in os.environ:
                values[field] = os.environ[name]
                break
    return Settings(**values)


settings = _load_settings()
