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
status, fixed event kinds, and allowlisted diagnostic phrases:

```sh
uv run python scripts/smoke.py claude
```

Codex now requires an explicit absolute path and an expected version; there is
no `PATH` fallback. For the verified Windows npm installation on this host,
the native executable is
`C:\nvm4w\nodejs\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`
and reports `codex-cli 0.162.1`. An example invocation is:

```powershell
uv run python scripts/smoke.py codex --codex-executable 'C:\nvm4w\nodejs\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe' --expected-version 0.162.1
```

The smoke runner checks `--version` before its one CLI invocation. Its
diagnostic detail prints only exact phrases from a fixed safe vocabulary; any
other provider text, headers and credentials are withheld. The underlying
`AgentResult.stderr_tail` remains best-effort redacted and must not be printed
or published as an unreviewed diagnostic log.

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
has already exited. POSIX uses a dedicated process group. On Windows, a gated
supervisor waits until it is assigned to a fresh unnamed Job Object before
starting the CLI. Its descendants inherit that Job; timeout and cancellation
terminate the Job and verify its active-process count is zero. A CLI exit code
of zero with a live descendant is reported as an infrastructure failure before
the descendant is stopped. A failure to verify cleanup raises
`ProcessTreeCleanupError`; a queue executor must leave that claim unacknowledged
for orphan quarantine. Live Windows tests covered a parent that exits while
its detached child retains output pipes, a detached child with closed pipes,
cancellation after the parent exits, normal completion, and preservation of an
unrelated process. These tests use local Python processes, not Codex or Claude.
The previous PID-based `taskkill` cleanup failed for an orphan in a disposable
probe and is retained only as a fallback outside the Windows Job path. This
does not establish universal containment for privileged processes that bypass
the ordinary child-process path. A harmless fixture that requested Windows
`CREATE_BREAKAWAY_FROM_JOB` did spawn on this host, but `IsProcessInJob`
confirmed that child remained in the Job and cancellation stopped it. A
separate test killed only its disposable runtime host process and confirmed
Job closure stopped the child. Neither test restarts Windows or represents a
full worker crash with a durable queue lease; keep shared queue CLI dispatch
disabled until that recovery path is verified. Codex defaults to
`read-only` sandbox; `workspace-write` requires explicit construction. Claude
defaults to restricted mode with no tools and denied permission prompts; tool
allowlists require explicit construction. Structured stdout events contribute
only event types and redacted final text; stderr is bounded and redacted by a
best-effort token pattern. Authentication and network errors detected in output
are infrastructure failures, other nonzero exits are agent failures. No CLI
authentication material is copied into the runtime.

For a Claude operation configured with `structured_schemas`, the adapter sends
the JSON Schema via `--json-schema` and selects the documented `json` output
envelope. It reads only `structured_output` as the operation result; a text
`result` is never substituted when that field is absent. The envelope is read
with a size limit, and the structured value is bounded before it reaches the
caller. Duplicate JSON fields and secretlike structured content fail closed
before any local parser can accept a transformed value. Operation code must
validate the value and avoid persisting arbitrary
provider content. Other Claude operations retain the `stream-json` path.
Offline fake-process tests cover a structured envelope, missing structured
output, an error envelope, duplicate fields, sensitive content, and an invalid
root type. The combination of CLI
flags has not been tested against a live Claude process. The envelope and
`--json-schema` usage follow Claude Code's
[programmatic CLI documentation](https://code.claude.com/docs/en/headless#get-structured-output).

The later offline envelope diagnostic gives each rejection a fixed reason and
framing code, fixed JSON type categories and a capped byte count. Absent
`type`/`subtype` tags are accepted because the documented JSON contract does
not require them; present tags, `status` and `is_error` must indicate success.
The adapter never falls back to text `result` or a nested structured value.
Exit-zero envelope rejection is reported as an agent failure. Raw provider
fields, response content and unknown field names are not retained in this
diagnostic. See the umbrella
[envelope diagnostic gate](../../docs/experiments/2026-10-14-envelope-diagnostic-gate.md).
An unrecognized root array remains rejected. The runtime records only bounded
shape counts and the position class of a sole tagged result, never an array
element. The official JSON contract inspected so far does not establish an
array wrapper for this CLI invocation.

Every CLI child now receives an explicit minimal environment: OS executable
lookup and user profile/config directory paths, plus `CODEX_HOME` or
`CLAUDE_CONFIG_DIR` if set. It does not inherit `CAF_WORKER_TOKEN`, database
credentials or model API keys from its parent. Windows Job Object supervisor
children inherit this already filtered environment. A native child test with
dummy secret variables verified their absence. This path-based setup has not
been revalidated with a real provider CLI after the environment change; a
provider requiring additional environment variables must be reviewed and
added explicitly rather than restoring full inheritance.

**Verified locally on 2026-10-13:** `codex-cli 0.162.1` help lists `exec`,
`--json`, `--ephemeral`, `--sandbox`, and `--cd`. `Claude Code 2.1.296` help
lists `--print`, `--verbose`, `--output-format`, `--json-schema`, `--restricted`,
`--permission-mode dontAsk`, `--permission-prompts none`, and
`--no-session-persistence`. A no-tools Claude call using the existing Pro login
exited 0, emitted `system`, `assistant`, `rate_limit_event`, and `result` events,
and returned the expected marker. An initial read-only Codex smoke exited 1 with
`error` and `turn.failed` events and an authentication-category diagnostic;
read-only investigation later found that unqualified Python `subprocess` chose
the separate desktop CLI 0.147.0. With the npm installation's native 0.162.1
executable supplied explicitly, **one** no-tools Codex smoke exited 0, emitted
`item.completed`, and returned the expected marker. No credential changes were
made. These calls do not verify a code-changing task or full workflow.

**Executable resolution finding:** PowerShell's `codex` command above selected
the npm 0.162.1 wrapper, but Python `subprocess.run(['codex', '--version'])`,
which matches this adapter's launch method, returned **0.147.0**. That version
matches the separate desktop Codex executable in the same Windows user's local
programs directory. Both installations reported a ChatGPT login, and the
0.147.0 `exec --help` lists the adapter's flags. The original smoke retained
only an authentication *category*, not the exact provider error. The successful
explicit-path smoke establishes that the npm 0.162.1 executable can complete a
no-tools task with the existing login. It does not prove the version mismatch
caused the earlier failure. See the umbrella
`docs/development/environment.md` for path and user metadata.

**Still pending:** Verify write-capable Codex and Claude tasks and actual file
changes in an isolated worktree;
structured session IDs and resume; provider-specific event/usage extraction;
full secret redaction; process-tree cleanup on each supported host; and
integration with workspace allocation, durable events and recovery. Worktrees
provide Git isolation, not a security sandbox.
