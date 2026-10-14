import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from caf_agent_runtime import (
    AgentStatus, ClaudeRuntime, CodexRuntime, CodingTask, FailureClass,
)
from caf_agent_runtime import cli
from caf_agent_runtime import child_environment
from caf_agent_runtime.windows_job import ProcessTreeCleanupError


@pytest.fixture(autouse=True)
def mocked_process_tests_bypass_windows_job(monkeypatch: pytest.MonkeyPatch) -> None:
    # This module injects FakeProcess objects instead of OS processes. Live Job
    # Object behavior is exercised in test_windows_job_live.py.
    monkeypatch.setattr(cli, "_WINDOWS_JOB_ENABLED", False)


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
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self.exit_code
        return self.exit_code

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def task(provider: str) -> CodingTask:
    return CodingTask("run-1", "corr-1", "Sensitive task prompt", provider)


def codex_runtime(**kwargs: object) -> CodexRuntime:
    return CodexRuntime(executable=Path(sys.executable), **kwargs)


def test_child_environment_excludes_transport_and_model_credentials() -> None:
    source = {"Path": "C:/bin", "USERPROFILE": "C:/Users/test",
              "CODEX_HOME": "C:/Users/test/.codex",
              "CLAUDE_CONFIG_DIR": "C:/Users/test/.claude",
              "CAF_WORKER_TOKEN": "worker-secret", "CAF_API_TOKEN": "api-secret",
              "OPENAI_API_KEY": "model-secret", "ANTHROPIC_API_KEY": "model-secret",
              "CAF_QUEUE_TEST_DATABASE_URL": "db-secret", "UNRELATED": "hidden"}
    assert child_environment(source) == {
        "Path": "C:/bin", "USERPROFILE": "C:/Users/test",
        "CODEX_HOME": "C:/Users/test/.codex",
        "CLAUDE_CONFIG_DIR": "C:/Users/test/.claude",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope,expected_reason,expected_text", [
    ({"type": "result", "result": "PRIVATE prose",
      "structured_output": {"steps": ["inspect", "verify"]}},
     "ok", '{"steps":["inspect","verify"]}'),
    ({"type": "result", "result": '{"steps":["text fallback"]}'},
     "missing_structured_output", None),
    ({"type": "result", "structured_output": ["wrong root"]},
     "structured_output_type", None),
    ({"type": "error", "structured_output": {"steps": ["wrong event"]}},
     "unexpected_type_tag", None),
    ({"type": "result", "subtype": "error_during_execution",
      "structured_output": {"steps": ["wrong subtype"]}},
     "unexpected_subtype", None),
    ({"type": "result", "is_error": True,
      "structured_output": {"steps": ["wrong status"]}},
     "provider_error", None),
])
async def test_claude_schema_argument_and_structured_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    envelope: dict, expected_reason: str, expected_text: str | None,
) -> None:
    schema = {"type": "object", "properties": {"steps": {"type": "array"}},
              "required": ["steps"], "additionalProperties": False}
    runtime = ClaudeRuntime(tools=(), structured_schemas={"plan": schema})
    fake = FakeProcess(json.dumps(envelope, indent=2).encode())
    seen = []

    async def spawn(*args: object, **kwargs: object) -> FakeProcess:
        seen.append(args)
        return fake

    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spawn)
    request = CodingTask("plan-1", "corr-1", "PRIVATE prompt", "claude", "plan")
    result = await runtime.execute(request, tmp_path)
    assert result.status == (AgentStatus.SUCCEEDED if expected_reason == "ok"
                             else AgentStatus.FAILED)
    assert result.output["response_source"] == (
        "structured_output" if expected_reason == "ok" else "rejected_envelope")
    assert result.output["envelope_diagnostic"]["reason"] == expected_reason
    assert result.output["final_text"] == expected_text
    assert "PRIVATE prose" not in str(result.output)
    args = seen[0]
    assert args[args.index("--output-format") + 1] == "json"
    assert "--verbose" not in args
    assert json.loads(args[args.index("--json-schema") + 1]) == schema
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert "--strict-mcp-config" in args
    assert "--mcp-config" not in args
    assert "--json-schema" not in runtime.command(tmp_path, "review")
    assert runtime.command(tmp_path, "review")[4] == "stream-json"
    assert "--verbose" in runtime.command(tmp_path, "review")


