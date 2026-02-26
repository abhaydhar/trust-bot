"""
Scope-aware structural chunker for languages with identifiable block delimiters.

Unlike the regex chunker (which finds definition lines and splits "this definition
to the next"), this chunker understands block boundaries — matching open/close
markers like DCL-PROC/END-PROC, BEGSR/ENDSR, DEFINE SUBROUTINE/END-DEFINE, etc.

Block rules are now read from LanguageProfile objects (set by Agent 0).
If no profile is loaded, seed profiles are used transparently as fallback.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("trustbot.indexing.structural_chunker")


@dataclass
class StructuralChunk:
    """A structurally-delimited code chunk."""

    text: str
    start_index: int
    end_index: int
    token_count: int
    block_type: str
    block_name: str
    language: str
    line_start: int
    line_end: int


@dataclass
class _BlockRule:
    """Defines how to recognise one kind of block in a language."""

    block_type: str
    open_pattern: re.Pattern
    close_pattern: re.Pattern
    name_group: str = "name"


def _line_of(text: str, char_offset: int) -> int:
    """Return 1-based line number for a character offset."""
    return text[:char_offset].count("\n") + 1


def _get_block_rules(language: str) -> list[_BlockRule]:
    """Get compiled block rules from the active language profile."""
    from trustbot.indexing.chunker import _get_profile

    profile = _get_profile(language)
    if not profile or not profile.block_rules:
        return []

    rules: list[_BlockRule] = []
    for br in profile.block_rules:
        try:
            rules.append(_BlockRule(
                block_type=br.block_type,
                open_pattern=re.compile(br.open_pattern, re.MULTILINE | re.IGNORECASE),
                close_pattern=re.compile(br.close_pattern, re.MULTILINE | re.IGNORECASE),
                name_group=br.name_group or "name",
            ))
        except re.error as e:
            logger.warning(
                "Invalid block rule regex for %s/%s: %s",
                language, br.block_type, e,
            )
    return rules


def structural_chunk(
    code: str,
    language: str,
    chunk_size: int = 2048,
) -> list[StructuralChunk]:
    """Parse *code* using block-boundary rules for *language*.

    Algorithm:
    1. Scan for all block open/close markers and record their positions.
    2. Match each opener with its nearest following closer of the same rule.
    3. Extract matched blocks as individual chunks.
    4. Collect any remaining code between blocks as "preamble" / "interstitial".
    5. Split oversized chunks at line boundaries.
    """
    rules = _get_block_rules(language.lower())
    if not rules:
        return [
            StructuralChunk(
                text=code, start_index=0, end_index=len(code),
                token_count=len(code), block_type="file",
                block_name="<unsupported>", language=language,
                line_start=1, line_end=code.count("\n") + 1,
            )
        ]

    blocks: list[tuple[int, int, str, str]] = []

    for rule in rules:
        openers = list(rule.open_pattern.finditer(code))
        closers = list(rule.close_pattern.finditer(code))

        ci = 0
        for opener in openers:
            name = opener.group(rule.name_group)
            open_start = opener.start()
            while ci < len(closers) and closers[ci].start() <= open_start:
                ci += 1
            if ci < len(closers):
                close_end = closers[ci].end()
                blocks.append((open_start, close_end, rule.block_type, name))
                ci += 1
            else:
                blocks.append((open_start, len(code), rule.block_type, name))

    blocks.sort(key=lambda b: b[0])

    merged: list[tuple[int, int, str, str]] = []
    for block in blocks:
        if merged and block[0] < merged[-1][1]:
            continue
        merged.append(block)

    chunks: list[StructuralChunk] = []

    pos = 0
    for start, end, btype, bname in merged:
        if start > pos:
            inter = code[pos:start].strip()
            if len(inter) > 3:
                chunks.append(StructuralChunk(
                    text=inter, start_index=pos, end_index=start,
                    token_count=len(inter), block_type="preamble",
                    block_name="<declarations>", language=language,
                    line_start=_line_of(code, pos),
                    line_end=_line_of(code, start - 1),
                ))

        block_text = code[start:end]
        chunks.append(StructuralChunk(
            text=block_text, start_index=start, end_index=end,
            token_count=len(block_text), block_type=btype,
            block_name=bname, language=language,
            line_start=_line_of(code, start),
            line_end=_line_of(code, end),
        ))
        pos = end

    if pos < len(code):
        tail = code[pos:].strip()
        if len(tail) > 3:
            chunks.append(StructuralChunk(
                text=tail, start_index=pos, end_index=len(code),
                token_count=len(tail), block_type="epilogue",
                block_name="<trailing>", language=language,
                line_start=_line_of(code, pos),
                line_end=_line_of(code, len(code) - 1),
            ))

    final: list[StructuralChunk] = []
    for chunk in chunks:
        if chunk.token_count <= chunk_size:
            final.append(chunk)
            continue
        lines = chunk.text.split("\n")
        buf: list[str] = []
        buf_start = chunk.start_index
        cur_offset = chunk.start_index
        for line in lines:
            if buf and (sum(len(l) for l in buf) + len(buf) + len(line)) > chunk_size:
                text = "\n".join(buf)
                final.append(StructuralChunk(
                    text=text, start_index=buf_start,
                    end_index=buf_start + len(text),
                    token_count=len(text), block_type=chunk.block_type,
                    block_name=chunk.block_name, language=chunk.language,
                    line_start=_line_of(code, buf_start),
                    line_end=_line_of(code, buf_start + len(text)),
                ))
                buf = []
                buf_start = cur_offset
            buf.append(line)
            cur_offset += len(line) + 1
        if buf:
            text = "\n".join(buf)
            final.append(StructuralChunk(
                text=text, start_index=buf_start,
                end_index=buf_start + len(text),
                token_count=len(text), block_type=chunk.block_type,
                block_name=chunk.block_name, language=chunk.language,
                line_start=_line_of(code, buf_start),
                line_end=_line_of(code, buf_start + len(text)),
            ))

    logger.info(
        "Structural chunker (%s): %d blocks found, %d chunks produced",
        language, len(merged), len(final),
    )
    return final


def get_supported_languages() -> list[str]:
    """Return list of languages that have structural block rules."""
    from trustbot.indexing.chunker import _ensure_profiles_loaded, _active_profiles

    _ensure_profiles_loaded()
    return sorted(
        lang for lang, profile in _active_profiles.items()
        if profile.block_rules
    )
