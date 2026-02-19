"""Tests for the tools base layer."""

import pytest

from trustbot.tools.base import BaseTool, ToolRegistry


class MockTool(BaseTool):
    name = "mock"
    description = "A mock tool for testing"

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def echo(self, message: str) -> str:
        return f"echo: {message}"

    async def fail(self) -> None:
        raise RuntimeError("Intentional failure")


@pytest.mark.asyncio
async def test_tool_call_records_success():
    tool = MockTool()
    result = await tool.call("echo", message="hello")
    assert result == "echo: hello"
    assert len(tool.audit_log) == 1
    assert tool.audit_log[0].success is True
    assert tool.audit_log[0].method == "echo"


@pytest.mark.asyncio
async def test_tool_call_records_failure():
    tool = MockTool()
    with pytest.raises(RuntimeError):
        await tool.call("fail")
    assert len(tool.audit_log) == 1
    assert tool.audit_log[0].success is False


@pytest.mark.asyncio
async def test_tool_call_invalid_method():
    tool = MockTool()
    with pytest.raises(ValueError, match="no method"):
        await tool.call("nonexistent")


@pytest.mark.asyncio
async def test_registry():
    registry = ToolRegistry()
    tool = MockTool()
    registry.register(tool)

    assert registry.get("mock") is tool
    assert "mock" in registry.tools

    with pytest.raises(KeyError):
        registry.get("nonexistent")


@pytest.mark.asyncio
async def test_registry_initialize_and_shutdown():
    registry = ToolRegistry()
    registry.register(MockTool())
    await registry.initialize_all()
    await registry.shutdown_all()


@pytest.mark.asyncio
async def test_full_audit_log():
    registry = ToolRegistry()
    tool = MockTool()
    registry.register(tool)

    await tool.call("echo", message="test1")
    await tool.call("echo", message="test2")

    audit = registry.get_full_audit_log()
    assert len(audit) == 2
