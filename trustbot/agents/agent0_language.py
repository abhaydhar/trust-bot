"""
Agent 0 — Language Intelligence Profiler.

Runs at INDEX time (before Agent 1/2/3) to auto-detect languages in the
target codebase and generate a ``LanguageProfile`` for each one.  These
profiles replace all hardcoded language logic in the chunker, LLM call
extractor, and structural chunker.

Phases:
    1. Detect — scan file extensions, group by language.
    2. Sample — pick representative files per language.
    3. Generate — send samples to the LLM with a structured prompt.
    4. Validate — run generated patterns against ALL files, refine on gaps.
    5. Persist — save profiles as JSON under ``.trustbot/profiles/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trustbot.models.language_profile import (
    BlockRuleConfig,
    ForwardDeclarationConfig,
    LanguageProfile,
    SpecialFileConfig,
)

logger = logging.getLogger("trustbot.agents.agent0")

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vs", "bin", "obj", "target",
    ".trustbot",
}

_SEED_EXTENSION_MAP: dict[str, str] = {
    ".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".cs": "csharp", ".go": "go",
    ".kt": "kotlin", ".rb": "ruby", ".rs": "rust", ".cpp": "cpp", ".c": "c",
    ".h": "c", ".hpp": "cpp", ".scala": "scala", ".swift": "swift", ".php": "php",
    ".pas": "delphi", ".dpr": "delphi", ".dfm": "delphi", ".inc": "delphi",
    ".cbl": "cobol", ".cob": "cobol",
    ".rpg": "rpg", ".rpgle": "rpg",
    ".nat": "natural", ".foc": "focus",
}

MAX_SAMPLE_FILES = 8
MAX_SAMPLE_LINES = 500
MAX_REFINEMENT_CYCLES = 3

# ---------------------------------------------------------------------------
# LLM prompt for profile generation
# ---------------------------------------------------------------------------

PROFILE_GENERATION_PROMPT = """\
You are an expert programming-language analyst.  Given sample source files
from a codebase, produce a **complete language profile** as a single JSON
object that precisely describes how to parse, chunk, and analyse call graphs
for this language.

REQUIREMENTS — be exhaustive; missing patterns will cause downstream failures:

1. **function_def_patterns** — regex strings (Python `re` syntax, MULTILINE)
   that match EVERY form of function/procedure/method/subroutine definition
   in this language.  Each pattern MUST contain a named group ``(?P<name>...)``
   capturing the function name.  Optionally include ``(?P<indent>...)`` for
   indentation and ``(?P<class_prefix>\\w+)`` for class-qualified names
   (e.g. ``TClassName.MethodName`` in Delphi).
   Cover ALL variants: async, static, virtual, override, abstract, class
   methods, constructors, destructors, anonymous/inline, etc.

   CRITICAL tips for robust patterns:
   - **Line-numbered sources**: some languages (Natural, COBOL, RPG) prefix
     every source line with a sequence number (e.g. ``0950 DEFINE ...``).
     If the sample files show this, add an optional prefix ``^(?:\\d+\\s+)?``
     so the pattern matches with and without line numbers.
   - **Local subroutines / inner functions**: include patterns for locally
     scoped callable definitions (Natural ``DEFINE SUBROUTINE Name``, RPG
     ``BEGSR Name``, Python nested ``def``, etc.) — these MUST be chunked
     as separate functions so that calls to them are tracked.
   - **Hyphenated identifiers**: if the language allows hyphens in names
     (Natural, COBOL), use ``(?P<name>\\w[\\w\\-]*)`` instead of
     ``(?P<name>\\w+)``.

2. **class_def_patterns** — regex strings that match class/interface/struct
   definitions with a ``(?P<name>\\w+)`` group.

3. **named_regex_groups** — a mapping of semantic role to group name used in
   the patterns above.  At minimum ``{"name": "name"}``.  Add
   ``"class_prefix": "<group>"`` if function patterns capture a qualifying
   class name.

