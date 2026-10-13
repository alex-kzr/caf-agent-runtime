# CAF agent runtime

This independently versioned package defines CAF's in-process coding-agent runtime
boundary (`CodingTask`, `CodingAgentRuntime`, `AgentResult`) and a deterministic
`SyntheticRuntime` for workflow tests. It implements no Codex or Claude CLI
invocation yet. The synthetic adapter never edits the workspace, starts a process,
or produces a real commit. See the umbrella `SPEC.md` §9 and
`docs/architecture/contracts.md` for the intended full boundary.

## Local verification

Requires Python 3.12+ and uv:

```sh
uv sync --group dev
uv run pytest
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
explicitly scripts one.

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

Real CLI adapters need verified argument arrays, process-tree cancellation,
timeouts, event parsing, redaction, workspace and credential handling, and
integration tests on the installed CLI versions. No production security or
session-resume claim follows from this synthetic implementation.
