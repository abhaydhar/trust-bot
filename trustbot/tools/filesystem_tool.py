from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from trustbot.config import settings
from trustbot.tools.base import BaseTool

logger = logging.getLogger("trustbot.tools.filesystem")

# Patterns for detecting function/method definitions across common languages.
# These are intentionally broad — the LLM handles precise analysis.
FUNCTION_PATTERNS: dict[str, re.Pattern] = {
    ".py": re.compile(
        r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+(?P<name>\w+)\s*\(", re.MULTILINE
    ),
    ".java": re.compile(
        r"(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+(?P<name>\w+)\s*\(", re.MULTILINE
    ),
    ".js": re.compile(
        r"(?:async\s+)?(?:function\s+(?P<name>\w+)|(?:const|let|var)\s+(?P<name2>\w+)\s*=\s*(?:async\s+)?\(?)",
        re.MULTILINE,
    ),
    ".ts": re.compile(
        r"(?:async\s+)?(?:function\s+(?P<name>\w+)|(?:const|let|var)\s+(?P<name2>\w+)\s*[=:])",
        re.MULTILINE,
    ),
    ".cs": re.compile(
        r"(?:public|private|protected|internal|static|\s)+[\w<>\[\]]+\s+(?P<name>\w+)\s*\(",
        re.MULTILINE,
    ),
    ".go": re.compile(r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(?P<name>\w+)\s*\(", re.MULTILINE),
    ".kt": re.compile(r"(?:fun|suspend\s+fun)\s+(?P<name>\w+)\s*\(", re.MULTILINE),
}

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vs", "bin", "obj", "target",
}

CODE_EXTENSIONS = {
    ".py", ".java", ".js", ".ts", ".jsx", ".tsx", ".cs",
    ".go", ".kt", ".rb", ".rs", ".cpp", ".c", ".h", ".hpp",
    ".scala", ".swift", ".php",
}


class FilesystemTool(BaseTool):
    """
    Tool for accessing the local codebase filesystem.

    All paths are resolved relative to the configured codebase root
    and sandboxed to prevent escaping that directory.
    """

    name = "filesystem"
    description = (
        "Read files, list directories, and search code in the local filesystem. "
        "All paths are relative to the configured codebase root."
    )

    def __init__(self) -> None:
        super().__init__()
        self._root: Path = Path(".")

    async def initialize(self) -> None:
        self._root = settings.codebase_root.resolve()
        if not self._root.exists():
            raise FileNotFoundError(f"Codebase root does not exist: {self._root}")
        logger.info("Filesystem tool initialized with root: %s", self._root)

    async def shutdown(self) -> None:
        pass

    def _resolve_safe(self, path: str) -> Path:
        """Resolve a path, ensuring it stays within the codebase root."""
        resolved = (self._root / path).resolve()
        if not str(resolved).startswith(str(self._root)):
            raise PermissionError(
                f"Path '{path}' resolves outside the codebase root. Access denied."
            )
        return resolved

    async def read_file(self, path: str) -> str:
        """Read the full contents of a file."""
        target = self._resolve_safe(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return target.read_text(encoding="utf-8", errors="replace")

    async def read_lines(
        self, path: str, start: int, end: int, buffer: int | None = None
    ) -> str:
        """
        Read specific lines from a file (1-indexed, inclusive).
        Optionally expand the range by `buffer` lines on each side.
        """
        if buffer is None:
            buffer = settings.function_context_buffer_lines
        target = self._resolve_safe(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        actual_start = max(0, start - 1 - buffer)
        actual_end = min(len(lines), end + buffer)
        selected = lines[actual_start:actual_end]
        numbered = [f"{actual_start + i + 1}| {line}" for i, line in enumerate(selected)]
        return "\n".join(numbered)

    async def list_directory(self, path: str = ".") -> list[str]:
        """List files and directories at the given path."""
        target = self._resolve_safe(path)
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        entries: list[str] = []
        for entry in sorted(target.iterdir()):
            if entry.name in IGNORED_DIRS:
                continue
            suffix = "/" if entry.is_dir() else ""
            entries.append(entry.name + suffix)
        return entries

    async def search_text(self, query: str, file_extensions: list[str] | None = None) -> list[dict]:
        """
        Search for a text pattern across all code files in the codebase.
        Returns matching file paths with line numbers and line content.
        """
        if file_extensions is None:
            file_extensions = list(CODE_EXTENSIONS)

        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results: list[dict] = []
        max_results = 50

        for root_dir, dirs, files in os.walk(self._root):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for filename in files:
                if len(results) >= max_results:
                    return results
                ext = os.path.splitext(filename)[1]
                if ext not in file_extensions:
                    continue
                filepath = Path(root_dir) / filename
                try:
                    content = filepath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        rel_path = str(filepath.relative_to(self._root))
                        results.append({
                            "file": rel_path,
                            "line_number": i,
                            "line": line.strip(),
                        })
                        if len(results) >= max_results:
                            return results
        return results

    async def find_function(self, function_name: str, file_path: str | None = None) -> list[dict]:
        """
        Find a function definition by name. If file_path is given, search only that file.
        Otherwise search the entire codebase. Returns location info for each match.
        """
        results: list[dict] = []

        if file_path:
            files_to_search = [self._resolve_safe(file_path)]
        else:
            files_to_search = list(self._iter_code_files())

        for fpath in files_to_search:
            ext = fpath.suffix
            pattern = FUNCTION_PATTERNS.get(ext)
            if pattern is None:
                # Fallback: generic search for the function name near common keywords
                pattern = re.compile(
                    rf"(?:def|func|function|fn)\s+{re.escape(function_name)}\s*\(",
                    re.MULTILINE,
                )

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for match in pattern.finditer(content):
                name = match.group("name") if "name" in match.groupdict() else None
                if name is None:
                    name = match.groupdict().get("name2")
                if name != function_name:
                    continue

                line_num = content[:match.start()].count("\n") + 1
                rel_path = str(fpath.relative_to(self._root))
                results.append({
                    "file": rel_path,
                    "line_number": line_num,
                    "match": match.group(0).strip(),
                })

        return results

    async def extract_function_body(
        self, path: str, function_name: str
    ) -> str | None:
        """
        Extract the body of a named function from a file.
        Uses indentation/brace heuristics to find the function boundaries.
        Returns the function code with line numbers, or None if not found.
        """
        target = self._resolve_safe(path)
        if not target.is_file():
            return None

        content = target.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        ext = target.suffix

        # Find the function start
        pattern = FUNCTION_PATTERNS.get(ext)
        if pattern is None:
            pattern = re.compile(
                rf"(?:def|func|function|fn)\s+{re.escape(function_name)}\s*\(",
                re.MULTILINE,
            )

        match = None
        for m in pattern.finditer(content):
            name = m.group("name") if "name" in m.groupdict() else None
            if name is None:
                name = m.groupdict().get("name2")
            if name == function_name:
                match = m
                break

        if match is None:
            return None

        start_line = content[:match.start()].count("\n")

        # Determine end of function based on language conventions
        if ext == ".py":
            end_line = self._find_python_function_end(lines, start_line)
        else:
            end_line = self._find_brace_function_end(lines, start_line)

        selected = lines[start_line : end_line + 1]
        if len(selected) > settings.max_function_lines_for_llm:
            # Truncate very large functions
            half = settings.max_function_lines_for_llm // 2
            head = selected[:half]
            tail = selected[-half:]
            selected = head + [f"    // ... ({len(selected) - settings.max_function_lines_for_llm} lines truncated) ..."] + tail

        numbered = [f"{start_line + i + 1}| {line}" for i, line in enumerate(selected)]
        return "\n".join(numbered)

    def _find_python_function_end(self, lines: list[str], start: int) -> int:
        """Find the end of a Python function using indentation."""
        if start >= len(lines):
            return start

        # Determine base indentation from the def line
        def_line = lines[start]
        base_indent = len(def_line) - len(def_line.lstrip())

        for i in range(start + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                return i - 1
        return len(lines) - 1

    def _find_brace_function_end(self, lines: list[str], start: int) -> int:
        """Find the end of a brace-delimited function (Java, JS, C#, Go, etc.)."""
        depth = 0
        found_open = False
        for i in range(start, len(lines)):
            for char in lines[i]:
                if char == "{":
                    depth += 1
                    found_open = True
                elif char == "}":
                    depth -= 1
                    if found_open and depth == 0:
                        return i
        return min(start + 100, len(lines) - 1)

    def _iter_code_files(self):
        """Iterate over all code files in the codebase root."""
        for root_dir, dirs, files in os.walk(self._root):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for filename in files:
                if os.path.splitext(filename)[1] in CODE_EXTENSIONS:
                    yield Path(root_dir) / filename

    async def check_file_exists(self, path: str) -> bool:
        """Check if a file exists at the given path."""
        try:
            target = self._resolve_safe(path)
            return target.is_file()
        except (PermissionError, FileNotFoundError):
            return False

    async def check_function_exists(self, path: str, function_name: str) -> bool:
        """Quick check: does the function name appear in the given file?"""
        try:
            target = self._resolve_safe(path)
            if not target.is_file():
                return False
            content = target.read_text(encoding="utf-8", errors="replace")
            return function_name in content
        except (PermissionError, FileNotFoundError, OSError):
            return False
