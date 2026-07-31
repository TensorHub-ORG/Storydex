from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable


class ToolAccess(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    EXECUTE = "execute"


class ToolConcurrency(str, Enum):
    PARALLEL = "parallel"
    BLOCKING = "blocking"


@dataclass
class ToolResult:
    success: bool
    output: str
    error: str | None = None


class BaseTool:
    name = ""
    description = ""
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        raise NotImplementedError

    def bridge_spec(self) -> Dict[str, Any]:
        return {
            "name": str(self.name),
            "description": str(self.description),
            "parameters": self.get_parameters_schema(),
        }


class StorydexToolRegistry:
    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            raise ValueError("Storydex tool name must not be empty")
        self._tools[name] = tool

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def specs(self) -> list[Dict[str, Any]]:
        return [tool.bridge_spec() for tool in self._tools.values()]

    def dispatch(self, name: str, arguments: Dict[str, Any] | None = None) -> ToolResult:
        tool = self._tools.get(str(name or ""))
        if tool is None:
            return ToolResult(success=False, output="", error=f"Unknown Storydex tool: {name}")
        try:
            result = tool.run(dict(arguments or {}))
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"{type(exc).__name__}: {exc}")
        if isinstance(result, ToolResult):
            return result
        return ToolResult(success=True, output=str(result), error=None)
