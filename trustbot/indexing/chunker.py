"""
Text-based code chunker that splits source files into function-level chunks
using regex patterns. No AST parser required.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("trustbot.indexing.chunker")

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vs", "bin", "obj", "target",
}

CODE_EXTENSIONS = {
    ".py", ".java", ".js", ".ts", ".jsx", ".tsx", ".cs",
    ".go", ".kt", ".rb", ".rs", ".cpp", ".c", ".h", ".hpp",
    ".scala", ".swift", ".php",
}

LANGUAGE_MAP = {
    ".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".cs": "csharp", ".go": "go",
    ".kt": "kotlin", ".rb": "ruby", ".rs": "rust", ".cpp": "cpp", ".c": "c",
    ".h": "c", ".hpp": "cpp", ".scala": "scala", ".swift": "swift", ".php": "php",
}

# Regex patterns to find function/method definitions per language.
# Named group "name" captures the function name.
FUNC_DEF_PATTERNS: dict[str, list[re.Pattern]] = {
    "python": [
        re.compile(r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+(?P<name>\w+)\s*\(", re.MULTILINE),
        re.compile(r"^(?P<indent>[ \t]*)class\s+(?P<name>\w+)", re.MULTILINE),
    ],
    "java": [
        re.compile(
            r"(?:(?:public|private|protected|static|final|abstract|synchronized)\s+)*"
            r"[\w<>\[\],\s]+\s+(?P<name>\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{",
            re.MULTILINE,
        ),
    ],
    "javascript": [
        re.compile(r"(?:async\s+)?function\s+(?P<name>\w+)\s*\(", re.MULTILINE),
        re.compile(r"(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE),
        re.compile(r"(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?function", re.MULTILINE),
        re.compile(r"class\s+(?P<name>\w+)", re.MULTILINE),
    ],
    "typescript": [
        re.compile(r"(?:async\s+)?function\s+(?P<name>\w+)\s*[\(<]", re.MULTILINE),
        re.compile(r"(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE),
        re.compile(r"(?:export\s+)?class\s+(?P<name>\w+)", re.MULTILINE),
        re.compile(r"(?:export\s+)?interface\s+(?P<name>\w+)", re.MULTILINE),
    ],
    "csharp": [
        re.compile(
            r"(?:(?:public|private|protected|internal|static|virtual|override|abstract|async)\s+)*"
            r"[\w<>\[\]]+\s+(?P<name>\w+)\s*\(",
            re.MULTILINE,
        ),
        re.compile(r"class\s+(?P<name>\w+)", re.MULTILINE),
    ],
    "go": [
        re.compile(r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(?P<name>\w+)\s*\(", re.MULTILINE),
    ],
    "kotlin": [
        re.compile(r"(?:suspend\s+)?fun\s+(?P<name>\w+)\s*[\(<]", re.MULTILINE),
        re.compile(r"class\s+(?P<name>\w+)", re.MULTILINE),
    ],
}


@dataclass
class CodeChunk:
    """A single chunk of code extracted from a source file."""

    file_path: str
    language: str
    function_name: str
    class_name: str
    line_start: int
    line_end: int
    content: str
    chunk_id: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.chunk_id:
            self.chunk_id = f"{self.file_path}::{self.class_name}::{self.function_name}"


def chunk_file(file_path: Path, root: Path) -> list[CodeChunk]:
    """
    Split a single source file into function-level code chunks.
    Falls back to file-level chunks if no functions are detected.
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    ext = file_path.suffix
    language = LANGUAGE_MAP.get(ext, "unknown")
    rel_path = str(file_path.relative_to(root)).replace("\\", "/")
    lines = content.splitlines()

    if not lines:
        return []

    patterns = FUNC_DEF_PATTERNS.get(language, [])
    if not patterns:
        # Unsupported language — return the whole file as a single chunk
        return [
            CodeChunk(
                file_path=rel_path,
                language=language,
                function_name="<module>",
                class_name="",
                line_start=1,
                line_end=len(lines),
                content=content,
            )
        ]

    # Find all function/class definition positions
    definitions: list[tuple[int, str, str]] = []  # (line_num, name, type)
    for pattern in patterns:
        for match in pattern.finditer(content):
            name = match.group("name")
            line_num = content[:match.start()].count("\n") + 1
            indent = match.groupdict().get("indent", "")
            kind = "class" if "class" in match.group(0) else "function"
            definitions.append((line_num, name, kind))

    definitions.sort(key=lambda d: d[0])

    if not definitions:
        return [
            CodeChunk(
                file_path=rel_path,
                language=language,
                function_name="<module>",
                class_name="",
                line_start=1,
                line_end=len(lines),
                content=content,
            )
        ]

    chunks: list[CodeChunk] = []
    current_class = ""

    for i, (line_num, name, kind) in enumerate(definitions):
        if kind == "class":
            current_class = name

        # Chunk extends from this definition to the next one (or EOF)
        start = line_num
        if i + 1 < len(definitions):
            end = definitions[i + 1][0] - 1
        else:
            end = len(lines)

        # For Python, trim trailing blank lines
        while end > start and not lines[end - 1].strip():
            end -= 1

        chunk_content = "\n".join(lines[start - 1 : end])
        class_name = current_class if kind == "function" else ""

        chunks.append(
            CodeChunk(
                file_path=rel_path,
                language=language,
                function_name=name,
                class_name=class_name,
                line_start=start,
                line_end=end,
                content=chunk_content,
            )
        )

    return chunks


def chunk_codebase(root: Path) -> list[CodeChunk]:
    """Walk the codebase and chunk all recognized source files."""
    root = root.resolve()
    all_chunks: list[CodeChunk] = []
    file_count = 0

    for dir_path, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for filename in files:
            ext = os.path.splitext(filename)[1]
            if ext not in CODE_EXTENSIONS:
                continue
            file_path = Path(dir_path) / filename
            chunks = chunk_file(file_path, root)
            all_chunks.extend(chunks)
            file_count += 1

    logger.info("Chunked %d files into %d chunks", file_count, len(all_chunks))
    return all_chunks
