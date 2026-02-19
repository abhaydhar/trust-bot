from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger("trustbot.tools")


class ToolCallRecord(BaseModel):
    """Audit log entry for a single tool invocation."""

    tool_name: str
    method: str
    args: dict
    result_preview: str = ""
    duration_ms: float = 0.0
    success: bool = True
    error: str = ""


class BaseTool(ABC):
    """
    Base class for all TrustBot tools.

    Provides audit logging, access control hooks, and consistent error handling.
    Subclasses implement domain-specific methods and register them via `tool_methods`.
    """

    name: str = "base_tool"
    description: str = ""

    def __init__(self) -> None:
        self._audit_log: list[ToolCallRecord] = []

    @abstractmethod
    async def initialize(self) -> None:
        """Set up connections, validate configuration."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Clean up resources."""

    @property
    def audit_log(self) -> list[ToolCallRecord]:
        return list(self._audit_log)

    async def _record_call(
        self,
        method: str,
        args: dict,
        result: Any = None,
        error: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        preview = ""
        if result is not None:
            preview = str(result)[:200]
        record = ToolCallRecord(
            tool_name=self.name,
            method=method,
            args={k: str(v)[:100] for k, v in args.items()},
            result_preview=preview,
            duration_ms=duration_ms,
            success=not error,
            error=error,
        )
        self._audit_log.append(record)
        if error:
            logger.warning("Tool %s.%s failed: %s", self.name, method, error)
        else:
            logger.debug("Tool %s.%s completed in %.1fms", self.name, method, duration_ms)

    async def call(self, method: str, **kwargs: Any) -> Any:
        """
        Invoke a tool method by name with audit logging.
        This is the primary entry point used by the agent orchestrator.
        """
        func = getattr(self, method, None)
        if func is None:
            raise ValueError(f"Tool {self.name} has no method '{method}'")

        start = time.perf_counter()
        try:
            result = await func(**kwargs)
            elapsed = (time.perf_counter() - start) * 1000
            await self._record_call(method, kwargs, result=result, duration_ms=elapsed)
            return result
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            await self._record_call(method, kwargs, error=str(exc), duration_ms=elapsed)
            raise


class ToolRegistry:
    """
    Central registry of all available tools.
    The agent orchestrator uses this to discover and invoke tools.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"No tool registered with name '{name}'")
        return tool

    @property
    def tools(self) -> dict[str, BaseTool]:
        return dict(self._tools)

    async def initialize_all(self) -> None:
        for tool in self._tools.values():
            await tool.initialize()

    async def shutdown_all(self) -> None:
        for tool in self._tools.values():
            await tool.shutdown()

    def get_tool_descriptions(self) -> list[dict]:
        """Return tool descriptions formatted for LLM function-calling."""
        descriptions = []
        for tool in self._tools.values():
            descriptions.append({"name": tool.name, "description": tool.description})
        return descriptions

    def get_full_audit_log(self) -> list[ToolCallRecord]:
        records: list[ToolCallRecord] = []
        for tool in self._tools.values():
            records.extend(tool.audit_log)
        return sorted(records, key=lambda r: r.duration_ms)
