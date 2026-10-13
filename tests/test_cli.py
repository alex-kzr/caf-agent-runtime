import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from caf_agent_runtime import (
    AgentStatus, ClaudeRuntime, CodexRuntime, CodingTask, FailureClass,
)
from caf_agent_runtime import cli


class FakeStdin:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class FakeStream:
    def __init__(self, data: bytes) -> None:
        self._lines = data.splitlines(keepends=True)
        self._data = data

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self, size: int) -> bytes:
        data, self._data = self._data[:size], self._data[size:]
        return data


class FakeProcess:
    pid = 1234

    def __init__(self, stdout: bytes, stderr: bytes = b"", exit_code: int = 0) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode: int | None = None
        self.exit_code = exit_code

    async def wait(self) -> int:
        self.returncode = self.exit_code
        return self.exit_code

    def kill(self) -> None:
        self.returncode = -9


def task(provider: str) -> CodingTask:
    return CodingTask("run-1", "corr-1", "Sensitive task prompt", provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_argument_array_stdin_and_redacted_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str
) -> None:
    if provider == "codex":
        runtime = CodexRuntime()
        event = {"type": "item.completed", "item": {
            "type": "agent_message", "text": "done sk-secretsecretsecret"}}
    else:
        runtime = ClaudeRuntime()
        event = {"type": "result", "result": "done sk-secretsecretsecret"}
    fake = FakeProcess((json.dumps(event) + "\n").encode(), b"access_token=secretsecret\n")
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def spawn(*args: object, **kwargs: object) -> FakeProcess:
        seen.append((args, kwargs))
        return fake

    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spawn)
    result = await runtime.execute(task(provider), tmp_path)
    assert result.status == AgentStatus.SUCCEEDED
    assert result.output["final_text"] == "done [REDACTED]"
    assert "secretsecret" not in str(result.output)
    assert all("secret" not in e.message for e in result.events)
    assert fake.stdin.written == b"Sensitive task prompt"
    args, kwargs = seen[0]
    assert "Sensitive task prompt" not in args
    assert kwargs["cwd"] == tmp_path.resolve()
    assert "shell" not in kwargs
    if provider == "codex":
        assert "read-only" in args and "--json" in args
    else:
        assert "--restricted" in args and "--permission-prompts" in args
        assert "--verbose" in args


@pytest.mark.asyncio
async def test_nonzero_exit_and_missing_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeProcess(b"", exit_code=9)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    result = await CodexRuntime().execute(task("codex"), tmp_path)
    assert result.status == AgentStatus.FAILED
    assert result.failure_class == FailureClass.AGENT
    assert result.exit_code == 9

    async def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("missing CLI")

    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", missing)
    failed = await ClaudeRuntime().execute(task("claude"), tmp_path)
    assert failed.status == AgentStatus.FAILED
    assert failed.failure_class == FailureClass.INFRASTRUCTURE


@pytest.mark.asyncio
async def test_auth_error_is_infrastructure_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    line = json.dumps({"type": "error", "message": "Authentication failed"}).encode() + b"\n"
    fake = FakeProcess(line, exit_code=1)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    result = await CodexRuntime().execute(task("codex"), tmp_path)
    assert result.failure_class == FailureClass.INFRASTRUCTURE
    assert result.output["diagnostic_category"] == "authentication"
    assert all("Authentication failed" not in event.message for event in result.events)


@pytest.mark.asyncio
async def test_timeout_and_cancel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeProcess(b"", exit_code=0)
    started = asyncio.Event()
    waiting = asyncio.Event()

    async def spawn(*args: object, **kwargs: object) -> FakeProcess:
        started.set()
        return fake

    async def blocked_read(size: int) -> bytes:
        await waiting.wait()
        return b""

    fake.stdout.read = blocked_read  # type: ignore[method-assign]
    killed = AsyncMock(side_effect=lambda process: waiting.set())
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cli, "_kill_tree", killed)
    runtime = CodexRuntime(timeout_seconds=0.01)
    result = await runtime.execute(task("codex"), tmp_path)
    assert result.status == AgentStatus.TIMED_OUT
    assert result.failure_class == FailureClass.TIMEOUT
    killed.assert_awaited_once_with(fake)

    waiting.clear()
    killed.reset_mock()
    runtime = CodexRuntime(timeout_seconds=10)
    pending = asyncio.create_task(runtime.execute(task("codex"), tmp_path))
    await started.wait()
    await asyncio.sleep(0)
    await runtime.cancel("run-1")
    cancelled = await pending
    assert cancelled.status == AgentStatus.CANCELLED
    killed.assert_awaited_once_with(fake)
