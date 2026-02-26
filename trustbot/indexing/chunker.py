"""
Text-based code chunker that splits source files into function-level chunks
using regex patterns.  No AST parser required.

When Agent 0 language profiles are available, patterns/extensions/rules are
read from the profiles.  Otherwise, built-in seed profiles are used as
fallback — preserving full backward compatibility.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trustbot.models.language_profile import LanguageProfile

logger = logging.getLogger("trustbot.indexing.chunker")

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vs", "bin", "obj", "target",
    ".trustbot",
}

# ── Active language profiles (set by Agent 0 or seed fallback) ───────────

_active_profiles: dict[str, "LanguageProfile"] = {}
_ext_to_lang: dict[str, str] = {}
_code_extensions: set[str] = set()


def set_language_profiles(profiles: dict[str, "LanguageProfile"]) -> None:
    """Install Agent 0 profiles for use by the chunker.

    Called once at index time after Agent 0 completes.  Rebuilds the
    extension-to-language map and recognised-extensions set.
    """
    global _active_profiles, _ext_to_lang, _code_extensions
    _active_profiles = dict(profiles)
    _ext_to_lang = {}
    _code_extensions = set()
    for lang, profile in profiles.items():
        for ext in profile.file_extensions:
            if ext == "":
                _ext_to_lang[""] = lang
                _code_extensions.add("")
            else:
                ext_lower = ext.lower() if ext.startswith(".") else f".{ext}".lower()
                _ext_to_lang[ext_lower] = lang
                _code_extensions.add(ext_lower)
    logger.info(
        "Chunker profiles loaded: %d languages, %d extensions (extensionless=%s)",
        len(profiles), len(_code_extensions), "" in _code_extensions,
    )


def _ensure_profiles_loaded() -> None:
    """Lazy-load seed profiles if Agent 0 hasn't run yet."""
    if _active_profiles:
        return
    from trustbot.agents.agent0_seed_profiles import get_all_seed_profiles
    set_language_profiles(get_all_seed_profiles())


def get_language_for_ext(ext: str) -> str:
    """Return the language name for a file extension."""
    _ensure_profiles_loaded()
    return _ext_to_lang.get(ext.lower(), "unknown")


def get_code_extensions() -> set[str]:
    """Return the set of all recognised source-code file extensions."""
    _ensure_profiles_loaded()
    return set(_code_extensions)


def _get_profile(language: str) -> "LanguageProfile | None":
    """Get the active profile for a language."""
    _ensure_profiles_loaded()
    return _active_profiles.get(language)


def _compile_patterns(profile: "LanguageProfile") -> list[re.Pattern]:
    """Compile function_def + class_def regex patterns from a profile."""
    compiled: list[re.Pattern] = []
    for pat_str in profile.function_def_patterns + profile.class_def_patterns:
        if "(?P<name>" not in pat_str and "(?P<name>" not in pat_str.replace(" ", ""):
            logger.warning(
                "Skipping pattern without (?P<name>...) group in %s: %s",
                profile.language, pat_str,
            )
            continue
        try:
            compiled.append(re.compile(pat_str, re.MULTILINE | re.IGNORECASE))
        except re.error as e:
            logger.warning(
                "Invalid regex in profile %s: %s — %s",
                profile.language, pat_str, e,
            )
    return compiled


# ── Data model ───────────────────────────────────────────────────────────

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


# ── Special-file parsers ────────────────────────────────────────────────

def _parse_dfm_file(content: str, rel_path: str) -> list[CodeChunk]:
    """Parse a Delphi .dfm form file to extract form objects and event handlers."""
    chunks: list[CodeChunk] = []
    lines = content.splitlines()

    form_pattern = re.compile(
        r"^\s*object\s+(?P<name>\w+)\s*:\s*(?P<class>\w+)",
        re.IGNORECASE,
    )
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
            em = event_pattern.match(stripped)
            if em:
                event_handlers.append(em.group("handler"))
        elif stripped.lower() == "end" and depth > 0:
            depth -= 1
            if depth == 0 and current_form_name:
                unique_handlers = list(dict.fromkeys(event_handlers))
                chunks.append(CodeChunk(
                    file_path=rel_path,
                    language="delphi",
                    function_name=current_form_name,
                    class_name=current_form_class,
                    line_start=current_form_start,
                    line_end=i,
                    content="\n".join(lines[current_form_start - 1:i]),
                    metadata={"event_handlers": unique_handlers, "is_dfm_form": True},
                ))
                current_form_name = ""
        else:
            em = event_pattern.match(stripped)
            if em and depth > 0:
                event_handlers.append(em.group("handler"))

    return chunks