4. **forward_declaration_rules** — if the language separates declarations from
   implementations (e.g. Delphi ``interface`` / ``implementation``), provide:
   ``{"keyword": "<boundary>", "strategy": "discard_before_keyword_unless_class_prefix"}``.
   Otherwise ``null``.

5. **special_file_types** — list of non-source file types that contain event
   bindings or declarative links (e.g. Delphi ``.dfm``, .NET ``.xaml``).
   Each entry: ``{"extension": ".dfm", "parser_type": "dfm_form",
   "object_pattern": "<regex>", "event_pattern": "<regex>",
   "metadata_keys": ["event_handlers"]}``.

6. **block_rules** — open/close block-boundary rules for scope-aware
   structural chunking.  Each rule: ``{"block_type": "procedure",
   "open_pattern": "<regex>", "close_pattern": "<regex>",
   "name_group": "name"}``.  Leave empty ``[]`` if the language does not
   benefit from structural chunking.

7. **llm_call_prompt** — a detailed, language-specific prompt addendum
   (plain text, will be appended to a base prompt) that tells an LLM:
   - Which syntactic patterns ARE function/procedure calls in this language
     (with concrete examples from the provided code samples).
   - Which patterns are NOT calls (variable declarations, imports, type
     references, class inheritance, etc. — with concrete examples).
   This must be exhaustive for the language.

8. **skip_tokens** — list of language KEYWORDS that should never be treated
   as function calls (e.g. ``["BEGIN", "END", "IF", "ELSE", ...]``).

9. **supports_bare_identifiers** — ``true`` if this language allows calling
   functions/procedures without parentheses (e.g. Delphi, Ruby).
   ``false`` for languages where calls always use ``()``.

10. **bare_id_negative_lookahead** — if ``supports_bare_identifiers`` is true,
    provide a regex lookahead to reject false matches (e.g.
    ``"(?!\\\\s*\\\\.)"`` to reject property access in Delphi).
    Otherwise ``""``.

11. **single_line_comment**, **multi_line_comment_open**,
    **multi_line_comment_close**, **string_delimiters** — comment and string
    syntax for the language.

12. **call_keyword_patterns** — regex strings for language-specific call
    invocation syntax that does NOT use parentheses.  Each pattern MUST
    contain a named group ``(?P<callee>...)``.  Examples:
    - Natural: ``FETCH 'ProgramName'`` → ``"(?:FETCH|FETCH\\s+RETURN)\\s+'(?P<callee>\\w+)'"``
    - Natural: ``PERFORM SubName`` → ``"PERFORM\\s+(?P<callee>\\w[\\w\\-]*)"``
    - COBOL: ``PERFORM paragraph`` → ``"PERFORM\\s+(?P<callee>[A-Z0-9\\-]+)"``
    - RPG: ``EXSR subroutine`` → ``"EXSR\\s+(?P<callee>\\w+)"``
    Leave empty ``[]`` for languages where all calls use ``()``.

Return ONLY the JSON object — no markdown fences, no commentary.

