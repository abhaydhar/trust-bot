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
    # Delphi/Pascal
    ".pas", ".dpr", ".dfm", ".inc",
    # Legacy/Mainframe
    ".cbl", ".cob",  # COBOL
    ".rpg", ".rpgle",  # RPG
    ".nat",  # Natural
    ".foc",  # FOCUS
}

LANGUAGE_MAP = {
    ".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".cs": "csharp", ".go": "go",
    ".kt": "kotlin", ".rb": "ruby", ".rs": "rust", ".cpp": "cpp", ".c": "c",
    ".h": "c", ".hpp": "cpp", ".scala": "scala", ".swift": "swift", ".php": "php",
    # Delphi/Pascal
    ".pas": "delphi", ".dpr": "delphi", ".dfm": "delphi", ".inc": "delphi",
    # Legacy/Mainframe
    ".cbl": "cobol", ".cob": "cobol",
    ".rpg": "rpg", ".rpgle": "rpg",
    ".nat": "natural",
    ".foc": "focus",
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
    "delphi": [
        # Delphi implementation: procedure TClassName.MethodName / function TClassName.MethodName
        # The optional (?:(\w+)\.)? captures the class prefix so "name" gets the method name.
        re.compile(
            r"^\s*(?:function|procedure)\s+(?:(?P<delphi_class>\w+)\.)?(?P<name>\w+)",
            re.MULTILINE | re.IGNORECASE,
        ),
        re.compile(
            r"^\s*(?:constructor|destructor)\s+(?:(?P<delphi_class>\w+)\.)?(?P<name>\w+)",
            re.MULTILINE | re.IGNORECASE,
        ),
    ],
    "cobol": [
        # COBOL paragraph/section patterns
        re.compile(r"^\s*(?P<name>[A-Z0-9\-]+)\s+(?:SECTION|DIVISION)\.", re.MULTILINE),
        re.compile(r"^\s*(?P<name>[A-Z0-9\-]+)\.\s*$", re.MULTILINE),
    ],
    "rpg": [
        # RPG procedure patterns
        re.compile(r"^\s*DCL-PROC\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE),
        re.compile(r"^\s*BEGSR\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE),
    ],
    "natural": [
        # Natural subroutine patterns
        re.compile(r"^\s*DEFINE\s+(?:SUBROUTINE|FUNCTION)\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE),
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


def _parse_dfm_file(content: str, rel_path: str) -> list[CodeChunk]:
    """
    Parse a Delphi .dfm form file to extract:
    - A chunk for each top-level form object (e.g., TForm1)
    - Event handler bindings stored in chunk.metadata["event_handlers"]

    DFM format example:
        object Form1: TForm1
          OnCreate = FormCreate
          object Button1: TButton
            OnClick = Button1Click
          end
        end

    Each form produces a CodeChunk whose metadata contains the event bindings,
    which the call graph builder uses to create form-to-handler edges.
    """
    chunks: list[CodeChunk] = []
    lines = content.splitlines()

    # Match top-level "object Name: TClassName"
    form_pattern = re.compile(
        r"^\s*object\s+(?P<name>\w+)\s*:\s*(?P<class>\w+)",
        re.IGNORECASE,
    )
    # Match "OnXxx = HandlerName" event bindings
    event_pattern = re.compile(
        r"^\s*On\w+\s*=\s*(?P<handler>\w+)",
        re.IGNORECASE,
    )

    current_form_name = ""
    current_form_class = ""
    current_form_start = 0
    event_handlers: list[str] = []
    depth = 0

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        if form_pattern.match(stripped) and depth == 0:
            m = form_pattern.match(stripped)
            current_form_name = m.group("name")
            current_form_class = m.group("class")
            current_form_start = i
            event_handlers = []
            depth = 1
        elif stripped.lower().startswith("object ") and depth > 0:
            depth += 1
            # Also check events on nested objects
            em = event_pattern.match(stripped)
            if em:
                event_handlers.append(em.group("handler"))
        elif stripped.lower() == "end" and depth > 0:
            depth -= 1
            if depth == 0 and current_form_name:
                chunks.append(CodeChunk(
                    file_path=rel_path,
                    language="delphi",
                    function_name=current_form_name,
                    class_name=current_form_class,
                    line_start=current_form_start,
                    line_end=i,
                    content="\n".join(lines[current_form_start - 1:i]),
                    metadata={"event_handlers": event_handlers, "is_dfm_form": True},
                ))
                current_form_name = ""
        else:
            em = event_pattern.match(stripped)
            if em and depth > 0:
                event_handlers.append(em.group("handler"))

    return chunks


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

    # .dfm files need special parsing (declarative form definitions)
    if ext.lower() == ".dfm":
        return _parse_dfm_file(content, rel_path)

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
    # Each entry: (line_num, name, kind, explicit_class)
    definitions: list[tuple[int, str, str, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(content):
            name = match.group("name")
            line_num = content[:match.start()].count("\n") + 1
            indent = match.groupdict().get("indent", "")
            kind = "class" if "class" in match.group(0).lower().split() else "function"
            # Delphi: extract explicit class from "TClassName.MethodName" patterns
            explicit_class = ""
            try:
                explicit_class = match.group("delphi_class") or ""
            except IndexError:
                pass
            definitions.append((line_num, name, kind, explicit_class))

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

    for i, (line_num, name, kind, explicit_class) in enumerate(definitions):
        if kind == "class":
            current_class = name

        # Chunk extends from this definition to the next one (or EOF)
        start = line_num
        if i + 1 < len(definitions):
            end = definitions[i + 1][0] - 1
        else:
            end = len(lines)

        # Trim trailing blank lines
        while end > start and not lines[end - 1].strip():
            end -= 1

        chunk_content = "\n".join(lines[start - 1 : end])

        # Use explicit class (e.g. TfrmMain from "procedure TfrmMain.MethodName")
        # falling back to the current_class from class definitions
        if explicit_class:
            class_name = explicit_class
        elif kind == "function":
            class_name = current_class
        else:
            class_name = ""

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
