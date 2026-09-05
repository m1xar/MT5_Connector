from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from pool.protocol import PoolStatus


class WorkerStatusResponse(BaseModel):
    worker_id: str
    terminal_path: str
    state: str
    pid: Optional[int]
    current_account_id: Optional[str]
    current_task_started_at: Optional[str]
    tasks_completed: int
    tasks_failed: int
    restarts: int
    last_error: Optional[str]


class PoolStatusResponse(BaseModel):
    workers: List[WorkerStatusResponse]
    worker_count: int
    idle_workers: int
    queue_depth: int
    hard_sync_queue_depth: int


class HealthResponse(BaseModel):
    status: str
    healthy_workers: int
    worker_count: int


def pool_status_to_response(status: PoolStatus) -> PoolStatusResponse:
    return PoolStatusResponse(
        workers=[
            WorkerStatusResponse(
                worker_id=worker.worker_id,
                terminal_path=worker.terminal_path,
                state=worker.state.value,
                pid=worker.pid,
                current_account_id=worker.current_account_id,
                current_task_started_at=(
                    worker.current_task_started_at.isoformat() if worker.current_task_started_at else None
                ),
                tasks_completed=worker.tasks_completed,
                tasks_failed=worker.tasks_failed,
                restarts=worker.restarts,
                last_error=worker.last_error,
            )
            for worker in status.workers
        ],
        worker_count=len(status.workers),
        idle_workers=status.idle_workers,
        queue_depth=status.queue_depth,
        hard_sync_queue_depth=status.hard_sync_queue_depth,
    )
