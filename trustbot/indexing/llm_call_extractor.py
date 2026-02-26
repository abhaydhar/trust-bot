"""
LLM-based call extraction — replaces regex-based call detection.

Sends each code chunk to an LLM with a structured prompt and a list of known
function names.  The LLM returns actual calls found in the code, avoiding
false positives from variable declarations, type references, and uses clauses.

Falls back to regex Strategy 1 (parenthesised calls) when the LLM is
unavailable or returns an error.

Language-specific prompts, skip-tokens, and bare-identifier rules are read
from the active LanguageProfile (set by Agent 0).  If no profile is loaded,
seed/fallback profiles are used transparently.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from trustbot.indexing.chunker import CodeChunk

logger = logging.getLogger("trustbot.indexing.llm_call_extractor")

_PROMPT_VERSION = "v7-strip-noncode-preamble"

# ---------------------------------------------------------------------------
# Base system prompt (language-agnostic rules)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_BASE = """\
You are a precise static-code-analysis engine.  Given a code chunk and a list
of known project functions, identify every function / procedure / method CALL
made inside the chunk.

RULES — follow them strictly:
1. Only report actual calls (procedure invocations, function calls, method calls).
2. Do NOT report:
   - variable or field declarations
   - type / class references
   - module or unit imports
   - class inheritance or interface declarations
   - the function's own name (self-reference from its declaration line)
3. Only report callees whose name appears in the KNOWN FUNCTIONS list.
4. If the chunk contains zero calls, return an empty array.

Return ONLY a JSON array — no markdown fences, no commentary:
[{"callee": "ExactFunctionName", "confidence": 0.95}]
"""

CHUNK_TEMPLATE = """\
LANGUAGE: {language}
FILE: {file_path}
FUNCTION: {function_name}

KNOWN FUNCTIONS in this project:
{known_functions}

