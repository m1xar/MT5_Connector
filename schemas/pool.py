from __future__ import annotations

from datetime import datetime
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, computed_field

from pool.health import PoolInspection, TerminalState
from pool.manager import PoolManager, PoolStatus
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


class ProxyHealth(BaseModel):
    assigned: str | None
    marker: str | None
    bound_pid: int | None
    bound_pid_alive: bool


class TerminalHealthResponse(BaseModel):
    worker_id: str
    terminal_path: str
    state: TerminalState
    worker_state: WorkerState
    build: str | None
    build_is_quorum: bool | None
    pids: list[int]
    launched_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    hard_failures: int
    proxy: ProxyHealth
    quarantined_at: datetime | None
    quarantine_reason: str | None
    reclones: int
    last_reclone_at: datetime | None
    reclone_error: str | None
    problems: list[str]


class PoolHealthResponse(BaseModel):
    status: str
    quorum_build: str | None
    master_build: str | None
    updaters: int
    terminals: list[TerminalHealthResponse]
    problems: list[str]


_UPDATER_STALE_SECONDS = 600.0


def pool_health_to_response(
    pool: PoolManager, inspection: PoolInspection, assigned: Mapping[str, int], status: str
) -> PoolHealthResponse:
    quorum = inspection.quorum[0] if inspection.quorum else None
    terminals = []
    problems: list[str] = []
    stranded = 0
    for item in inspection.terminals:
        health, worker = item.health, pool.worker_for(item.health.terminal_path)
        marker = item.marker
        bound_alive = marker is not None and marker.pid in item.pids
        own: list[str] = []
        if health.state is TerminalState.quarantined:
            own.append(f"quarantined since {health.quarantined_at:%Y-%m-%d %H:%M}Z: {health.quarantine_reason}")
            stranded += assigned.get(health.terminal_path, 0)
        if health.state is TerminalState.recloning:
            own.append("recloning")
        if health.reclone_error:
            own.append(f"reclone failed: {health.reclone_error}")
        if health.state is TerminalState.suspect:
            own.append(f"suspect: {health.strikes} strikes, {health.hard_strikes} hard")
        if quorum and health.build and health.build != quorum:
            own.append("build differs from the quorum")
        if item.pids and not bound_alive:
            own.append("the running instance was not launched by the worker")
        if worker.state is WorkerState.failed and health.usable:
            own.append(f"worker failed: {worker.last_error}")
        terminals.append(TerminalHealthResponse(
            worker_id=worker.worker_id,
            terminal_path=health.terminal_path,
            state=health.state,
            worker_state=worker.state,
            build=health.build,
            build_is_quorum=(health.build == quorum) if quorum and health.build else None,
            pids=item.pids,
            launched_at=item.launched_at,
            last_success_at=health.last_success_at,
            consecutive_failures=health.strikes,
            hard_failures=health.hard_strikes,
            proxy=ProxyHealth(
                assigned=worker.proxy_endpoint,
                marker=marker.proxy if marker else None,
                bound_pid=marker.pid if marker else None,
                bound_pid_alive=bound_alive,
            ),
            quarantined_at=health.quarantined_at,
            quarantine_reason=health.quarantine_reason,
            reclones=health.reclones,
            last_reclone_at=health.last_reclone_at,
            reclone_error=health.reclone_error,
            problems=own,
        ))
    if quorum and inspection.master_build and inspection.master_build != quorum:
        problems.append("the master is behind the quorum build; refresh it with make-master.sh")
    stale = sum(1 for _, age in inspection.updaters if age > _UPDATER_STALE_SECONDS)
    if stale:
        problems.append(f"{stale} LiveUpdate process(es) older than 10 minutes")
    if stranded:
        problems.append(f"{stranded} account(s) still pinned to quarantined terminals")
    if not pool.dispatcher_alive:
        problems.append("a dispatcher has died")
    return PoolHealthResponse(
        status=status,
        quorum_build=quorum,
        master_build=inspection.master_build,
        updaters=len(inspection.updaters),
        terminals=terminals,
        problems=problems,
    )
