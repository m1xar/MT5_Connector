from __future__ import annotations

from datetime import datetime
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, computed_field

from pool.manager import PoolStatus
from pool.protocol import WorkerState


class WorkerStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    worker_id: str
    terminal_path: str
    proxy: str | None = Field(default=None, validation_alias="proxy_endpoint")
    state: WorkerState
    pid: int | None
    current_account_id: str | None
    current_task_started_at: datetime | None
    tasks_completed: int
    tasks_failed: int
    restarts: int
    last_error: str | None
    queue_depth: int
    assigned_accounts: int = 0


class PoolStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workers: list[WorkerStatusResponse]
    idle_workers: int
    queue_depth: int
    hard_sync_queue_depth: int

    @computed_field
    @property
    def worker_count(self) -> int:
        return len(self.workers)


class HealthResponse(BaseModel):
    status: str
    healthy_workers: int
    worker_count: int


def pool_status_to_response(status: PoolStatus, assigned: Mapping[str, int]) -> PoolStatusResponse:
    response = PoolStatusResponse.model_validate(status)
    for worker in response.workers:
        worker.assigned_accounts = assigned.get(worker.terminal_path, 0)
    return response
