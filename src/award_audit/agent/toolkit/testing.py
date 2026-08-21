"""Deterministic Fake Tool facilities for network-free tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from copy import deepcopy

from pydantic import BaseModel, ConfigDict

from award_audit.agent.toolkit.contracts import ToolResult, ToolSpec
from award_audit.agent.toolkit.registry import ToolExecutionContext, ToolRegistry


class AnyToolInput(BaseModel):
    model_config = ConfigDict(extra="allow")


class FakeTool:
    """Queue canned results and retain validated calls for assertions."""

    def __init__(self, results: Iterable[ToolResult | dict[str, object]]) -> None:
        self._results = deque(results)
        self.calls: list[dict[str, object]] = []

    def __call__(self, arguments: BaseModel, _context: ToolExecutionContext) -> ToolResult:
        self.calls.append(deepcopy(arguments.model_dump()))
        if not self._results:
            return ToolResult.failure("FAKE_TOOL_EXHAUSTED", "no canned result remains")
        result = self._results.popleft()
        return result if isinstance(result, ToolResult) else ToolResult.model_validate(result)


def register_fake_tool(
    registry: ToolRegistry,
    name: str,
    results: Iterable[ToolResult | dict[str, object]],
    *,
    input_model: type[BaseModel] = AnyToolInput,
    timeout_seconds: float = 1.0,
) -> FakeTool:
    fake = FakeTool(results)
    registry.register(ToolSpec(name=name, description=f"Offline fake for {name}.",
                               input_model=input_model, timeout_seconds=timeout_seconds), fake)
    return fake
