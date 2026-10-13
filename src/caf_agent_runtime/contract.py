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
        if self.status == AgentStatus.SUCCEEDED and self.failure_class is not None:
            raise ValueError("successful result cannot have a failure class")
        if self.status in (AgentStatus.FAILED, AgentStatus.TIMED_OUT) and self.failure_class is None:
            raise ValueError("failed or timed out result requires a failure class")
        if self.status == AgentStatus.RUNNING:
            raise ValueError("running is not a terminal result")


class CodingAgentRuntime(Protocol):
    async def execute(
        self, task: CodingTask, workspace: Path, session_id: str | None = None
    ) -> AgentResult: ...

    async def cancel(self, run_id: str) -> None: ...

    async def get_status(self, run_id: str) -> AgentStatus: ...
