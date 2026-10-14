"""Host CLI adapters. This module never invokes a shell or copies credentials."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Literal, Mapping

from .contract import AgentEvent, AgentResult, AgentStatus, CodingTask, FailureClass
from .windows_job import ProcessTreeCleanupError, WindowsJob


_WINDOWS_JOB_ENABLED = os.name == "nt"


_CHILD_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PYTHONIOENCODING", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
})


def child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Pass OS lookup and CLI config paths, never the worker or model secrets."""
    source = os.environ if source is None else source
    return {key: value for key, value in source.items()
            if key.upper() in _CHILD_ENV_KEYS and value}


_SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"(?:api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*[^\s,}\]]+)"
)
_MAX_TEXT = 4096
_MAX_CAPTURE = 65536
_MAX_STRUCTURED_TEXT = 16384
_KNOWN_EVENT_TYPES = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.updated", "item.completed", "error",
    "system", "assistant", "user", "result", "stream_event", "rate_limit_event",
})
_SAFE_DIAGNOSTIC_PHRASES = re.compile(
    r"(?i)(?<![\w-])(?:401\s+unauthorized|403\s+forbidden|"
    r"authentication failed|failed to authenticate|not authenticated|"
    r"login required|log in again|failed to refresh token|token expired|"
    r"invalid token|permission denied|access denied|network error|"
    r"connection failed|request failed|stream disconnected|"
    r"unauthorized|forbidden|auth failed)(?![\w-])"
)


def _redact(value: str) -> str:
    return _SECRET.sub("[REDACTED]", value)[:_MAX_TEXT]


class _DuplicateJSONField(ValueError):
    pass


class _NonJSONConstant(ValueError):
    pass


def _reject_json_constant(_value: str) -> None:
    raise _NonJSONConstant


