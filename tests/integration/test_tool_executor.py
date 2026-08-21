"""Offline M5.1 executor integration: a failed tool call does not kill the case."""

from award_audit.agent.toolkit import (
    SafeToolExecutor,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from award_audit.agent.toolkit.testing import register_fake_tool


def test_fake_tool_failure_isolated_and_next_call_runs(tmp_path) -> None:  # noqa: ANN001
    registry = ToolRegistry()
    fake = register_fake_tool(registry, "evidence_fake", [
        ToolResult.failure("UPSTREAM_UNAVAILABLE", "temporary failure"),
        ToolResult(ok=True, data={"evidence": "official roster"}),
    ])
    context = ToolExecutionContext.create([tmp_path])
    executor = SafeToolExecutor(registry)

    first = executor.execute("evidence_fake", {"step": 1}, context)
    second = executor.execute("evidence_fake", {"step": 2}, context)

    assert first.error_code == "UPSTREAM_UNAVAILABLE"
    assert second.ok and second.data["evidence"] == "official roster"
    assert len(fake.calls) == 2 and len(context.trace) == 2
    assert [observation.ok for observation in context.trace] == [False, True]
