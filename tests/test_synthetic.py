import asyncio
from pathlib import Path

import pytest

from caf_agent_runtime import (
    AgentResult, AgentStatus, CodingTask, FailureClass, SyntheticOutcome, SyntheticRuntime,
)


def task(run_id: str, operation: str = "implement") -> CodingTask:
    return CodingTask(run_id, "corr-1", "Do the operation", "synthetic", operation)


def test_contract_rejects_invalid_tasks_and_results() -> None:
    with pytest.raises(ValueError, match="run_id"):
        task("")
    with pytest.raises(ValueError, match="failure class"):
        AgentResult("r", "c", AgentStatus.FAILED, "failed", ())
    with pytest.raises(ValueError, match="timeout failure class"):
        SyntheticOutcome(status=AgentStatus.TIMED_OUT)


@pytest.mark.asyncio
async def test_scripted_review_sequence_and_correlation(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"review": [
        SyntheticOutcome(output={"approved": False, "findings": ["Fix test"]}),
        SyntheticOutcome(output={"approved": True, "findings": []}),
    ]})
    results = [await runtime.execute(task(f"review-{i}", "review"), tmp_path) for i in range(3)]
    assert [r.output["approved"] for r in results] == [False, True, True]
    assert all(r.status == AgentStatus.SUCCEEDED for r in results)
    assert [e.sequence for e in results[0].events] == [1, 2]
    assert all(e.correlation_id == "corr-1" for e in results[0].events)
    assert results[0].usage is None
    assert results[0].exit_code is None
    assert await runtime.get_status("review-0") == AgentStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_failure_class_and_duplicate_id(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"test": [SyntheticOutcome(
        status=AgentStatus.FAILED, failure_class=FailureClass.QUALITY_GATE,
        exit_code=1, output={"passed": False, "checks": ["pytest"]},
    )]})
    result = await runtime.execute(task("test-1", "test"), tmp_path)
    assert result.failure_class == FailureClass.QUALITY_GATE
    assert result.exit_code == 1
    assert result.output["passed"] is False
    with pytest.raises(ValueError, match="already used"):
        await runtime.execute(task("test-1", "test"), tmp_path)
    with pytest.raises(KeyError):
        await runtime.get_status("missing")


@pytest.mark.asyncio
async def test_cancel_running_operation(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"implement": [SyntheticOutcome(delay_seconds=60)]})
    pending = asyncio.create_task(runtime.execute(task("slow"), tmp_path))
    await asyncio.sleep(0)
    assert await runtime.get_status("slow") == AgentStatus.RUNNING
    await runtime.cancel("slow")
    result = await asyncio.wait_for(pending, 1)
    assert result.status == AgentStatus.CANCELLED
    assert result.output == {}
    assert await runtime.get_status("slow") == AgentStatus.CANCELLED


@pytest.mark.asyncio
async def test_workspace_and_caller_cancellation(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"implement": [SyntheticOutcome(delay_seconds=60)]})
    with pytest.raises(ValueError, match="workspace"):
        await runtime.execute(task("bad"), tmp_path / "missing")
    pending = asyncio.create_task(runtime.execute(task("caller"), tmp_path))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await runtime.get_status("caller") == AgentStatus.CANCELLED