@pytest.mark.asyncio
async def test_structured_envelope_preserves_schema_valid_unicode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    steps = ["😀" * 300 for _ in range(8)]
    fake = FakeProcess(json.dumps({"type": "result", "subtype": "success",
                                   "is_error": False,
                                   "structured_output": {"steps": steps}}).encode())
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    runtime = ClaudeRuntime(structured_schemas={"plan": {"type": "object"}})
    result = await runtime.execute(CodingTask("plan-2", "corr-2", "prompt", "claude", "plan"),
                                   tmp_path)
    assert result.output["response_source"] == "structured_output"
    assert json.loads(result.output["final_text"]) == {"steps": steps}
    assert len(result.output["final_text"].encode()) > 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope,expected_reason", [
    ('{"type":"result","structured_output":{"steps":["ok"],"steps":["bad"]}}',
     "duplicate_keys"),
    (json.dumps({"type": "result", "structured_output": {
        "steps": ["x" * 300 + " sk-abcdefghijklmnop"]}}),
     "sensitive_structured_output"),
])
async def test_structured_envelope_rejects_duplicate_or_sensitive_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    envelope: str, expected_reason: str,
) -> None:
    fake = FakeProcess(envelope.encode())
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    runtime = ClaudeRuntime(structured_schemas={"plan": {"type": "object"}})
    result = await runtime.execute(CodingTask("plan-3", "corr-3", "prompt", "claude", "plan"),
                                   tmp_path)
    assert result.status == AgentStatus.FAILED
    assert result.output["response_source"] == "rejected_envelope"
    assert result.output["envelope_diagnostic"]["reason"] == expected_reason
    assert result.output["final_text"] is None
    assert "sk-abcdefghijklmnop" not in str(result.output)


@pytest.mark.parametrize(
    "case", json.loads((Path(__file__).parent / "fixtures/claude_envelopes.json")
                       .read_text(encoding="utf-8")),
    ids=lambda case: case["name"],
)
def test_structured_envelope_contract_fixtures(case: dict) -> None:
    candidate, diagnostic = cli._structured_envelope(
        case["stdout"].encode(), {"type": "object"})
    assert diagnostic["reason"] == case["reason"]
    assert diagnostic["framing"] == case["framing"]
    for key in ("root_type", "type_tag", "subtype_tag", "status_tag", "is_error",
                "result_type", "structured_output_type",
                "nested_structured_output_type", "array_length_capped",
                "array_non_object_count_capped", "array_result_count_capped",
                "array_structured_count_capped", "array_error_count_capped",
                "array_result_position"):
        if key in case:
            assert diagnostic[key] == case[key]
    assert (candidate is not None) == (case["reason"] == "ok")
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_structured_envelope_invalid_utf8_and_size_are_fixed_codes() -> None:
    _, invalid = cli._structured_envelope(b"\xff", {"type": "object"})
    assert (invalid["reason"], invalid["framing"]) == ("invalid_utf8", "invalid_utf8")
    _, oversized = cli._structured_envelope(b"private", {"type": "object"}, True)
    assert (oversized["reason"], oversized["framing"]) == ("oversized_envelope", "oversized")
    assert oversized["stdout_bytes_capped"] == cli._MAX_CAPTURE + 1
    _, unsupported = cli._structured_envelope(
        b'{"structured_output":{"steps":["ok"]}}', {"type": ["object", "null"]})
    assert unsupported["reason"] == "unsupported_schema_root"


@pytest.mark.asyncio
async def test_structured_array_remains_rejected_without_copying_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = b'[{"type":"system","private":"PRIVATE"},{"type":"result",' \
          b'"structured_output":{"steps":["PRIVATE"]}}]'
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=FakeProcess(raw)))
    runtime = ClaudeRuntime(structured_schemas={"plan": {"type": "object"}})
    result = await runtime.execute(
        CodingTask("array-plan", "array-corr", "prompt", "claude", "plan"), tmp_path)
    assert result.status == AgentStatus.FAILED
    assert result.output["final_text"] is None
    assert result.output["response_source"] == "rejected_envelope"
    assert result.output["envelope_diagnostic"]["reason"] == "root_type"
    assert result.output["envelope_diagnostic"]["array_result_position"] == "last"
    assert "PRIVATE" not in json.dumps(result.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("escaped,expected_reason,expected_value", [
    (r"\ud800", "malformed_json", None),
    (r"\udc00", "malformed_json", None),
    (r"\ud83d\ude00", "ok", "😀"),
], ids=["lone_high_surrogate", "lone_low_surrogate", "valid_surrogate_pair"])
async def test_structured_envelope_surrogates_keep_fixed_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    escaped: str, expected_reason: str, expected_value: str | None,
) -> None:
    raw = ('{"structured_output":{"steps":["' + escaped + '"]}}').encode("ascii")
    fake = FakeProcess(raw)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    runtime = ClaudeRuntime(structured_schemas={"plan": {"type": "object"}})
    result = await runtime.execute(
        CodingTask("surrogate-plan", "corr-surrogate", "prompt", "claude", "plan"),
        tmp_path,
    )
    diagnostic = result.output["envelope_diagnostic"]
    assert diagnostic["reason"] == expected_reason
    assert result.status == (AgentStatus.SUCCEEDED if expected_reason == "ok"
                             else AgentStatus.FAILED)
    if expected_value is None:
        assert diagnostic["framing"] == "invalid_json"
        assert result.output["response_source"] == "rejected_envelope"
        assert result.output["final_text"] is None
        assert escaped not in json.dumps(result.output)
    else:
        assert json.loads(result.output["final_text"]) == {"steps": [expected_value]}


