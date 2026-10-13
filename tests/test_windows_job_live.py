"""Live Job Object tests with disposable Python children, no coding CLI."""

import asyncio
import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from caf_agent_runtime.windows_job import WindowsJob


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


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object test")
@pytest.mark.asyncio
async def test_job_kills_orphan_after_supervisor_exits_without_touching_foreign_process(tmp_path: Path):
    script = tmp_path / "parent.py"
    pid_file = tmp_path / "orphan.pid"
    script.write_text(
        "import subprocess,sys\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        " stdout=sys.stdout,stderr=sys.stderr)\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    foreign = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job = WindowsJob()
    supervisor = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "caf_agent_runtime._windows_supervisor",
        sys.executable, str(script), str(pid_file), cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    orphan_pid = None
    try:
        job.assign(supervisor.pid)
        supervisor.stdin.write(b"\x02")
        await supervisor.stdin.drain()
        supervisor.stdin.close()
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(.05)
        assert pid_file.exists()
        orphan_pid = int(pid_file.read_text())
        assert _alive(orphan_pid)
        for _ in range(100):
            if not _alive(supervisor.pid):
                break
            await asyncio.sleep(.05)
        assert not _alive(supervisor.pid)
        assert job.active_processes() >= 1
        await asyncio.to_thread(job.terminate_and_verify)
        assert not _alive(orphan_pid)
        assert _alive(foreign.pid)
        assert await asyncio.wait_for(supervisor.stdout.read(), 5) == b""
        assert await asyncio.wait_for(supervisor.wait(), 5) == 0
    finally:
        try:
            await asyncio.to_thread(job.terminate_and_verify)
        finally:
            job.close()
        if orphan_pid is not None and _alive(orphan_pid):
            subprocess.run(["taskkill", "/PID", str(orphan_pid), "/F"], capture_output=True, timeout=5)
        foreign.terminate()
        foreign.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object test")
@pytest.mark.asyncio
async def test_job_normal_exit_and_repeated_cleanup(tmp_path: Path):
    job = WindowsJob()
    supervisor = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "caf_agent_runtime._windows_supervisor",
        sys.executable, "-c", "print('done')", cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        job.assign(supervisor.pid)
        supervisor.stdin.write(b"\x02")
        await supervisor.stdin.drain()
        supervisor.stdin.close()
        output = await asyncio.wait_for(supervisor.stdout.read(), 5)
        assert output.strip() == b"done"
        assert await asyncio.wait_for(supervisor.wait(), 5) == 0
        await asyncio.to_thread(job.terminate_and_verify)
        await asyncio.to_thread(job.terminate_and_verify)
        assert job.active_processes() == 0
    finally:
        job.close()
