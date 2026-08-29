import os

from dotenv import load_dotenv
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://mt5_api:mt5_api_pass@localhost:5432/mt5_api"

    terminal_paths: str = ""
    terminal_init_timeout_ms: int = 60000
    terminal_login_timeout_ms: int = 60000

    sync_interval_minutes: int = 15
    sync_scheduler_tick_seconds: int = 60
    sync_task_timeout_seconds: float = 300.0
    hard_sync_wait_timeout_seconds: float = 300.0
    max_task_retries: int = 2
    worker_start_timeout_seconds: float = 120.0
    enrich_mae_mfe: bool = True

    api_token: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8030
    cors_allow_origin: str = "*"

    log_level: str = "INFO"
    log_json: bool = True


_ENV_PREFIX = "MT5_API_"


def _env_candidates(field_name: str) -> list[str]:
    upper = field_name.upper()
    return [f"{_ENV_PREFIX}{upper}", upper]


def _load_settings() -> Settings:
    load_dotenv()
    values: dict[str, object] = {}
    fields = Settings.model_fields if hasattr(Settings, "model_fields") else Settings.__fields__
    for field in fields:
        for env_var in _env_candidates(field):
            if env_var in os.environ:
                values[field] = os.environ[env_var]
                break

    return Settings(**values)


settings = _load_settings()


def terminal_path_list() -> list[str]:
    raw = settings.terminal_paths.replace("\n", ";")
    return [part.strip() for part in raw.split(";") if part.strip()]
