"""Real Windows process-tree cancellation, with no coding CLI or network."""

import asyncio
import ctypes
import os
import subprocess
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


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree test")
@pytest.mark.asyncio
async def test_timeout_kills_detached_orphan_after_parent_exits(tmp_path: Path) -> None:
    script = tmp_path / "parent_exits.py"
    child_pid_file = tmp_path / "orphan.pid"
    script.write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        " stdout=sys.stdout,stderr=sys.stderr,creationflags=8)\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    foreign = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    runtime = CodexRuntime(executable=Path(sys.executable), timeout_seconds=1)
    runtime.command = lambda _: (sys.executable, str(script), str(child_pid_file))  # type: ignore[method-assign]
    task = CodingTask("caf-orphan-test", "caf-orphan-corr", "No coding", "codex")
    child_pid = None
    try:
        result = await asyncio.wait_for(runtime.execute(task, tmp_path), 10)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        assert result.status == AgentStatus.TIMED_OUT
        assert not _alive(child_pid)
        assert _alive(foreign.pid)
        await runtime.cancel(task.run_id)
        await runtime.cancel(task.run_id)
    finally:
        if child_pid is not None and _alive(child_pid):
            subprocess.run(["taskkill", "/PID", str(child_pid), "/F"],
                           capture_output=True, timeout=5)
        foreign.terminate()
        foreign.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree test")
@pytest.mark.asyncio
async def test_cancel_after_parent_exit_kills_its_orphan(tmp_path: Path) -> None:
    script = tmp_path / "parent_exits.py"
    child_pid_file = tmp_path / "orphan.pid"
    script.write_text(
        "import subprocess,sys\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        " stdout=sys.stdout,stderr=sys.stderr,creationflags=8)\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    runtime = CodexRuntime(executable=Path(sys.executable), timeout_seconds=15)
    runtime.command = lambda _: (sys.executable, str(script), str(child_pid_file))  # type: ignore[method-assign]
    task = CodingTask("caf-orphan-cancel", "caf-orphan-cancel-corr", "No coding", "codex")
    pending = asyncio.create_task(runtime.execute(task, tmp_path))
    child_pid = None
    try:
        for _ in range(100):
            if child_pid_file.exists():
                break
            await asyncio.sleep(.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        assert _alive(child_pid)
        supervisor_pid = runtime._processes[task.run_id].pid
        for _ in range(100):
            if not _alive(supervisor_pid):
                break
            await asyncio.sleep(.05)
        assert not _alive(supervisor_pid)  # child still retains stdout/stderr
        await runtime.cancel(task.run_id)
        await runtime.cancel(task.run_id)
        result = await asyncio.wait_for(pending, 10)
        assert result.status == AgentStatus.CANCELLED
        assert not _alive(child_pid)
    finally:
        if not pending.done():
            await runtime.cancel(task.run_id)
            await asyncio.wait_for(pending, 10)
        if child_pid is not None and _alive(child_pid):
            subprocess.run(["taskkill", "/PID", str(child_pid), "/F"],
                           capture_output=True, timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree test")
@pytest.mark.asyncio
async def test_clean_parent_exit_with_detached_child_is_failure(tmp_path: Path) -> None:
    script = tmp_path / "parent_exits.py"
    child_pid_file = tmp_path / "orphan.pid"
    script.write_text(
        "import subprocess,sys\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        " stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=8)\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    runtime = CodexRuntime(executable=Path(sys.executable), timeout_seconds=10)
    runtime.command = lambda _: (sys.executable, str(script), str(child_pid_file))  # type: ignore[method-assign]
    task = CodingTask("caf-orphan-success", "caf-orphan-success-corr", "No coding", "codex")
    child_pid = None
    try:
        result = await asyncio.wait_for(runtime.execute(task, tmp_path), 15)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        assert result.exit_code == 0
        assert result.status == AgentStatus.FAILED
        assert result.output["diagnostic_category"] == "orphan_process"
        assert not _alive(child_pid)
    finally:
        if child_pid is not None and _alive(child_pid):
            subprocess.run(["taskkill", "/PID", str(child_pid), "/F"],
                           capture_output=True, timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object breakaway test")
@pytest.mark.asyncio
async def test_breakaway_attempt_remains_in_job(tmp_path: Path) -> None:
    script = tmp_path / "try_breakaway.py"
    result_file = tmp_path / "result.txt"
    script.write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "try:\n"
        " child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        " stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x01000000)\n"
        " Path(sys.argv[1]).write_text('spawned:'+str(child.pid))\n"
        "except OSError as exc:\n"
        " Path(sys.argv[1]).write_text('rejected:'+str(exc.winerror))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    runtime = CodexRuntime(executable=Path(sys.executable), timeout_seconds=10)
    runtime.command = lambda _: (sys.executable, str(script), str(result_file))  # type: ignore[method-assign]
    task = CodingTask("caf-breakaway", "caf-breakaway-corr", "No coding", "codex")
    pending = asyncio.create_task(runtime.execute(task, tmp_path))
    try:
        for _ in range(100):
            if result_file.exists():
                break
            await asyncio.sleep(.05)
        assert result_file.exists()
        observation = result_file.read_text()
        if observation.startswith("spawned:"):
            pid = int(observation.split(":")[1])
            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(0x1000, False, pid)
            assert handle
            try:
                in_job = ctypes.c_int()
                assert kernel.IsProcessInJob(handle, runtime._jobs[task.run_id]._handle,
                                             ctypes.byref(in_job))
                assert in_job.value == 1
            finally:
                kernel.CloseHandle(handle)
        else:
            assert observation == "rejected:5"
        await runtime.cancel(task.run_id)
        result = await asyncio.wait_for(pending, 10)
        assert result.status == AgentStatus.CANCELLED
    finally:
        if not pending.done():
            await runtime.cancel(task.run_id)
            await asyncio.wait_for(pending, 10)
        if result_file.exists() and result_file.read_text().startswith("spawned:"):
            pid = int(result_file.read_text().split(":")[1])
            if _alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object host crash test")
@pytest.mark.asyncio
async def test_killed_runtime_host_closes_job_and_child(tmp_path: Path) -> None:
    child_script = tmp_path / "spawn_child.py"
    child_pid_file = tmp_path / "child.pid"
    child_script.write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        " stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    launcher_script = tmp_path / "runtime_host.py"
    launcher_script.write_text(
        "import asyncio,sys\nfrom pathlib import Path\n"
        "from caf_agent_runtime import CodexRuntime,CodingTask\n"
        "runtime=CodexRuntime(executable=Path(sys.executable),timeout_seconds=60)\n"
        "runtime.command=lambda _: (sys.executable,sys.argv[1],sys.argv[2])\n"
        "asyncio.run(runtime.execute(CodingTask('crash-run','crash-corr','No coding','codex'),"
        "Path(sys.argv[3])))\n",
        encoding="utf-8",
    )
    host = subprocess.Popen([sys.executable, str(launcher_script), str(child_script),
                             str(child_pid_file), str(tmp_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        for _ in range(100):
            if child_pid_file.exists():
                break
            await asyncio.sleep(.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        assert _alive(child_pid)
        host.kill()  # Only this test-owned runtime host; no system restart.
        host.wait(timeout=5)
        for _ in range(100):
            if not _alive(child_pid):
                break
            await asyncio.sleep(.05)
        assert not _alive(child_pid)
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=5)
        if child_pid is not None and _alive(child_pid):
            subprocess.run(["taskkill", "/PID", str(child_pid), "/F"],
                           capture_output=True, timeout=5)