CODE CHUNK:
```
{content}
```"""

MAX_KNOWN_FUNCTIONS_IN_PROMPT = 200
MAX_CHUNK_CHARS = 6000
BATCH_SIZE = 5


# ---------------------------------------------------------------------------
# Profile-driven helpers
# ---------------------------------------------------------------------------

def _get_profile(language: str):
    """Retrieve the active LanguageProfile for a language (lazy-loads seeds)."""
    from trustbot.indexing.chunker import _get_profile as _chunker_get_profile
    return _chunker_get_profile(language)


def _get_system_prompt(language: str) -> str:
    """Build the full system prompt: base rules + language-specific addendum."""
    profile = _get_profile(language)
    if profile and profile.llm_call_prompt:
        return SYSTEM_PROMPT_BASE + profile.llm_call_prompt
    return SYSTEM_PROMPT_BASE


def _get_skip_tokens(language: str) -> frozenset[str]:
    """Return the set of skip tokens for a language."""
    profile = _get_profile(language)
    if profile and profile.skip_tokens:
        return frozenset(t.upper() for t in profile.skip_tokens)
    return frozenset()


def _supports_bare_identifiers(language: str) -> bool:
    """Check whether the language supports bare-identifier calls."""
    profile = _get_profile(language)
    return bool(profile and profile.supports_bare_identifiers)


def _get_bare_id_lookahead(language: str) -> str:
    """Return the negative lookahead regex for bare-identifier matching."""
    profile = _get_profile(language)
    if profile and profile.bare_id_negative_lookahead:
        return profile.bare_id_negative_lookahead
    return ""


# ---------------------------------------------------------------------------
# LLM / parsing helpers
# ---------------------------------------------------------------------------

def _create_llm():
    from trustbot.config import settings

    try:
        from langchain_litellm import ChatLiteLLM
    except ImportError:
        from langchain_community.chat_models import ChatLiteLLM

    kwargs: dict[str, Any] = {
        "model": settings.litellm_model,
        "temperature": 0.0,
        "max_tokens": 1024,
    }
    if settings.litellm_api_base:
        kwargs["api_base"] = settings.litellm_api_base
    if settings.litellm_api_key:
        kwargs["api_key"] = settings.litellm_api_key

    return ChatLiteLLM(**kwargs)


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _parse_json_array(content: str) -> list[dict]:
    text = content.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1]
        text = text.split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1]
        text = text.split("```", 1)[0]
    text = text.strip()
    result = json.loads(text)
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "calls" in result:
        return result["calls"]
    return []


# ---------------------------------------------------------------------------
# Content stripping for identifier scanning (generic, profile-driven)
# ---------------------------------------------------------------------------


def _strip_non_code_content(content: str, language: str) -> str:
    """Strip string literals, comments, and the declaration line from *content*.

    Returns text suitable for bare-identifier / parenthesised-call scanning
    where names inside strings and comments must not produce false matches.
    Fully profile-driven — works for any language without hardcoded rules.
    """
    profile = _get_profile(language)
    if not profile:
        return content

    lines = content.splitlines(keepends=True)
    if lines:
        lines[0] = "\n"
    text = "".join(lines)

    sl = profile.single_line_comment
    if sl:
        text = re.sub(re.escape(sl) + r".*$", "", text, flags=re.MULTILINE)

    ml_open = profile.multi_line_comment_open
    ml_close = profile.multi_line_comment_close
    if ml_open and ml_close and ml_open != sl:
        text = re.sub(
            re.escape(ml_open) + r"[\s\S]*?" + re.escape(ml_close),
            " ",
            text,
        )

    for delim in (profile.string_delimiters or []):
        esc = re.escape(delim)
        if len(delim) == 1:
            text = re.sub(esc + r"[^" + delim + r"]*" + esc, delim * 2, text)
        else:
            text = re.sub(esc + r"[\s\S]*?" + esc, delim * 2, text)

    return text


# ---------------------------------------------------------------------------
# Regex fallback
# ---------------------------------------------------------------------------

def _get_call_keyword_patterns(language: str) -> list[re.Pattern]:
    """Return compiled call-keyword patterns from the profile (e.g. FETCH 'name')."""
    profile = _get_profile(language)
    if not profile or not profile.call_keyword_patterns:
        return []
    compiled = []
    for pat_str in profile.call_keyword_patterns:
        try:
            compiled.append(re.compile(pat_str, re.IGNORECASE))
        except re.error:
            pass
    return compiled


def _regex_fallback(
    chunk: CodeChunk,
    known_upper: set[str],
    dfm_names: frozenset[str] = frozenset(),
) -> list[dict]:
    """Regex fallback — parenthesised calls, call keywords, and bare-identifier matching."""
    results = []
    seen: set[str] = set()
    if not chunk.content:
        return results

    func_upper_self = (chunk.function_name or "").upper()
    skip = _get_skip_tokens(chunk.language)

    # Content with strings, comments, and the declaration/header line removed.
    # Prevents matching identifiers that only appear inside string literals,
    # comments, or the function's own signature.  Profile-driven.
    clean = _strip_non_code_content(chunk.content, chunk.language)

    # Strategy A: parenthesised calls (all languages) — scans cleaned content
    paren_pattern = re.compile(r"\b(?P<callee>[A-Za-z_]\w*)\s*\(")
    for m in paren_pattern.finditer(clean):
        callee = m.group("callee")
        upper = callee.upper()
        if upper in known_upper and upper not in seen:
            if upper == func_upper_self:
                paren_count = len(re.findall(
                    r"\b" + re.escape(upper) + r"\s*\(",
                    clean, re.IGNORECASE,
                ))
                if paren_count > 1:
                    seen.add(upper)
                    results.append({"callee": callee, "confidence": 0.70})
            elif upper not in dfm_names:
                seen.add(upper)
                results.append({"callee": callee, "confidence": 0.70})

    # Strategy A2: call-keyword patterns (e.g. FETCH 'name', CALLNAT 'name')
    # Scans ORIGINAL content — some languages embed call targets in strings.
    # Self-references are permitted: a call keyword always denotes a genuine call.
    for kw_pat in _get_call_keyword_patterns(chunk.language):
        for m in kw_pat.finditer(chunk.content):
            callee = m.group("callee")
            upper = callee.upper()
            if upper in known_upper and upper not in seen:
                seen.add(upper)
                results.append({"callee": callee, "confidence": 0.80})

    # Strategy B: bare-identifier matching (profile-driven) — scans cleaned content
    if _supports_bare_identifiers(chunk.language):
        lookahead = _get_bare_id_lookahead(chunk.language)
        for func_upper in known_upper:
            if func_upper in seen:
                continue
            if func_upper == func_upper_self:
                name_hits = len(re.findall(
                    r"\b" + re.escape(func_upper) + r"\b",
                    clean, re.IGNORECASE,
                ))
                if name_hits <= 1:
                    continue
            if func_upper in skip:
                continue
            if func_upper in dfm_names:
                continue
            if len(func_upper) < 3:
                continue
            bare_regex = r"\b" + re.escape(func_upper) + r"\b"
            if lookahead:
                bare_regex += lookahead
            bare_pat = re.compile(bare_regex, re.IGNORECASE)
            if bare_pat.search(clean):
                seen.add(func_upper)
                results.append({"callee": func_upper, "confidence": 0.60})

    return results


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

async def extract_calls_llm(
    chunks: list[CodeChunk],
    known_function_names: list[str],
    cache_conn: sqlite3.Connection | None = None,
) -> list[tuple[str, str, float]]:
    """Extract call edges from code chunks using an LLM.

    Returns (caller_chunk_id, callee_name, confidence) tuples.
    """
    from trustbot.config import settings

    known_upper: set[str] = {n.upper() for n in known_function_names}
    known_display = known_function_names[:MAX_KNOWN_FUNCTIONS_IN_PROMPT]
    known_str = ", ".join(known_display)
    if len(known_function_names) > MAX_KNOWN_FUNCTIONS_IN_PROMPT:
        known_str += f" ... and {len(known_function_names) - MAX_KNOWN_FUNCTIONS_IN_PROMPT} more"

    dfm_names: frozenset[str] = frozenset(
        c.function_name.upper()
        for c in chunks
        if c.metadata.get("is_dfm_form") and c.function_name
    )

    seen_chunk_ids: set[str] = set()
    code_chunks: list[CodeChunk] = []
    for c in chunks:
        if (c.function_name
                and c.function_name != "<module>"
                and c.content
                and not c.metadata.get("is_dfm_form")
                and c.chunk_id not in seen_chunk_ids):
            seen_chunk_ids.add(c.chunk_id)
            code_chunks.append(c)

    if not code_chunks:
        return []

    all_edges: list[tuple[str, str, float]] = []
    semaphore = asyncio.Semaphore(settings.max_concurrent_llm_calls)
    llm = None

    cached = 0
    llm_called = 0
    fallback_used = 0

    async def _process_chunk(chunk: CodeChunk):
        nonlocal cached, llm_called, fallback_used, llm

        content_h = _content_hash(chunk.content + _PROMPT_VERSION + chunk.language)

        if cache_conn:
            try:
                row = cache_conn.execute(
                    "SELECT result_json FROM llm_call_cache WHERE content_hash = ?",
                    (content_h,),
                ).fetchone()
                if row:
                    calls = json.loads(row[0] if isinstance(row, tuple) else row["result_json"])
                    cached += 1
                    edges = _calls_to_edges(chunk, calls, known_upper)
                    edges = _supplement_bare_identifiers(chunk, edges, known_upper, dfm_names)
                    return _expand_call_sites(chunk, edges)
            except Exception:
                pass

        chunk_content = chunk.content
        if len(chunk_content) > MAX_CHUNK_CHARS:
            chunk_content = chunk_content[:MAX_CHUNK_CHARS] + "\n... (truncated)"

        user_msg = CHUNK_TEMPLATE.format(
            language=chunk.language,
            file_path=chunk.file_path,
            function_name=chunk.function_name or "unknown",
            known_functions=known_str,
            content=chunk_content,
        )

        async with semaphore:
            try:
                if llm is None:
                    llm = _create_llm()

                from langchain_core.messages import HumanMessage, SystemMessage
                sys_prompt = _get_system_prompt(chunk.language)
                resp = await llm.ainvoke([
                    SystemMessage(content=sys_prompt),
                    HumanMessage(content=user_msg),
                ])
                raw = resp.content if hasattr(resp, "content") else str(resp)
                calls = _parse_json_array(raw)
                llm_called += 1

                if cache_conn:
                    try:
                        cache_conn.execute(
                            "INSERT OR REPLACE INTO llm_call_cache "
                            "(content_hash, result_json, model, created_at) "
                            "VALUES (?, ?, ?, ?)",
                            (content_h, json.dumps(calls),
                             settings.litellm_model,
                             datetime.now(timezone.utc).isoformat()),
                        )
                        cache_conn.commit()
                    except Exception:
                        pass

                edges = _calls_to_edges(chunk, calls, known_upper)
                edges = _supplement_bare_identifiers(chunk, edges, known_upper, dfm_names)
                return _expand_call_sites(chunk, edges)

            except Exception as exc:
                logger.warning(
                    "LLM extraction failed for %s::%s, falling back to regex: %s",
                    chunk.file_path, chunk.function_name, exc,
                )
                fallback_used += 1
                fallback_calls = _regex_fallback(chunk, known_upper, dfm_names)
                edges = _calls_to_edges(chunk, fallback_calls, known_upper)
                edges = _supplement_bare_identifiers(chunk, edges, known_upper, dfm_names)
                return _expand_call_sites(chunk, edges)

    tasks = [_process_chunk(c) for c in code_chunks]
    results = await asyncio.gather(*tasks)

    for edge_list in results:
        all_edges.extend(edge_list)

    logger.info(
        "LLM call extraction: %d chunks processed "
        "(%d cached, %d LLM calls, %d regex fallback) → %d edges",
        len(code_chunks), cached, llm_called, fallback_used, len(all_edges),
    )
    return all_edges


# ---------------------------------------------------------------------------
# Post-processing helpers
# ---------------------------------------------------------------------------

def _supplement_bare_identifiers(
    chunk: CodeChunk,
    existing_edges: list[tuple[str, str, float]],
    known_upper: set[str],
    dfm_names: frozenset[str] = frozenset(),
) -> list[tuple[str, str, float]]:
    """Catch known function names the LLM missed by scanning chunk content."""
    if not chunk.content:
        return existing_edges

    if not _supports_bare_identifiers(chunk.language):
        return existing_edges

    already = {e[1].upper() for e in existing_edges}
    func_upper_self = (chunk.function_name or "").upper()
    supplemented = list(existing_edges)
    skip = _get_skip_tokens(chunk.language)
    lookahead = _get_bare_id_lookahead(chunk.language)
    clean = _strip_non_code_content(chunk.content, chunk.language)

    for func_upper in known_upper:
        if func_upper in already:
            continue
        if func_upper == func_upper_self:
            name_hits = len(re.findall(
                r"\b" + re.escape(func_upper) + r"\b",
                clean, re.IGNORECASE,
            ))
            if name_hits <= 1:
                continue
        if func_upper in skip:
            continue
        if func_upper in dfm_names:
            continue
        if len(func_upper) < 3:
            continue
        bare_regex = r"\b" + re.escape(func_upper) + r"\b"
        if lookahead:
            bare_regex += lookahead
        pat = re.compile(bare_regex, re.IGNORECASE)
        if pat.search(clean):
            supplemented.append((chunk.chunk_id, func_upper, 0.55))
            already.add(func_upper)

    if len(supplemented) > len(existing_edges):
        logger.debug(
            "Supplemented %d bare-identifier edges for %s::%s",
            len(supplemented) - len(existing_edges),
            chunk.file_path, chunk.function_name,
        )
    return supplemented


def _expand_call_sites(
    chunk: CodeChunk,
    edges: list[tuple[str, str, float]],
) -> list[tuple[str, str, float]]:
    """Expand unique edges to per-call-site edges."""
    if not chunk.content:
        return edges

    content = chunk.content
    func_upper_self = (chunk.function_name or "").upper()
    expanded: list[tuple[str, str, float]] = []

    for caller_id, callee_name, confidence in edges:
        upper = callee_name.upper()

        paren_count = len(re.findall(
            r"\b" + re.escape(upper) + r"\s*\(", content, re.IGNORECASE,
        ))

        if upper == func_upper_self and paren_count > 0:
            paren_count -= 1

        if paren_count >= 2:
            for _ in range(paren_count):
                expanded.append((caller_id, callee_name, confidence))
        elif paren_count == 0:
            bare_count = len(re.findall(
                r"\b" + re.escape(upper) + r"\b", content, re.IGNORECASE,
            ))
            if upper == func_upper_self:
                bare_count -= 1
            for _ in range(max(bare_count, 1)):
                expanded.append((caller_id, callee_name, confidence))
        else:
            expanded.append((caller_id, callee_name, confidence))

    return expanded


def _calls_to_edges(
    chunk: CodeChunk,
    calls: list[dict],
    known_upper: set[str],
) -> list[tuple[str, str, float]]:
    """Validate LLM output and convert to (caller_chunk_id, callee_name, confidence) tuples."""
    edges: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    content_upper = (chunk.content or "").upper()
    func_upper_self = (chunk.function_name or "").upper()
    for call in calls:
        callee = call.get("callee", "").strip()
        if not callee:
            continue
        upper = callee.upper()
        if upper not in known_upper:
            continue
        if upper in seen:
            continue
        if upper == func_upper_self:
            name_occurrences = len(re.findall(
                r"\b" + re.escape(upper) + r"\b", content_upper,
            ))
            if name_occurrences <= 1:
                continue
        if not re.search(r"\b" + re.escape(upper) + r"\b", content_upper):
            logger.debug(
                "Rejected hallucinated call %s -> %s (not in chunk content)",
                chunk.function_name, callee,
            )
            continue
        seen.add(upper)
        confidence = min(max(float(call.get("confidence", 0.85)), 0.0), 1.0)
        edges.append((chunk.chunk_id, callee, confidence))
    return edges