def _parse_special_file(
    content: str,
    rel_path: str,
    language: str,
    profile: "LanguageProfile",
    ext: str,
) -> list[CodeChunk] | None:
    """Dispatch to the correct special-file parser based on profile config.

    Returns None if no special-file config matches, meaning normal chunking
    should be used.
    """
    for sf in profile.special_file_types:
        if sf.extension.lower() == ext.lower():
            if sf.parser_type == "dfm_form":
                return _parse_dfm_file(content, rel_path)
            logger.warning(
                "Unknown special file parser_type '%s' for %s",
                sf.parser_type, ext,
            )
    return None


# ── Core chunking ────────────────────────────────────────────────────────

def chunk_file(file_path: Path, root: Path) -> list[CodeChunk]:
    """Split a single source file into function-level code chunks."""
    _ensure_profiles_loaded()

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    ext = file_path.suffix.lower()
    language = _ext_to_lang.get(ext, "unknown")
    rel_path = str(file_path.relative_to(root)).replace("\\", "/")
    lines = content.splitlines()

    if not lines:
        return []

    profile = _get_profile(language)

    if profile:
        special = _parse_special_file(content, rel_path, language, profile, ext)
        if special is not None:
            return special
    elif ext.lower() == ".dfm":
        return _parse_dfm_file(content, rel_path)

    patterns = _compile_patterns(profile) if profile else []
    if not patterns:
        fname = file_path.stem if file_path.suffix else file_path.name
        return [
            CodeChunk(
                file_path=rel_path,
                language=language,
                function_name=fname if fname else "<module>",
                class_name="",
                line_start=1,
                line_end=len(lines),
                content=content,
            )
        ]

    class_prefix_group = (
        profile.named_regex_groups.get("class_prefix", "")
        if profile else ""
    )

    definitions: list[tuple[int, str, str, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(content):
            try:
                name = match.group("name")
            except IndexError:
                continue
            if not name:
                continue
            line_num = content[:match.start()].count("\n") + 1
            kind = "class" if "class" in match.group(0).lower().split() else "function"
            explicit_class = ""
            if class_prefix_group:
                try:
                    explicit_class = match.group(class_prefix_group) or ""
                except (IndexError, re.error):
                    pass
            definitions.append((line_num, name, kind, explicit_class))

    definitions.sort(key=lambda d: d[0])

    if profile and profile.forward_declaration_rules:
        fwd = profile.forward_declaration_rules
        if fwd.keyword and fwd.strategy == "discard_before_keyword_unless_class_prefix":
            impl_line: int | None = None
            for idx, line_text in enumerate(lines, 1):
                if line_text.strip().lower() == fwd.keyword.lower():
                    impl_line = idx
                    break
            if impl_line is not None:
                definitions = [
                    d for d in definitions
                    if d[0] > impl_line or d[3]
                ]

    if not definitions:
        fname = file_path.stem if file_path.suffix else file_path.name
        return [
            CodeChunk(
                file_path=rel_path,
                language=language,
                function_name=fname if fname else "<module>",
                class_name="",
                line_start=1,
                line_end=len(lines),
                content=content,
            )
        ]

    chunks: list[CodeChunk] = []
    current_class = ""

    # Preamble chunk: code before the first definition (main program body,
    # module-level code, etc.).  Named after the file so it appears in the
    # index as a callable entry point.  Generic — benefits any language.
    if definitions[0][0] > 1:
        preamble_end = definitions[0][0] - 1
        while preamble_end > 0 and not lines[preamble_end - 1].strip():
            preamble_end -= 1
        if preamble_end > 0:
            fname = file_path.stem if file_path.suffix else file_path.name
            chunks.append(
                CodeChunk(
                    file_path=rel_path,
                    language=language,
                    function_name=fname if fname else "<module>",
                    class_name="",
                    line_start=1,
                    line_end=preamble_end,
                    content="\n".join(lines[:preamble_end]),
                )
            )

    for i, (line_num, name, kind, explicit_class) in enumerate(definitions):
        if kind == "class":
            current_class = name

        start = line_num
        if i + 1 < len(definitions):
            end = definitions[i + 1][0] - 1
        else:
            end = len(lines)

        while end > start and not lines[end - 1].strip():
            end -= 1

        chunk_content = "\n".join(lines[start - 1 : end])

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
    """Walk the codebase and chunk all recognised source files."""
    _ensure_profiles_loaded()
    root = root.resolve()
    all_chunks: list[CodeChunk] = []
    seen_ids: set[str] = set()
    file_count = 0
    extensions = get_code_extensions()

    for dir_path, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue
            file_path = Path(dir_path) / filename
            chunks = chunk_file(file_path, root)
            for chunk in chunks:
                if chunk.chunk_id not in seen_ids:
                    seen_ids.add(chunk.chunk_id)
                    all_chunks.append(chunk)
            file_count += 1

    logger.info("Chunked %d files into %d unique chunks", file_count, len(all_chunks))
    return all_chunks
