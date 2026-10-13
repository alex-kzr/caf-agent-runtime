"""Real Windows process-tree cancellation, with no coding CLI or network."""

import asyncio
import ctypes
import os
import sys
from pathlib import Path

import pytest

from caf_agent_runtime import AgentStatus, CodexRuntime, CodingTask


def _alive(pid: int) -> bool:
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill process-tree test")
@pytest.mark.asyncio
async def test_cancel_terminates_running_child_process(tmp_path: Path) -> None:
    script = tmp_path / "parent.py"
    child_pid_file = tmp_path / "child.pid"
    script.write_text(
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    runtime = CodexRuntime(executable=Path(sys.executable), timeout_seconds=20)
    runtime.command = lambda _: (sys.executable, str(script), str(child_pid_file))  # type: ignore[method-assign]
    task = CodingTask("caf-tree-test", "caf-tree-corr", "No coding", "codex")
    pending = asyncio.create_task(runtime.execute(task, tmp_path))
    try:
        for _ in range(100):
            if child_pid_file.exists():
                break
            await asyncio.sleep(.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        assert _alive(child_pid)
        await runtime.cancel(task.run_id)
        result = await asyncio.wait_for(pending, 10)
        assert result.status == AgentStatus.CANCELLED
        for _ in range(40):
            if not _alive(child_pid):
                break
            await asyncio.sleep(.05)
        assert not _alive(child_pid)
    finally:
        if not pending.done():
            await runtime.cancel(task.run_id)
            await asyncio.wait_for(pending, 10)