@pytest.mark.parametrize("stdout", [
    b'{"structured_output":' + b'{"x":' * 3500 + b'0' + b'}' * 3500 + b'}',
    b'{"structured_output":{"value":' + b'1' * 5000 + b'}}',
    b'{"structured_output":{}} ' + b'[' * 3500 + b'0' + b']' * 3500,
], ids=["deep_object", "long_integer", "deep_trailing_value"])
def test_structured_envelope_pathological_json_has_fixed_reason(stdout: bytes) -> None:
    candidate, diagnostic = cli._structured_envelope(stdout, {"type": "object"})
    assert candidate is None
    assert diagnostic["reason"] in {"malformed_json", "trailing_data", "multiple_json_values"}
    assert diagnostic["framing"] in {"invalid_json", "trailing_data", "multiple_json_values"}
    assert set(diagnostic) == {
        "reason", "framing", "root_type", "type_tag", "subtype_tag", "status_tag",
        "is_error", "result_type", "structured_output_type",
        "nested_structured_output_type", "stdout_bytes_capped",
        "array_length_capped", "array_non_object_count_capped",
        "array_result_count_capped", "array_structured_count_capped",
        "array_error_count_capped", "array_result_position",
    }


def test_codex_requires_an_explicit_existing_absolute_executable(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        CodexRuntime()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="absolute path"):
        CodexRuntime(executable=Path("codex.exe"))
    with pytest.raises(ValueError, match="absolute path"):
        CodexRuntime(executable=tmp_path / "missing.exe")
    if os.name == "nt":
        wrapper = tmp_path / "codex.cmd"
        wrapper.write_text("@echo off", encoding="utf-8")
        with pytest.raises(ValueError, match="native .exe"):
            CodexRuntime(executable=wrapper)
    runtime = codex_runtime()
    assert runtime.command(tmp_path)[0] == str(Path(sys.executable).resolve())
    assert runtime.command(tmp_path)[0] != "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_argument_array_stdin_and_redacted_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str
) -> None:
    if provider == "codex":
        runtime = codex_runtime()
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
    assert "env" in kwargs and "CAF_WORKER_TOKEN" not in kwargs["env"]
    if provider == "codex":
        assert "read-only" in args and "--json" in args
        assert args[0] == str(Path(sys.executable).resolve())
    else:
        assert "--restricted" in args and "--permission-prompts" in args
        assert "--verbose" in args


@pytest.mark.asyncio
async def test_nonzero_exit_and_missing_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeProcess(b"", exit_code=9)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    result = await codex_runtime().execute(task("codex"), tmp_path)
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
    result = await codex_runtime().execute(task("codex"), tmp_path)
    assert result.failure_class == FailureClass.INFRASTRUCTURE
    assert result.output["diagnostic_category"] == "authentication"
    assert result.output["diagnostic_detail"] == "Authentication failed"
    assert all("Authentication failed" not in event.message for event in result.events)


def test_diagnostic_detail_redacts_headers_tokens_urls_and_email() -> None:
    detail = "auth failed X-Auth-Token: abc123 password=abc123 session_id=abc123 Authorization: Bearer secretsecretsecretsecretsecretsecret; https://example.test/private?token=abc admin@example.test"
    safe = cli._safe_diagnostic_detail(detail)
    assert safe == "auth failed"
    for private in ("Bearer", "secretsecret", "example.test", "admin@", "abc123", "password", "session_id", "X-Auth"):
        assert private not in safe
    assert cli._safe_diagnostic_detail("X-Auth-Token: abc123") == "[provider detail withheld]"


def test_provider_event_type_is_allowlisted() -> None:
    assert cli._safe_event_type({"type": "item.completed"}) == "item.completed"
    assert cli._safe_event_type({"type": "sk-abcdefghijklmnop"}) == "unknown"
    assert cli._safe_event_type({"type": "X-Auth-Token"}) == "unknown"


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
    runtime = codex_runtime(timeout_seconds=0.01)
    result = await runtime.execute(task("codex"), tmp_path)
    assert result.status == AgentStatus.TIMED_OUT
    assert result.failure_class == FailureClass.TIMEOUT
    killed.assert_awaited_once_with(fake)

    waiting.clear()
    killed.reset_mock()
    runtime = codex_runtime(timeout_seconds=10)
    pending = asyncio.create_task(runtime.execute(task("codex"), tmp_path))
    await started.wait()
    await asyncio.sleep(0)
    await runtime.cancel("run-1")
    cancelled = await pending
    assert cancelled.status == AgentStatus.CANCELLED
    killed.assert_awaited_once_with(fake)


