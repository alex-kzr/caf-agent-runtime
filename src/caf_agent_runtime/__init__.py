"""Public runtime contract and deterministic synthetic adapter."""

from .contract import (
    AgentEvent,
    AgentResult,
    AgentStatus,
    CodingAgentRuntime,
    CodingTask,
    FailureClass,
)
from .synthetic import SyntheticOutcome, SyntheticRuntime
from .cli import ClaudeRuntime, CodexRuntime, HostCliRuntime, child_environment

__all__ = [
    "AgentEvent",
    "AgentResult",
    "AgentStatus",
    "CodingAgentRuntime",
    "CodingTask",
    "ClaudeRuntime",
    "CodexRuntime",
    "child_environment",
    "FailureClass",
    "HostCliRuntime",
    "SyntheticOutcome",
    "SyntheticRuntime",
]
