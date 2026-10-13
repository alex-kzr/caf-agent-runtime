"""In-process runtime boundary. Transport and persistence remain undecided."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Protocol


class AgentStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class FailureClass(StrEnum):
    INFRASTRUCTURE = "infrastructure"
    AGENT = "agent"
    TIMEOUT = "timeout"
    QUALITY_GATE = "quality_gate"


def _validate_terminal_failure(status: AgentStatus, failure_class: FailureClass | None) -> None:
    """Keep result and synthetic-outcome terminal semantics aligned."""
    if status == AgentStatus.RUNNING:
        raise ValueError("running is not a terminal result")
    if status in (AgentStatus.SUCCEEDED, AgentStatus.CANCELLED):
        if failure_class is not None:
            raise ValueError(f"{status.value} result cannot have a failure class")
    elif status == AgentStatus.TIMED_OUT:
        if failure_class != FailureClass.TIMEOUT:
            raise ValueError("timed out result requires timeout failure class")
    elif status == AgentStatus.FAILED:
        if failure_class is None or failure_class == FailureClass.TIMEOUT:
            raise ValueError("failed result requires a non-timeout failure class")


@dataclass(frozen=True, slots=True)
class CodingTask:
    run_id: str
    correlation_id: str
    prompt: str
    provider: str
    operation: str = "implement"

    def __post_init__(self) -> None:
        for name in ("run_id", "correlation_id", "prompt", "provider", "operation"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    run_id: str
    correlation_id: str
    sequence: int
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class AgentResult:
    run_id: str
    correlation_id: str
    status: AgentStatus
    summary: str
    events: tuple[AgentEvent, ...]
    failure_class: FailureClass | None = None
    exit_code: int | None = None
    session_id: str | None = None
    changed_files: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    output: Mapping[str, object] = field(default_factory=dict)
    usage: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _validate_terminal_failure(self.status, self.failure_class)


class CodingAgentRuntime(Protocol):
    async def execute(
        self, task: CodingTask, workspace: Path, session_id: str | None = None
    ) -> AgentResult: ...

    async def cancel(self, run_id: str) -> None: ...

    async def get_status(self, run_id: str) -> AgentStatus: ...
