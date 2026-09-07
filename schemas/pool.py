from __future__ import annotations

from datetime import datetime
from typing import List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, computed_field

from pool.protocol import PoolStatus, WorkerState


class WorkerStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    worker_id: str
    terminal_path: str
    state: WorkerState
    pid: Optional[int]
    current_account_id: Optional[str]
    current_task_started_at: Optional[datetime]
    tasks_completed: int
    tasks_failed: int
    restarts: int
    last_error: Optional[str]
    queue_depth: int
    assigned_accounts: int = 0


class PoolStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workers: List[WorkerStatusResponse]
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
