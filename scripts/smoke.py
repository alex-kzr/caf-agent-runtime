"""Run a no-tools smoke prompt in a disposable external Git worktree."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import tempfile
from pathlib import Path

from caf_agent_runtime import AgentStatus, ClaudeRuntime, CodexRuntime, CodingTask


async def smoke(provider: str) -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="caf-runtime-smoke-") as directory:
        workspace = Path(directory)
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "add", "--detach", str(workspace), "HEAD"],
            check=True, capture_output=True, text=True,
        )
        try:
            runtime = CodexRuntime(timeout_seconds=90) if provider == "codex" else ClaudeRuntime(timeout_seconds=90)
            result = await runtime.execute(
                CodingTask(
                    f"smoke-{provider}", f"smoke-{provider}",
                    "Reply exactly CAF_SMOKE_OK. Do not use tools, read files, or modify anything.",
                    provider,
                ),
                workspace,
            )
            print(
                f"provider={provider} status={result.status.value} "
                f"failure={result.failure_class} exit={result.exit_code} "
                f"diagnostic={result.output.get('diagnostic_category')} "
                f"marker_present={'CAF_SMOKE_OK' in str(result.output.get('final_text'))} "
                f"events={[event.kind for event in result.events]} "
                f"stderr_categories={[word for word in ('auth', 'sandbox', 'permission', 'network', 'error', 'warning', 'verbose', 'output-format', 'restricted', 'tools', 'invalid', 'unknown', 'session') if word in str(result.output.get('stderr_tail')).lower()]}"
            )
            if result.status != AgentStatus.SUCCEEDED or "CAF_SMOKE_OK" not in str(result.output.get("final_text")):
                raise SystemExit(1)
        finally:
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "remove", "--force", str(workspace)],
                check=True, capture_output=True, text=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=("codex", "claude"))
    args = parser.parse_args()
    asyncio.run(smoke(args.provider))
