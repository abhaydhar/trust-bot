"""Tests for the filesystem tool."""

import pytest

from trustbot.config import settings
from trustbot.tools.filesystem_tool import FilesystemTool


@pytest.fixture
async def fs_tool(tmp_path):
    """Create a filesystem tool rooted at a temp directory with sample files."""
    # Create sample files
    (tmp_path / "hello.py").write_text(
        'def greet(name):\n    return f"Hello, {name}!"\n\n'
        'def farewell(name):\n    result = greet(name)\n    return f"Goodbye, {name}!"\n'
    )
    (tmp_path / "service.py").write_text(
        "class MyService:\n"
        "    def process(self, data):\n"
        "        validated = self.validate(data)\n"
        "        return self.transform(validated)\n\n"
        "    def validate(self, data):\n"
        "        return data\n\n"
        "    def transform(self, data):\n"
        '        return {"result": data}\n'
    )
    subdir = tmp_path / "utils"
    subdir.mkdir()
    (subdir / "helper.py").write_text("def double(x):\n    return x * 2\n")

    # Temporarily override the codebase root
    original_root = settings.codebase_root
    settings.codebase_root = tmp_path

    tool = FilesystemTool()
    await tool.initialize()

    yield tool

    await tool.shutdown()
    settings.codebase_root = original_root


@pytest.mark.asyncio
async def test_read_file(fs_tool):
    content = await fs_tool.read_file("hello.py")
    assert "def greet" in content


@pytest.mark.asyncio
async def test_list_directory(fs_tool):
    entries = await fs_tool.list_directory(".")
    assert "hello.py" in entries
    assert "service.py" in entries
    assert "utils/" in entries


@pytest.mark.asyncio
async def test_search_text(fs_tool):
    results = await fs_tool.search_text("greet")
    assert len(results) > 0
    assert any(r["file"] == "hello.py" for r in results)


@pytest.mark.asyncio
async def test_check_file_exists(fs_tool):
    assert await fs_tool.check_file_exists("hello.py") is True
    assert await fs_tool.check_file_exists("nonexistent.py") is False


@pytest.mark.asyncio
async def test_check_function_exists(fs_tool):
    assert await fs_tool.check_function_exists("hello.py", "greet") is True
    assert await fs_tool.check_function_exists("hello.py", "nonexistent") is False


@pytest.mark.asyncio
async def test_extract_function_body(fs_tool):
    body = await fs_tool.extract_function_body("hello.py", "greet")
    assert body is not None
    assert "greet" in body
    assert "Hello" in body


@pytest.mark.asyncio
async def test_find_function(fs_tool):
    results = await fs_tool.find_function("greet")
    assert len(results) > 0
    assert results[0]["file"] == "hello.py"


@pytest.mark.asyncio
async def test_path_sandboxing(fs_tool):
    with pytest.raises(PermissionError):
        await fs_tool.read_file("../../etc/passwd")


@pytest.mark.asyncio
async def test_read_lines(fs_tool):
    content = await fs_tool.read_lines("hello.py", 1, 2, buffer=0)
    assert "greet" in content
    lines = content.strip().split("\n")
    assert len(lines) == 2
