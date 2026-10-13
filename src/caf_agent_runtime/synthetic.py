"""Deterministic test adapter; never starts a process or edits a workspace."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .contract import (
    AgentEvent, AgentResult, AgentStatus, CodingTask, FailureClass,
    _validate_terminal_failure,
)


@dataclass(frozen=True, slots=True)
class SyntheticOutcome:
    status: AgentStatus = AgentStatus.SUCCEEDED
    summary: str = "Synthetic operation completed"
    output: Mapping[str, object] = field(default_factory=dict)
    delay_seconds: float = 0.0
    failure_class: FailureClass | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must be nonnegative")
        if self.status not in (AgentStatus.SUCCEEDED, AgentStatus.FAILED, AgentStatus.TIMED_OUT):
            raise ValueError("synthetic outcomes must be success, failure, or timeout")
        _validate_terminal_failure(self.status, self.failure_class)


class SyntheticRuntime:
    """Script outcomes by operation; repeat the final outcome when exhausted.

    An instance keeps status only in memory. It is for contract and workflow tests,
    not recovery, CLI execution, workspace isolation, or production use.
    """

    def __init__(self, scenarios: Mapping[str, Sequence[SyntheticOutcome]] | None = None) -> None:
        self._scenarios = {key: tuple(value) for key, value in (scenarios or {}).items()}
        if any(not outcomes for outcomes in self._scenarios.values()):
            raise ValueError("scenario outcome sequences must not be empty")
        self._counts: dict[str, int] = {}
        self._statuses: dict[str, AgentStatus] = {}
        self._cancellations: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self, task: CodingTask, workspace: Path, session_id: str | None = None
    ) -> AgentResult:
        if not workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        async with self._lock:
            if task.run_id in self._statuses:
                raise ValueError(f"run_id already used: {task.run_id}")
            outcomes = self._scenarios.get(task.operation, (SyntheticOutcome(),))
            index = self._counts.get(task.operation, 0)
            outcome = outcomes[min(index, len(outcomes) - 1)]
            self._counts[task.operation] = index + 1
            self._statuses[task.run_id] = AgentStatus.RUNNING
            cancel_event = asyncio.Event()
            self._cancellations[task.run_id] = cancel_event

        started = AgentEvent(task.run_id, task.correlation_id, 1, "started", task.operation)
        status = AgentStatus.CANCELLED
        try:
            if outcome.delay_seconds:
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=outcome.delay_seconds)
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)
            if cancel_event.is_set():
                status = AgentStatus.CANCELLED
                summary = "Synthetic operation cancelled"
                failure_class = None
                exit_code = None
                output: Mapping[str, object] = {}
            else:
                status = outcome.status
                summary = outcome.summary
                failure_class = outcome.failure_class
                exit_code = outcome.exit_code
                output = outcome.output
        except asyncio.CancelledError:
            status = AgentStatus.CANCELLED
            summary = "Synthetic operation cancelled by caller"
            failure_class = None
            exit_code = None
            output = {}
            raise
        finally:
            async with self._lock:
                self._cancellations.pop(task.run_id, None)
                self._statuses[task.run_id] = status

        ended = AgentEvent(task.run_id, task.correlation_id, 2, status.value, summary)
        return AgentResult(
            run_id=task.run_id,
            correlation_id=task.correlation_id,
            status=status,
            summary=summary,
            events=(started, ended),
            failure_class=failure_class,
            exit_code=exit_code,
            session_id=session_id,
            output=output,
        )

    async def cancel(self, run_id: str) -> None:
        async with self._lock:
            if run_id not in self._statuses:
                raise KeyError(run_id)
            cancel_event = self._cancellations.get(run_id)
            if cancel_event is not None:
                cancel_event.set()

    async def get_status(self, run_id: str) -> AgentStatus:
        async with self._lock:
            return self._statuses[run_id]