JSON SCHEMA:
{
  "language": "<string>",
  "aliases": ["<string>", ...],
  "file_extensions": [".ext", ...],
  "function_def_patterns": ["<regex>", ...],
  "class_def_patterns": ["<regex>", ...],
  "named_regex_groups": {"name": "name", ...},
  "forward_declaration_rules": {"keyword": "", "strategy": ""} | null,
  "special_file_types": [{"extension": "", "parser_type": "", "object_pattern": "", "event_pattern": "", "metadata_keys": []}],
  "block_rules": [{"block_type": "", "open_pattern": "", "close_pattern": "", "name_group": "name"}],
  "llm_call_prompt": "<string>",
  "skip_tokens": ["<string>", ...],
  "supports_bare_identifiers": false,
  "bare_id_negative_lookahead": "",
  "call_keyword_patterns": ["<regex with (?P<callee>...)>", ...],
  "call_pattern_examples": ["<example from code>", ...],
  "non_call_examples": ["<example from code>", ...],
  "single_line_comment": "//",
  "multi_line_comment_open": "/*",
  "multi_line_comment_close": "*/",
  "string_delimiters": ["\\""]
}
"""

REFINEMENT_PROMPT = """\
Your previously generated regex patterns MISSED some function definitions in
the target codebase.  Below are the lines that contain real function/procedure
definitions but were NOT matched by your patterns.

MISSED LINES (file → line):
{missed_lines}

Analyse these missed lines, update your function_def_patterns and
class_def_patterns to cover them.  Return the FULL updated JSON profile
(same schema as before) — not just the changed fields.
"""


def _create_llm():
    from trustbot.config import settings

    try:
        from langchain_litellm import ChatLiteLLM
    except ImportError:
        from langchain_community.chat_models import ChatLiteLLM

    kwargs: dict[str, Any] = {
        "model": settings.litellm_model,
        "temperature": 0.0,
        "max_tokens": 4096,
    }
    if settings.litellm_api_base:
        kwargs["api_base"] = settings.litellm_api_base
    if settings.litellm_api_key:
        kwargs["api_key"] = settings.litellm_api_key

    return ChatLiteLLM(**kwargs)


def _parse_json_object(text: str) -> dict:
    """Parse a JSON object from LLM output, stripping markdown fences."""
    t = text.strip()
    if "```json" in t:
        t = t.split("```json", 1)[1]
        t = t.split("```", 1)[0]
    elif "```" in t:
        t = t.split("```", 1)[1]
        t = t.split("```", 1)[0]
    t = t.strip()
    if not t:
        raise ValueError("LLM returned empty response (no JSON content)")
    brace = t.find("{")
    if brace > 0:
        t = t[brace:]
    last_brace = t.rfind("}")
    if last_brace >= 0:
        t = t[: last_brace + 1]
    return json.loads(t)


class Agent0LanguageProfiler:
    """Auto-detect languages and generate LanguageProfile objects."""

    def __init__(self, codebase_root: Path) -> None:
        self._root = codebase_root.resolve()
        self._profiles_dir = self._root / ".trustbot" / "profiles"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        progress_callback=None,
    ) -> dict[str, LanguageProfile]:
        """Execute the full 5-phase pipeline and return profiles keyed by language."""

        if progress_callback:
            progress_callback("agent0", "Detecting languages...")

        files_by_lang = self._detect_languages()
        if not files_by_lang:
            logger.warning("No recognised source files found in %s", self._root)
            return {}

        logger.info(
            "Detected %d language(s): %s",
            len(files_by_lang),
            ", ".join(f"{k} ({len(v)} files)" for k, v in files_by_lang.items()),
        )

        profiles: dict[str, LanguageProfile] = {}

        for language, file_list in files_by_lang.items():
            codebase_hash = self._compute_hash(file_list)

            cached = self._load_cached_profile(language, codebase_hash)
            if cached is not None:
                logger.info("Using cached profile for %s (hash match)", language)
                profiles[language] = cached
                continue

            if progress_callback:
                progress_callback(
                    "agent0",
                    f"Generating profile for {language} ({len(file_list)} files)...",
                )

            samples = self._sample_files(file_list)

            profile = await self._generate_profile(language, file_list, samples)

            profile = await self._validate_and_refine(
                profile, language, file_list,
            )

            profile.codebase_hash = codebase_hash
            profile.source_file_count = len(file_list)
            profile.generated_at = datetime.now(timezone.utc).isoformat()

            self._persist_profile(profile)
            profiles[language] = profile

            logger.info(
                "Profile for %s: %d func patterns, %d block rules, "
                "%.0f%% validation coverage",
                language,
                len(profile.function_def_patterns),
                len(profile.block_rules),
                profile.validation_coverage * 100,
            )

        return profiles

    # ------------------------------------------------------------------
    # Phase 1: Language detection
    # ------------------------------------------------------------------

    def _detect_languages(self) -> dict[str, list[Path]]:
        """Scan the codebase and group source files by language.

        Files with known extensions are mapped directly.  Files with no
        extension or unrecognised extensions are collected separately and
        identified via LLM sampling (Phase 1b).
        """
        files_by_lang: dict[str, list[Path]] = {}
        unknown_files: list[Path] = []

        _skip_names = {"readme", "license", "licence", "makefile", "dockerfile",
                       "changelog", "contributing", "authors", ".gitignore",
                       ".gitattributes", ".editorconfig", ".env", ".env.example",
                       "repo-metadata.txt"}
        _skip_exts = {".md", ".txt", ".json", ".xml", ".yaml", ".yml", ".toml",
                      ".cfg", ".ini", ".csv", ".log", ".lock", ".svg", ".png",
                      ".jpg", ".gif", ".ico", ".pdf", ".zip", ".tar", ".gz",
                      ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".class",
                      ".jar", ".war", ".pyc", ".pyo", ".whl", ".egg"}

        for dir_path, dirs, files in os.walk(self._root):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext:
                    lang = _SEED_EXTENSION_MAP.get(ext)
                    if lang:
                        full = Path(dir_path) / filename
                        files_by_lang.setdefault(lang, []).append(full)
                    elif ext not in _skip_exts:
                        unknown_files.append(Path(dir_path) / filename)
                else:
                    if filename.lower() not in _skip_names:
                        unknown_files.append(Path(dir_path) / filename)

        if unknown_files:
            detected = self._identify_unknown_files(unknown_files)
            for lang, flist in detected.items():
                files_by_lang.setdefault(lang, []).extend(flist)

        return files_by_lang

    def _identify_unknown_files(
        self, unknown_files: list[Path],
    ) -> dict[str, list[Path]]:
        """Sample unknown files and use heuristics to identify their language.

        Checks for language-specific keywords in file content rather than
        relying on an LLM call, making this fast and deterministic.
        """
        _LANGUAGE_SIGNATURES: dict[str, list[str]] = {
            "natural": [
                "DEFINE DATA", "END-DEFINE", "DEFINE SUBROUTINE",
                "END-SUBROUTINE", "DEFINE FUNCTION", "CALLNAT",
                "PERFORM ", "RESET ", "FETCH ", "INPUT USING MAP",
            ],
            "cobol": [
                "IDENTIFICATION DIVISION", "DATA DIVISION",
                "PROCEDURE DIVISION", "WORKING-STORAGE SECTION",
                "PERFORM ", "EVALUATE ", "MOVE ",
            ],
            "rpg": [
                "DCL-PROC ", "END-PROC", "DCL-S ", "DCL-DS ",
                "BEGSR ", "ENDSR", "DCL-PI ", "CALLP ",
            ],
            "focus": [
                "TABLE FILE", "-DEFINE FUNCTION", "-DEFINE FILE",
                "GRAPH FILE", "-IF ", "-ENDIF",
            ],
        }

        language_votes: dict[str, int] = {}
        language_files: dict[str, list[Path]] = {}

        sample = unknown_files[:20]
        for fpath in sample:
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                upper = content.upper()
            except OSError:
                continue

            for lang, signatures in _LANGUAGE_SIGNATURES.items():
                hits = sum(1 for sig in signatures if sig.upper() in upper)
                if hits >= 2:
                    language_votes[lang] = language_votes.get(lang, 0) + hits
                    language_files.setdefault(lang, [])

        if not language_votes:
            logger.info(
                "Could not identify language for %d extensionless files",
                len(unknown_files),
            )
            return {}

        best_lang = max(language_votes, key=language_votes.get)
        logger.info(
            "Identified %d extensionless files as %s (keyword score: %d)",
            len(unknown_files), best_lang, language_votes[best_lang],
        )

        result: dict[str, list[Path]] = {best_lang: list(unknown_files)}
        return result

    # ------------------------------------------------------------------
    # Phase 2: Code sampling
    # ------------------------------------------------------------------

    def _sample_files(self, file_list: list[Path]) -> list[tuple[str, str]]:
        """Select representative files and return (relative_path, content) pairs."""
        if not file_list:
            return []

        sized = []
        for f in file_list:
            try:
                sized.append((f, f.stat().st_size))
            except OSError:
                continue

        if not sized:
            return []

        sized.sort(key=lambda x: x[1])

        selected: list[Path] = []
        if sized:
            selected.append(sized[0][0])
        if len(sized) > 1:
            selected.append(sized[-1][0])
        mid = len(sized) // 2
        if len(sized) > 2 and sized[mid][0] not in selected:
            selected.append(sized[mid][0])

        import random
        remaining = [s[0] for s in sized if s[0] not in selected]
        random.seed(42)
        for f in random.sample(remaining, min(MAX_SAMPLE_FILES - len(selected), len(remaining))):
            selected.append(f)

        samples: list[tuple[str, str]] = []
        for fpath in selected[:MAX_SAMPLE_FILES]:
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()[:MAX_SAMPLE_LINES]
                rel = str(fpath.relative_to(self._root)).replace("\\", "/")
                samples.append((rel, "\n".join(lines)))
            except OSError:
                continue

        return samples

    # ------------------------------------------------------------------
    # Phase 3: Profile generation (LLM)
    # ------------------------------------------------------------------

    async def _generate_profile(
        self,
        language: str,
        file_list: list[Path],
        samples: list[tuple[str, str]],
    ) -> LanguageProfile:
        """Send code samples to LLM and parse a LanguageProfile from the response."""
        extensions = sorted({f.suffix.lower() for f in file_list if f.suffix})
        has_extensionless = any(not f.suffix for f in file_list)
        if has_extensionless:
            extensions.append("")

        sample_text = ""
        for rel_path, content in samples:
            sample_text += f"\n--- FILE: {rel_path} ---\n{content}\n"

        user_msg = (
            f"LANGUAGE: {language}\n"
            f"FILE EXTENSIONS: {', '.join(extensions)}\n\n"
            f"SAMPLE SOURCE FILES ({len(samples)} files):\n"
            f"{sample_text}"
        )

        try:
            llm = _create_llm()
            from langchain_core.messages import HumanMessage, SystemMessage

            resp = await llm.ainvoke([
                SystemMessage(content=PROFILE_GENERATION_PROMPT),
                HumanMessage(content=user_msg),
            ])
            raw = resp.content if hasattr(resp, "content") else str(resp)
            data = _parse_json_object(raw)
            profile = self._dict_to_profile(data, language, extensions)
            if has_extensionless and "" not in profile.file_extensions:
                profile.file_extensions.append("")
            logger.info("LLM generated profile for %s successfully", language)
            return profile

        except Exception as exc:
            logger.warning(
                "LLM profile generation failed for %s: %s — using seed fallback",
                language, exc,
            )
            fallback = self._build_seed_profile(language, extensions)
            if has_extensionless and "" not in fallback.file_extensions:
                fallback.file_extensions.append("")
            return fallback

    def _dict_to_profile(
        self,
        data: dict,
        language: str,
        extensions: list[str],
    ) -> LanguageProfile:
        """Convert LLM JSON output to a LanguageProfile, with defensive defaults."""
        fwd = data.get("forward_declaration_rules")
        fwd_cfg = None
        if isinstance(fwd, dict) and fwd.get("keyword"):
            fwd_cfg = ForwardDeclarationConfig(**fwd)

        special = []
        for sf in data.get("special_file_types", []):
            if isinstance(sf, dict) and sf.get("extension"):
                special.append(SpecialFileConfig(**sf))

        blocks = []
        for br in data.get("block_rules", []):
            if isinstance(br, dict) and br.get("open_pattern"):
                blocks.append(BlockRuleConfig(**br))

        return LanguageProfile(
            language=data.get("language", language),
            aliases=data.get("aliases", []),
            file_extensions=data.get("file_extensions", extensions),
            function_def_patterns=data.get("function_def_patterns", []),
            class_def_patterns=data.get("class_def_patterns", []),
            named_regex_groups=data.get("named_regex_groups", {"name": "name"}),
            forward_declaration_rules=fwd_cfg,
            special_file_types=special,
            block_rules=blocks,
            llm_call_prompt=data.get("llm_call_prompt", ""),
            skip_tokens=data.get("skip_tokens", []),
            supports_bare_identifiers=bool(data.get("supports_bare_identifiers", False)),
            bare_id_negative_lookahead=data.get("bare_id_negative_lookahead", ""),
            call_keyword_patterns=data.get("call_keyword_patterns", []),
            call_pattern_examples=data.get("call_pattern_examples", []),
            non_call_examples=data.get("non_call_examples", []),
            single_line_comment=data.get("single_line_comment", "//"),
            multi_line_comment_open=data.get("multi_line_comment_open", "/*"),
            multi_line_comment_close=data.get("multi_line_comment_close", "*/"),
            string_delimiters=data.get("string_delimiters", ['"']),
        )

    # ------------------------------------------------------------------
    # Phase 4: Validation & iterative refinement
    # ------------------------------------------------------------------

    async def _validate_and_refine(
        self,
        profile: LanguageProfile,
        language: str,
        file_list: list[Path],
    ) -> LanguageProfile:
        """Validate patterns against all files and refine with LLM if gaps exist."""

        for cycle in range(MAX_REFINEMENT_CYCLES):
            coverage, missed = self._validate_patterns(profile, file_list)
            profile.validation_coverage = coverage

            if coverage >= 1.0 or not missed:
                logger.info(
                    "Validation PASS for %s: %.0f%% coverage (cycle %d)",
                    language, coverage * 100, cycle,
                )
                return profile

            logger.info(
                "Validation gap for %s: %.0f%% coverage, %d missed lines — "
                "starting refinement cycle %d",
                language, coverage * 100, len(missed), cycle + 1,
            )

            profile = await self._refine_profile(profile, missed)

        final_cov, _ = self._validate_patterns(profile, file_list)
        profile.validation_coverage = final_cov
        logger.info(
            "Final validation for %s after %d cycles: %.0f%% coverage",
            language, MAX_REFINEMENT_CYCLES, final_cov * 100,
        )
        return profile

    def _validate_patterns(
        self,
        profile: LanguageProfile,
        file_list: list[Path],
    ) -> tuple[float, list[str]]:
        """Run regex patterns against all files and compare with keyword scan.

        Returns (coverage_fraction, list_of_missed_line_descriptions).
        """
        compiled: list[re.Pattern] = []
        for pat_str in profile.function_def_patterns + profile.class_def_patterns:
            try:
                compiled.append(re.compile(pat_str, re.MULTILINE | re.IGNORECASE))
            except re.error as e:
                logger.warning("Invalid regex in profile for %s: %s — %s", profile.language, pat_str, e)

        keywords = self._get_naive_keywords(profile.language)

        total_keyword_hits = 0
        total_pattern_hits = 0
        missed_lines: list[str] = []

        for fpath in file_list:
            if any(sf.extension.lower() == fpath.suffix.lower() for sf in profile.special_file_types):
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel = str(fpath.relative_to(self._root)).replace("\\", "/")

            keyword_lines: set[int] = set()
            for kw in keywords:
                for m in re.finditer(
                    r"(?:^|\s)" + re.escape(kw) + r"\s",
                    content,
                    re.MULTILINE | re.IGNORECASE,
                ):
                    lineno = content[:m.start()].count("\n") + 1
                    keyword_lines.add(lineno)

            pattern_lines: set[int] = set()
            for pat in compiled:
                for m in pat.finditer(content):
                    lineno = content[:m.start()].count("\n") + 1
                    pattern_lines.add(lineno)

            total_keyword_hits += len(keyword_lines)
            total_pattern_hits += len(pattern_lines)

            lines = content.splitlines()
            for lineno in sorted(keyword_lines - pattern_lines):
                if 0 < lineno <= len(lines):
                    line_text = lines[lineno - 1].strip()
                    if line_text:
                        missed_lines.append(f"{rel}:{lineno}: {line_text}")

        if total_keyword_hits == 0:
            return 1.0, []

        coverage = min(total_pattern_hits / total_keyword_hits, 1.0)
        return coverage, missed_lines[:50]

    def _get_naive_keywords(self, language: str) -> list[str]:
        """Return simple keywords that indicate function definitions for a language."""
        kw_map: dict[str, list[str]] = {
            "python": ["def ", "class "],
            "java": ["void ", "public ", "private ", "protected "],
            "javascript": ["function ", "class "],
            "typescript": ["function ", "class ", "interface "],
            "delphi": ["procedure ", "function ", "constructor ", "destructor "],
            "csharp": ["void ", "public ", "private ", "protected ", "class "],
            "go": ["func "],
            "kotlin": ["fun ", "class "],
            "ruby": ["def ", "class "],
            "rust": ["fn ", "struct ", "impl "],
            "cobol": ["SECTION.", "DIVISION."],
            "rpg": ["DCL-PROC ", "BEGSR "],
            "natural": ["DEFINE SUBROUTINE ", "DEFINE FUNCTION ", "1NEXT "],
            "focus": ["-DEFINE FUNCTION ", "-DEFINE FILE "],
        }
        return kw_map.get(language, ["function ", "procedure ", "def ", "sub "])

    async def _refine_profile(
        self,
        profile: LanguageProfile,
        missed_lines: list[str],
    ) -> LanguageProfile:
        """Send missed lines back to LLM for pattern refinement."""
        missed_text = "\n".join(missed_lines)

        user_msg = REFINEMENT_PROMPT.format(missed_lines=missed_text)
        user_msg += "\n\nCURRENT PROFILE:\n" + profile.model_dump_json(indent=2)

        try:
            llm = _create_llm()
            from langchain_core.messages import HumanMessage, SystemMessage

            resp = await llm.ainvoke([
                SystemMessage(content=PROFILE_GENERATION_PROMPT),
                HumanMessage(content=user_msg),
            ])
            raw = resp.content if hasattr(resp, "content") else str(resp)
            data = _parse_json_object(raw)
            refined = self._dict_to_profile(
                data, profile.language, profile.file_extensions,
            )
            refined.codebase_hash = profile.codebase_hash
            refined.source_file_count = profile.source_file_count
            return refined

        except Exception as exc:
            logger.warning("Refinement LLM call failed: %s — keeping current profile", exc)
            return profile

    # ------------------------------------------------------------------
    # Phase 5: Persistence
    # ------------------------------------------------------------------

    def _persist_profile(self, profile: LanguageProfile) -> None:
        """Save profile as JSON to .trustbot/profiles/{language}.json."""
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        out = self._profiles_dir / f"{profile.language}.json"
        out.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Persisted profile to %s", out)

    def _load_cached_profile(
        self, language: str, expected_hash: str,
    ) -> LanguageProfile | None:
        """Load a cached profile if it exists and its hash matches."""
        path = self._profiles_dir / f"{language}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profile = LanguageProfile(**data)
            if profile.codebase_hash == expected_hash:
                return profile
            logger.info(
                "Cache miss for %s: hash changed (%s != %s)",
                language, profile.codebase_hash[:12], expected_hash[:12],
            )
        except Exception as exc:
            logger.warning("Failed to load cached profile for %s: %s", language, exc)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_hash(self, file_list: list[Path]) -> str:
        """Compute a deterministic hash of file paths + sizes for cache keys."""
        items = []
        for f in sorted(file_list):
            try:
                items.append(f"{f.relative_to(self._root)}:{f.stat().st_size}")
            except OSError:
                items.append(str(f.relative_to(self._root)))
        return hashlib.md5("\n".join(items).encode()).hexdigest()

    def _build_seed_profile(
        self, language: str, extensions: list[str],
    ) -> LanguageProfile:
        """Build a minimal seed profile from hardcoded defaults as LLM fallback."""
        from trustbot.agents.agent0_seed_profiles import get_seed_profile

        seed = get_seed_profile(language)
        if seed is not None:
            seed.file_extensions = extensions or seed.file_extensions
            return seed

        return LanguageProfile(
            language=language,
            file_extensions=extensions,
            function_def_patterns=[
                r"^\s*(?:async\s+)?(?:def|function|procedure|func|fun|sub)\s+(?P<name>\w+)",
            ],
            named_regex_groups={"name": "name"},
            single_line_comment="//",
        )