@pytest.mark.asyncio
async def test_stalled_stdin_is_bounded_by_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeProcess(b"")
    killed = []
    fake.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    blocker = asyncio.Event()

    async def blocked_drain() -> None:
        await blocker.wait()

    fake.stdin.drain = blocked_drain  # type: ignore[method-assign]
    killed = AsyncMock()
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    monkeypatch.setattr(cli, "_kill_tree", killed)
    result = await asyncio.wait_for(codex_runtime(timeout_seconds=0.01).execute(task("codex"), tmp_path), 1)
    assert result.status == AgentStatus.TIMED_OUT
    killed.assert_awaited_once_with(fake)


@pytest.mark.asyncio
async def test_cancelled_broken_pipe_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeProcess(b"")
    started = asyncio.Event()
    released = asyncio.Event()

    async def broken_after_cancel() -> None:
        started.set()
        await released.wait()
        raise BrokenPipeError("stdin closed")

    async def kill(process: FakeProcess) -> None:
        released.set()

    fake.stdin.drain = broken_after_cancel  # type: ignore[method-assign]
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    monkeypatch.setattr(cli, "_kill_tree", kill)
    runtime = codex_runtime()
    pending = asyncio.create_task(runtime.execute(task("codex"), tmp_path))
    await started.wait()
    await runtime.cancel("run-1")
    result = await asyncio.wait_for(pending, 1)
    assert result.status == AgentStatus.CANCELLED
    assert result.failure_class is None


@pytest.mark.asyncio
async def test_parent_exited_but_inherited_pipe_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeProcess(b"")
    fake.returncode = 0
    blocked = asyncio.Event()

    async def inherited_pipe(size: int) -> bytes:
        await blocked.wait()
        return b""

    fake.stdout.read = inherited_pipe  # type: ignore[method-assign]
    killed = AsyncMock()
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    monkeypatch.setattr(cli, "_kill_tree", killed)
    result = await asyncio.wait_for(codex_runtime(timeout_seconds=0.01).execute(task("codex"), tmp_path), 1)
    assert result.status == AgentStatus.TIMED_OUT
    killed.assert_awaited_once_with(fake)


@pytest.mark.asyncio
async def test_terminal_result_parsed_after_event_retention_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = [json.dumps({"type": "item.started"}) for _ in range(1005)]
    lines.append(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "late final text"}}))
    fake = FakeProcess(("\n".join(lines) + "\n").encode())
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    result = await codex_runtime().execute(task("codex"), tmp_path)
    assert result.output["final_text"] == "late final text"
    assert len(result.events) == 1001  # 1 start, 999 stream events, 1 terminal


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill path")
async def test_kill_tree_attempted_after_parent_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = FakeProcess(b"")
    parent.returncode = 0
    killer = FakeProcess(b"")
    spawn = AsyncMock(return_value=killer)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spawn)
    await cli._kill_tree(parent)
    assert spawn.await_args.args[:4] == ("taskkill", "/PID", str(parent.pid), "/T")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object failure path")
async def test_unverified_job_cleanup_is_visible_failure(monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    class FailingJob:
        def assign(self, pid: int) -> None:
            pass

        def active_processes(self) -> int:
            return 0

        def terminate_and_verify(self) -> None:
            raise ProcessTreeCleanupError("synthetic cleanup failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "_WINDOWS_JOB_ENABLED", True)
    monkeypatch.setattr(cli, "WindowsJob", FailingJob)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=FakeProcess(b"")))
    runtime = codex_runtime()
    with pytest.raises(ProcessTreeCleanupError, match="cleanup failure"):
        await runtime.execute(task("codex"), tmp_path)
    assert await runtime.get_status("run-1") == AgentStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object assignment path")
async def test_failed_job_assignment_kills_gated_supervisor(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    class RejectingJob:
        def assign(self, pid: int) -> None:
            raise ProcessTreeCleanupError("assignment refused")

        def terminate_and_verify(self) -> None:
            pass

        def close(self) -> None:
            pass

    fake = FakeProcess(b"")
    monkeypatch.setattr(cli, "_WINDOWS_JOB_ENABLED", True)
    monkeypatch.setattr(cli, "WindowsJob", RejectingJob)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake))
    with pytest.raises(ProcessTreeCleanupError, match="assignment refused"):
        await codex_runtime().execute(task("codex"), tmp_path)
    assert fake.killed
