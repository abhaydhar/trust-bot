"""
Scope-aware structural chunker for languages without tree-sitter AST support.

Unlike the regex chunker (which finds definition lines and splits "this definition
to the next"), this chunker understands block boundaries -- matching open/close
markers like DCL-PROC/END-PROC, BEGSR/ENDSR, DEFINE SUBROUTINE/END-DEFINE, etc.

This produces structurally correct chunks that never split mid-block, similar to
what a proper AST parser would do, but built from explicit block-boundary rules.

Supported languages: RPG/RPGLE, FOCUS, Natural (SAG/ADABAS).
Easily extensible to any language with identifiable block delimiters.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

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


# ── Language block rules ─────────────────────────────────────────────────

_RPG_RULES: list[_BlockRule] = [
    _BlockRule(
        block_type="procedure",
        open_pattern=re.compile(
            r"^\s*DCL-PROC\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END-PROC\b[^;\n]*;?", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="subroutine",
        open_pattern=re.compile(
            r"^\s*BEGSR\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*ENDSR\b[^;\n]*;?", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="data_structure",
        open_pattern=re.compile(
            r"^\s*DCL-DS\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END-DS\b[^;\n]*;?", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="interface",
        open_pattern=re.compile(
            r"^\s*DCL-PI\s+(?P<name>\w+|\*N)", re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END-PI\b[^;\n]*;?", re.MULTILINE | re.IGNORECASE,
        ),
    ),
]

_FOCUS_RULES: list[_BlockRule] = [
    _BlockRule(
        block_type="procedure",
        open_pattern=re.compile(
            r"^-\s*DEFINE\s+(?:FUNCTION|FILE)\s+(?P<name>\w+)",
            re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^-\s*END\b", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="table_request",
        open_pattern=re.compile(
            r"^\s*TABLE\s+FILE\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END\b", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="graph",
        open_pattern=re.compile(
            r"^\s*GRAPH\s+FILE\s+(?P<name>\w+)", re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END\b", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="if_block",
        open_pattern=re.compile(
            r"^-\s*IF\s+(?P<name>.+)", re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^-\s*ENDIF\b", re.MULTILINE | re.IGNORECASE,
        ),
    ),
]

_NATURAL_RULES: list[_BlockRule] = [
    _BlockRule(
        block_type="subroutine",
        open_pattern=re.compile(
            r"^\s*DEFINE\s+SUBROUTINE\s+(?P<name>\w[\w\-]*)",
            re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END-SUBROUTINE\b", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="function",
        open_pattern=re.compile(
            r"^\s*DEFINE\s+FUNCTION\s+(?P<name>\w[\w\-]*)",
            re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END-FUNCTION\b", re.MULTILINE | re.IGNORECASE,
        ),
    ),
    _BlockRule(
        block_type="class",
        open_pattern=re.compile(
            r"^\s*DEFINE\s+CLASS\s+(?P<name>\w[\w\-]*)",
            re.MULTILINE | re.IGNORECASE,
        ),
        close_pattern=re.compile(
            r"^\s*END-CLASS\b", re.MULTILINE | re.IGNORECASE,
        ),
    ),
]

LANGUAGE_RULES: dict[str, list[_BlockRule]] = {
    "rpg": _RPG_RULES,
    "rpgle": _RPG_RULES,
    "focus": _FOCUS_RULES,
    "natural": _NATURAL_RULES,
}


def _line_of(text: str, char_offset: int) -> int:
    """Return 1-based line number for a character offset."""
    return text[:char_offset].count("\n") + 1


def structural_chunk(
    code: str,
    language: str,
    chunk_size: int = 2048,
) -> list[StructuralChunk]:
    """
    Parse *code* using block-boundary rules for *language* and return
    structurally meaningful chunks.

    Algorithm:
    1. Scan for all block open/close markers and record their positions.
    2. Match each opener with its nearest following closer of the same rule.
    3. Extract matched blocks as individual chunks.
    4. Collect any remaining code between blocks as "preamble" / "interstitial"
       chunks (declarations, control flow, comments outside blocks).
    5. If a chunk exceeds *chunk_size* characters, split it at line boundaries
       while keeping it as whole as possible.
    """
    rules = LANGUAGE_RULES.get(language.lower(), [])
    if not rules:
        return [
            StructuralChunk(
                text=code, start_index=0, end_index=len(code),
                token_count=len(code), block_type="file",
                block_name="<unsupported>", language=language,
                line_start=1, line_end=code.count("\n") + 1,
            )
        ]

    # Collect all block spans: (start_char, end_char, block_type, block_name)
    blocks: list[tuple[int, int, str, str]] = []

    for rule in rules:
        openers = list(rule.open_pattern.finditer(code))
        closers = list(rule.close_pattern.finditer(code))

        ci = 0
        for opener in openers:
            name = opener.group(rule.name_group)
            open_start = opener.start()
            # Find the next closer that appears after this opener
            while ci < len(closers) and closers[ci].start() <= open_start:
                ci += 1
            if ci < len(closers):
                close_end = closers[ci].end()
                blocks.append((open_start, close_end, rule.block_type, name))
                ci += 1
            else:
                # No closer found -- take to end of code
                blocks.append((open_start, len(code), rule.block_type, name))

    blocks.sort(key=lambda b: b[0])

    # Remove overlapping blocks (keep the first / outermost)
    merged: list[tuple[int, int, str, str]] = []
    for block in blocks:
        if merged and block[0] < merged[-1][1]:
            continue
        merged.append(block)

    chunks: list[StructuralChunk] = []

    # Walk through code, emitting interstitial + block chunks
    pos = 0
    for start, end, btype, bname in merged:
        # Interstitial code before this block (skip trivial whitespace/punctuation)
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

    # Trailing code after last block (skip trivial remnants)
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

    # Split oversized chunks at line boundaries
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
    return sorted(LANGUAGE_RULES.keys())