def _unique_json_fields(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONField
        value[key] = item
    return value


def _json_kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def _field_kind(value: dict, name: str) -> str:
    return _json_kind(value[name]) if name in value else "missing"


def _tag_kind(value: dict, name: str, expected: str) -> str:
    if name not in value:
        return "missing"
    if not isinstance(value[name], str):
        return "non_string"
    return expected if value[name] == expected else "other"


def _error_flag_kind(value: dict) -> str:
    if "is_error" not in value:
        return "missing"
    if value["is_error"] is True:
        return "true"
    if value["is_error"] is False:
        return "false"
    return "non_boolean"


def _structured_envelope(data: bytes, schema: dict,
                         oversized: bool = False) -> tuple[str | None, dict]:
    """Extract one documented JSON result with fixed metadata, never raw fields."""
    diagnostic = {
        "reason": "ok", "framing": "single_json", "root_type": None,
        "type_tag": None, "subtype_tag": None, "status_tag": None,
        "is_error": None,
        "result_type": None, "structured_output_type": None,
        "nested_structured_output_type": None,
        "stdout_bytes_capped": min(len(data), _MAX_CAPTURE + 1),
    }

    def reject(reason: str) -> tuple[None, dict]:
        diagnostic["reason"] = reason
        return None, diagnostic

    if oversized:
        diagnostic["framing"] = "oversized"
        diagnostic["stdout_bytes_capped"] = _MAX_CAPTURE + 1
        return reject("oversized_envelope")
    if not data.strip():
        diagnostic["framing"] = "empty"
        return reject("empty_stdout")
    try:
        raw = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        diagnostic["framing"] = "invalid_utf8"
        return reject("invalid_utf8")
    try:
        envelope = json.loads(raw, object_pairs_hook=_unique_json_fields,
                              parse_constant=_reject_json_constant)
    except _DuplicateJSONField:
        diagnostic["framing"] = "duplicate_keys"
        return reject("duplicate_keys")
    except _NonJSONConstant:
        diagnostic["framing"] = "invalid_json"
        return reject("malformed_json")
    except json.JSONDecodeError as exc:
        diagnostic["framing"] = "invalid_json"
        if exc.msg == "Extra data":
            suffix = raw[exc.pos:].lstrip()
            try:
                json.JSONDecoder(object_pairs_hook=_unique_json_fields,
                                 parse_constant=_reject_json_constant).raw_decode(suffix)
            except (json.JSONDecodeError, _DuplicateJSONField, _NonJSONConstant,
                    RecursionError, ValueError):
                diagnostic["framing"] = "trailing_data"
                return reject("trailing_data")
            diagnostic["framing"] = "multiple_json_values"
            return reject("multiple_json_values")
        return reject("malformed_json")
    except (RecursionError, ValueError):
        diagnostic["framing"] = "invalid_json"
        return reject("malformed_json")
    diagnostic["root_type"] = _json_kind(envelope)
    if not isinstance(envelope, dict):
        return reject("root_type")
    diagnostic["type_tag"] = _tag_kind(envelope, "type", "result")
    diagnostic["subtype_tag"] = _tag_kind(envelope, "subtype", "success")
    diagnostic["status_tag"] = _tag_kind(envelope, "status", "success")
    if envelope.get("status") == "error":
        diagnostic["status_tag"] = "error"
    diagnostic["is_error"] = _error_flag_kind(envelope)
    diagnostic["result_type"] = _field_kind(envelope, "result")
    diagnostic["structured_output_type"] = _field_kind(envelope, "structured_output")
    result = envelope.get("result")
    diagnostic["nested_structured_output_type"] = (
        _field_kind(result, "structured_output") if isinstance(result, dict) else "missing")
    if diagnostic["is_error"] == "true":
        return reject("provider_error")
    if diagnostic["status_tag"] == "error":
        return reject("provider_error")
    if diagnostic["is_error"] == "non_boolean":
        return reject("invalid_error_flag")
    # The documented single-JSON envelope promises structured_output, but does
    # not require type/subtype fields. Check their values only when present.
    if diagnostic["type_tag"] not in ("missing", "result"):
        return reject("unexpected_type_tag")
    if diagnostic["subtype_tag"] not in ("missing", "success"):
        return reject("unexpected_subtype")
    if diagnostic["status_tag"] not in ("missing", "success"):
        return reject("unexpected_status")
    if "structured_output" not in envelope:
        return reject("missing_structured_output")
    expected = schema.get("type")
    if not isinstance(expected, str) or expected not in {
            "object", "array", "string", "number", "boolean", "null"}:
        return reject("unsupported_schema_root")
    if diagnostic["structured_output_type"] != expected:
        return reject("structured_output_type")
    structured = envelope["structured_output"]
    try:
        candidate = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    except (RecursionError, ValueError):
        diagnostic["framing"] = "invalid_json"
        return reject("malformed_json")
    if len(candidate.encode("utf-8")) > _MAX_STRUCTURED_TEXT:
        return reject("oversized_structured_output")
    if _SECRET.search(candidate):
        return reject("sensitive_structured_output")
    return candidate, diagnostic


def _safe_diagnostic_detail(value: str) -> str:
    """Retain only exact matched phrases from a fixed safe vocabulary."""
    phrases = [match.group(0) for match in _SAFE_DIAGNOSTIC_PHRASES.finditer(value)]
    return "; ".join(phrases[:4]) or "[provider detail withheld]"


def _safe_event_type(item: object) -> str:
    if not isinstance(item, dict):
        return "unstructured"
    event_type = item.get("type")
    if isinstance(event_type, str) and event_type in _KNOWN_EVENT_TYPES:
        return event_type
    return "unknown"


def _result_text(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    # Claude emits the final text in a result event. Codex emits agent message
    # items. Other event fields are not copied into artifacts.
    if item.get("type") == "result" and isinstance(item.get("result"), str):
        return _redact(item["result"])
    if item.get("type") == "item.completed":
        nested = item.get("item")
        if isinstance(nested, dict) and nested.get("type") == "agent_message":
            text = nested.get("text")
            if isinstance(text, str):
                return _redact(text)
    return None


def _diagnostic_category(message: str) -> str | None:
    lower = message.lower()
    if any(term in lower for term in ("auth", "login", "credential", "401", "token expired")):
        return "authentication"
    if any(term in lower for term in ("network", "connection", "dns", "timed out")):
        return "network"
    return None


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    # Descendants may still own stdout/stderr after the direct CLI has exited.
    # Attempt tree cleanup even when its return code is already available.
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                env=child_environment(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        except (OSError, TimeoutError):
            if process.returncode is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()


class HostCliRuntime:
    """One CLI process per run; no remote transport or persistent state."""

    def __init__(
        self,
        provider: Literal["codex", "claude"],
        *,
        timeout_seconds: float = 120,
        codex_sandbox: Literal["read-only", "workspace-write"] = "read-only",
        codex_executable: Path | None = None,
        claude_tools: tuple[str, ...] = (),
        structured_schemas: Mapping[str, dict] | None = None,
    ) -> None:
        if provider not in ("codex", "claude"):
            raise ValueError("provider must be codex or claude")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if codex_sandbox not in ("read-only", "workspace-write"):
            raise ValueError("unsupported Codex sandbox mode")
        if provider == "codex":
            if codex_executable is None or not codex_executable.is_absolute() or not codex_executable.is_file():
                raise ValueError("Codex requires an absolute path to an existing CLI executable")
            if os.name == "nt" and codex_executable.suffix.lower() != ".exe":
                raise ValueError("Codex on Windows requires a native .exe, not a shell wrapper")
            codex_executable = codex_executable.resolve(strict=True)
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.codex_sandbox = codex_sandbox
        self.codex_executable = codex_executable
        self.claude_tools = claude_tools
        self.structured_schemas = dict(structured_schemas or {})
        if self.structured_schemas and provider != "claude":
            raise ValueError("structured schemas require Claude runtime")
        self._statuses: dict[str, AgentStatus] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._jobs: dict[str, WindowsJob] = {}
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    def command(self, workspace: Path, operation: str | None = None) -> tuple[str, ...]:
        if self.provider == "codex":
            assert self.codex_executable is not None
            return (
                str(self.codex_executable), "exec", "--json", "--ephemeral", "--sandbox", self.codex_sandbox,
                "--cd", str(workspace), "-",
            )
        schema = self.structured_schemas.get(operation) if operation else None
        command = (
            "claude", "--print", "--verbose", "--output-format",
            "json" if schema is not None else "stream-json",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--restricted", "--no-session-persistence", "--tools",
            ",".join(self.claude_tools),
        )
        return command + (("--json-schema", json.dumps(schema, separators=(",", ":")))
                          if schema is not None else ())

    async def execute(
        self, task: CodingTask, workspace: Path, session_id: str | None = None
    ) -> AgentResult:
        if task.provider != self.provider:
            raise ValueError("task provider does not match runtime")
        if session_id is not None:
            raise ValueError("session resume is not verified for this adapter")
        if not workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        workspace = workspace.resolve()
        async with self._lock:
            if task.run_id in self._statuses:
                raise ValueError(f"run_id already used: {task.run_id}")
            self._statuses[task.run_id] = AgentStatus.RUNNING

        events = [AgentEvent(task.run_id, task.correlation_id, 1, "started", self.provider)]
        final_text: str | None = None
        response_source = "none"
        envelope_diagnostic: dict | None = None
        stderr = ""
        diagnostic_category: str | None = None
        diagnostic_detail: str | None = None
        process: asyncio.subprocess.Process | None = None
        job: WindowsJob | None = None
        failure_class: FailureClass | None = None
        status = AgentStatus.FAILED
        exit_code: int | None = None
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            flags = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            }
            structured_mode = task.operation in self.structured_schemas
            command = (self.command(workspace, task.operation) if structured_mode
                       else self.command(workspace))
            if _WINDOWS_JOB_ENABLED:
                job = WindowsJob()
                command = (sys.executable, "-m", "caf_agent_runtime._windows_supervisor", *command)
            async with asyncio.timeout_at(deadline):
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=workspace,
                    env=child_environment(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **flags,
                )
            if job is not None:
                try:
                    job.assign(process.pid)
                except ProcessTreeCleanupError:
                    # The gated supervisor has not seen GO and has no child.
                    # Kill only this held process handle if assignment fails.
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
                    raise
            async with self._lock:
                self._processes[task.run_id] = process
                if job is not None:
                    self._jobs[task.run_id] = job
                already_cancelled = task.run_id in self._cancelled
            if already_cancelled:
                if job is not None:
                    job.terminate_and_verify()
                else:
                    await _kill_tree(process)
            else:
                if job is not None:
                    assert process.stdin is not None
                    process.stdin.write(b"\x02")
                    await process.stdin.drain()
                # A prompt is sent via stdin, never placed in argv or a shell.
                async def read_stdout() -> None:
                    nonlocal final_text, response_source, envelope_diagnostic
                    nonlocal diagnostic_category, diagnostic_detail
                    assert process is not None and process.stdout is not None
                    if structured_mode:
                        captured = bytearray()
                        oversized = False
                        while chunk := await process.stdout.read(4096):
                            if len(captured) + len(chunk) > _MAX_CAPTURE:
                                oversized = True
                            if not oversized:
                                captured.extend(chunk)
                        final_text, envelope_diagnostic = _structured_envelope(
                            captured, self.structured_schemas[task.operation], oversized)
                        response_source = ("structured_output" if final_text is not None
                                           else "rejected_envelope")
                        if final_text is not None and len(events) < 1000:
                            events.append(AgentEvent(task.run_id, task.correlation_id,
                                                     len(events) + 1, "result", "result"))
                        return
                    pending = bytearray()

                    def accept_line(line: bytes) -> None:
                        nonlocal final_text, diagnostic_category, diagnostic_detail
                        try:
                            parsed = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            kind = "unstructured"
                        else:
                            kind = _safe_event_type(parsed)
                            candidate = _result_text(parsed)
                            if candidate is not None:
                                final_text = candidate
                                response_source = "result_text"
                            if kind in ("error", "turn.failed") and isinstance(parsed, dict):
                                detail = parsed.get("message", parsed.get("error"))
                                if isinstance(detail, str):
                                    diagnostic_category = _diagnostic_category(detail) or diagnostic_category
                                    diagnostic_detail = _safe_diagnostic_detail(detail)
                                elif isinstance(detail, dict) and isinstance(detail.get("message"), str):
                                    diagnostic_category = _diagnostic_category(detail["message"]) or diagnostic_category
                                    diagnostic_detail = _safe_diagnostic_detail(detail["message"])
                        if len(events) < 1000:
                            events.append(AgentEvent(
                                task.run_id, task.correlation_id, len(events) + 1, kind, kind,
                            ))

                    while chunk := await process.stdout.read(4096):
                        pending.extend(chunk)
                        if len(pending) > _MAX_CAPTURE and b"\n" not in pending:
                            pending.clear()
                            accept_line(b"")
                        while newline := pending.find(b"\n") + 1:
                            line = bytes(pending[:newline])
                            del pending[:newline]
                            if len(line) <= _MAX_CAPTURE:
                                accept_line(line)
                            else:
                                accept_line(b"")
                    if pending:
                        accept_line(bytes(pending))

                async def read_stderr() -> str:
                    assert process is not None and process.stderr is not None
                    data = bytearray()
                    while chunk := await process.stderr.read(4096):
                        data.extend(chunk)
                        if len(data) > _MAX_CAPTURE:
                            del data[:-_MAX_CAPTURE]
                    return _redact(data.decode("utf-8", errors="replace"))

                async def write_stdin() -> None:
                    assert process is not None and process.stdin is not None
                    process.stdin.write(task.prompt.encode("utf-8"))
                    await process.stdin.drain()
                    process.stdin.close()
                    await process.stdin.wait_closed()

                operations = [
                    asyncio.create_task(write_stdin()),
                    asyncio.create_task(read_stdout()),
                    asyncio.create_task(read_stderr()),
                    asyncio.create_task(process.wait()),
                ]
                try:
                    async with asyncio.timeout_at(deadline):
                        _, _, stderr, exit_code = await asyncio.gather(*operations)
                finally:
                    for operation in operations:
                        if not operation.done():
                            operation.cancel()
                    await asyncio.gather(*operations, return_exceptions=True)
            async with self._lock:
                was_cancelled = task.run_id in self._cancelled
            if was_cancelled:
                status = AgentStatus.CANCELLED
            elif exit_code == 0:
                if structured_mode and envelope_diagnostic is not None \
                        and envelope_diagnostic["reason"] != "ok":
                    status = AgentStatus.FAILED
                    failure_class = FailureClass.AGENT
                else:
                    status = AgentStatus.SUCCEEDED
            else:
                status = AgentStatus.FAILED
                diagnostic_category = diagnostic_category or _diagnostic_category(stderr)
                if diagnostic_detail is None and stderr:
                    diagnostic_detail = _safe_diagnostic_detail(stderr)
                failure_class = (
                    FailureClass.INFRASTRUCTURE
                    if diagnostic_category in ("authentication", "network")
                    else FailureClass.AGENT
                )
        except TimeoutError:
            status = AgentStatus.TIMED_OUT
            failure_class = FailureClass.TIMEOUT
            if process is not None and job is None:
                await _kill_tree(process)
        except OSError as exc:
            async with self._lock:
                was_cancelled = task.run_id in self._cancelled
            status = AgentStatus.CANCELLED if was_cancelled else AgentStatus.FAILED
            failure_class = None if was_cancelled else FailureClass.INFRASTRUCTURE
            stderr = "" if was_cancelled else _redact(str(exc))
            diagnostic_category = _diagnostic_category(stderr)
            diagnostic_detail = None if was_cancelled else _safe_diagnostic_detail(stderr)
            if process is not None and job is None:
                await _kill_tree(process)
        except asyncio.CancelledError:
            status = AgentStatus.CANCELLED
            if process is not None and job is None:
                await asyncio.shield(_kill_tree(process))
            raise
        finally:
            cleanup_error: ProcessTreeCleanupError | None = None
            if job is not None:
                try:
                    if status == AgentStatus.SUCCEEDED and job.active_processes():
                        # Supervisor exited, but a detached task process is
                        # still running. Killing it is not a clean success.
                        status = AgentStatus.FAILED
                        failure_class = FailureClass.INFRASTRUCTURE
                        diagnostic_category = "orphan_process"
                        diagnostic_detail = "task process survived CLI exit"
                    job.terminate_and_verify()
                except ProcessTreeCleanupError as exc:
                    cleanup_error = exc
                try:
                    job.close()
                except ProcessTreeCleanupError as exc:
                    cleanup_error = exc
                if cleanup_error is not None:
                    status = AgentStatus.FAILED
            async with self._lock:
                self._processes.pop(task.run_id, None)
                self._jobs.pop(task.run_id, None)
                self._statuses[task.run_id] = status
                self._cancelled.discard(task.run_id)
            if cleanup_error is not None:
                raise cleanup_error

        summary = f"{self.provider} CLI {status.value}"
        events.append(AgentEvent(task.run_id, task.correlation_id, len(events) + 1, status.value, summary))
        return AgentResult(
            run_id=task.run_id, correlation_id=task.correlation_id,
            status=status, summary=summary, events=tuple(events),
            failure_class=failure_class, exit_code=exit_code,
            output={"final_text": final_text, "stderr_tail": stderr,
                    "response_source": response_source,
                    "envelope_diagnostic": envelope_diagnostic,
                    "diagnostic_category": diagnostic_category,
                    "diagnostic_detail": diagnostic_detail},
        )

    async def cancel(self, run_id: str) -> None:
        async with self._lock:
            if run_id not in self._statuses:
                raise KeyError(run_id)
            if self._statuses[run_id] != AgentStatus.RUNNING:
                return
            self._cancelled.add(run_id)
            process = self._processes.get(run_id)
            job = self._jobs.get(run_id)
        if process is not None:
            if job is not None:
                job.terminate_and_verify()
                await asyncio.wait_for(process.wait(), timeout=5)
            else:
                await _kill_tree(process)

    async def get_status(self, run_id: str) -> AgentStatus:
        async with self._lock:
            return self._statuses[run_id]


class CodexRuntime(HostCliRuntime):
    def __init__(self, *, executable: Path, timeout_seconds: float = 120,
                 sandbox: Literal["read-only", "workspace-write"] = "read-only") -> None:
        super().__init__("codex", timeout_seconds=timeout_seconds,
                         codex_sandbox=sandbox, codex_executable=executable)


class ClaudeRuntime(HostCliRuntime):
    def __init__(self, *, timeout_seconds: float = 120,
                 tools: tuple[str, ...] = (),
                 structured_schemas: Mapping[str, dict] | None = None) -> None:
        super().__init__("claude", timeout_seconds=timeout_seconds,
                         claude_tools=tools, structured_schemas=structured_schemas)
