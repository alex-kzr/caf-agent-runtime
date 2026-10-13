# CAF agent runtime

This independently versioned package defines CAF's in-process coding-agent runtime
boundary (`CodingTask`, `CodingAgentRuntime`, `AgentResult`), a deterministic
`SyntheticRuntime` for workflow tests, and host `CodexRuntime`/`ClaudeRuntime`
adapters. The synthetic adapter never edits the workspace, starts a process,
or produces a real commit. See the umbrella `SPEC.md` §9 and
`docs/architecture/contracts.md` for the intended full boundary.

## Local verification

Requires Python 3.12+ and uv:

```sh
uv sync --group dev
uv run pytest
```

The opt-in CLI smoke runner creates and removes a disposable Git worktree under
the system temporary directory. It sends a fixed no-tools prompt and prints only
status, event kinds, and diagnostic categories:

```sh
uv run python scripts/smoke.py codex
uv run python scripts/smoke.py claude
```

The package can be installed in another local CAF service with a versioned Git
dependency later. During local development use an editable path dependency or
`uv pip install -e ../caf-agent-runtime`; do not copy the models into workers.

## Contract

`execute(task: CodingTask, workspace: Path, session_id: str | None = None)` returns
an `AgentResult`. `cancel(run_id)` requests cancellation; `get_status(run_id)`
returns `AgentStatus` or raises `KeyError` for an unknown ID. A run ID is unique
per runtime instance. `CodingTask.operation` is an opaque operation label (for
example `review` or `test`), while `provider` records the intended external
provider. Synthetic scenarios choose a sequence of `SyntheticOutcome` objects by
operation and repeat the last outcome when the sequence is exhausted.

`AgentResult.output` carries operation-specific structured data. A worker may
validate `{"approved": false, "findings": [...]}` for review, or
`{"passed": true, "checks": [...]}` for tests; this package does not prescribe
those schemas. Events carry run/correlation IDs and per-execution sequence
numbers. `usage=None` means unavailable, not zero. Failure classes distinguish
agent, infrastructure, timeout, and quality-gate outcomes. Caller cancellation
is propagated as `asyncio.CancelledError`; `cancel(run_id)` instead causes a
terminal `cancelled` result. Synthetic state is in memory and cannot recover
after a restart. Synthetic outcomes have no CLI exit code unless a test
explicitly scripts one. A `timed_out` result requires the `timeout` failure
class; `failed` requires another failure class; `succeeded` and `cancelled`
cannot carry a failure class.

## Example

```python
from pathlib import Path
from caf_agent_runtime import CodingTask, SyntheticOutcome, SyntheticRuntime

runtime = SyntheticRuntime({
    "review": [SyntheticOutcome(output={"approved": True, "findings": []})]
})
result = await runtime.execute(
    CodingTask("run-1", "corr-1", "Review the integrated diff", "synthetic", "review"),
    Path("."),
)
assert result.output["approved"] is True
```

## Pending work

The host adapters pass argument arrays to `asyncio.create_subprocess_exec`, send
the prompt over stdin, set an explicit working directory, and enforce a wall-time
limit across process launch and concurrent stdin/stdout/stderr handling. On timeout
or cancellation they attempt process-tree cleanup even if the direct CLI process
has already exited. POSIX uses a dedicated process group; Windows uses
`taskkill /T /F`, which may not find descendants after the parent exits. Windows
orphan cleanup is therefore not yet guaranteed or empirically verified. Codex defaults to
`read-only` sandbox; `workspace-write` requires explicit construction. Claude
defaults to restricted mode with no tools and denied permission prompts; tool
allowlists require explicit construction. Structured stdout events contribute
only event types and redacted final text; stderr is bounded and redacted by a
best-effort token pattern. Authentication and network errors detected in output
are infrastructure failures, other nonzero exits are agent failures. No CLI
authentication material is copied into the runtime.

**Verified locally on 2026-10-13:** `codex-cli 0.162.1` help lists `exec`,
`--json`, `--ephemeral`, `--sandbox`, and `--cd`. `Claude Code 2.1.296` help
lists `--print`, `--verbose`, `--output-format stream-json`, `--restricted`,
`--permission-mode dontAsk`, `--permission-prompts none`, and
`--no-session-persistence`. A no-tools Claude call using the existing Pro login
exited 0, emitted `system`, `assistant`, `rate_limit_event`, and `result` events,
and returned the expected marker. A read-only Codex smoke call exited 1 with
`error` and `turn.failed` events and an authentication-related diagnostic,
despite `codex login status` reporting a ChatGPT login. No credential changes
were made. This does not verify successful Codex task execution.

**Executable resolution finding:** PowerShell's `codex` command above selected
the npm 0.162.1 wrapper, but Python `subprocess.run(['codex', '--version'])`,
which matches this adapter's launch method, returned **0.147.0**. That version
matches the separate desktop Codex executable in the same Windows user's local
programs directory. Both installations reported a ChatGPT login, and the
0.147.0 `exec --help` lists the adapter's flags. The original smoke retained
only an authentication *category*, not the exact provider error. The executable
mismatch must be resolved and a new isolated smoke run approved before claiming
which Codex installation can execute tasks. The observed mismatch does not by
itself prove the authentication failure's cause. See the umbrella
`docs/development/environment.md` for path and user metadata.

**Still pending:** Confirm Codex execution with the existing profile; verify
write-capable CLI paths and actual file changes in an isolated worktree;
structured session IDs and resume; provider-specific event/usage extraction;
full secret redaction; process-tree cleanup on each supported host; and
integration with workspace allocation, durable events and recovery. Worktrees
provide Git isolation, not a security sandbox.
