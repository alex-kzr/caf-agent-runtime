"""Run a no-tools smoke prompt in a disposable external Git worktree."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from caf_agent_runtime import AgentStatus, ClaudeRuntime, CodexRuntime, CodingTask


_PRINTABLE_EVENT_KINDS = frozenset({
    "started", "system", "assistant", "result", "rate_limit_event",
    "error", "turn.failed", "item.completed", "succeeded", "failed", "timed_out",
})


async def smoke(provider: str, codex_executable: Path | None, expected_version: str | None) -> None:
    repository = Path(__file__).resolve().parents[1]
    if provider == "codex":
        if codex_executable is None or expected_version is None:
            raise ValueError("Codex requires --codex-executable and --expected-version")
        if not codex_executable.is_absolute():
            raise ValueError("Codex executable path must be absolute")
        if os.name == "nt" and codex_executable.suffix.lower() != ".exe":
            raise ValueError("Codex on Windows requires a native .exe")
        codex_executable = codex_executable.resolve(strict=True)
        version = subprocess.run(
            [str(codex_executable), "--version"], check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if version != f"codex-cli {expected_version}":
            raise ValueError(f"Codex executable version mismatch: expected {expected_version}")
    with tempfile.TemporaryDirectory(prefix="caf-runtime-smoke-") as directory:
        workspace = Path(directory)
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "add", "--detach", str(workspace), "HEAD"],
            check=True, capture_output=True, text=True,
        )
        try:
            runtime = (
                CodexRuntime(executable=codex_executable, timeout_seconds=90)
                if provider == "codex" else ClaudeRuntime(timeout_seconds=90)
            )
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
                f"detail={result.output.get('diagnostic_detail')} "
                f"marker_present={'CAF_SMOKE_OK' in str(result.output.get('final_text'))} "
                f"events={[event.kind for event in result.events if event.kind in _PRINTABLE_EVENT_KINDS]} "
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
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    asyncio.run(smoke(args.provider, args.codex_executable, args.expected_version))
