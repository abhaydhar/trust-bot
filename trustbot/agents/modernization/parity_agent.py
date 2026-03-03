"""
Agent 7: Parity Verification Agent

Extracts a business logic inventory from the legacy codebase, searches
the new codebase for migrated equivalents, and generates a coverage
matrix showing migrated/partial/missing status for each item.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from pathlib import Path, PurePosixPath

import litellm

from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.models.modernization import (
    ModernizationConfig,
    ParityItem,
    ParityReport,
    ParityStatus,
    Phase1Result,
    Phase2Result,
    TestResult,
)

logger = logging.getLogger("trustbot.agents.modernization.parity")


class ParityAgent:
    """Parity Verification Agent -- ensures all business logic is migrated."""

    def __init__(self, code_index: CodeIndex) -> None:
        self._index = code_index

    async def run(
        self,
        phase1: Phase1Result,
        phase2: Phase2Result,
        tests: TestResult,
        config: ModernizationConfig,
        progress_callback=None,
    ) -> ParityReport:
        if progress_callback:
            progress_callback(0.0, "Extracting business logic inventory from legacy...")

        legacy_items = await self._extract_legacy_inventory(phase1, config)

        if progress_callback:
            progress_callback(0.3, "Scanning new codebase for migrated equivalents...")

        new_codebase_index = self._index_new_codebase(config)

        if progress_callback:
            progress_callback(0.5, "Matching legacy items to new codebase...")

        parity_items = await self._check_parity(
            legacy_items, new_codebase_index, config
        )

        if progress_callback:
            progress_callback(0.8, "Generating parity report...")

        migrated = sum(1 for p in parity_items if p.status == ParityStatus.MIGRATED)
        partial = sum(1 for p in parity_items if p.status == ParityStatus.PARTIAL)
        missing = sum(1 for p in parity_items if p.status == ParityStatus.MISSING)
        total = len(parity_items)

        markdown = self._generate_report_markdown(parity_items, migrated, partial, missing)

        return ParityReport(
            items=parity_items,
            total_items=total,
            migrated_count=migrated,
            partial_count=partial,
            missing_count=missing,
            coverage_pct=(migrated / total * 100) if total > 0 else 0.0,
            markdown_document=markdown,
        )

    async def _extract_legacy_inventory(
        self, phase1: Phase1Result, config: ModernizationConfig,
    ) -> list[dict]:
        """Extract business logic items from the legacy codebase."""
        conn = self._index.get_cache_conn()

        rows = conn.execute(
            "SELECT function_name, file_path, class_name FROM code_index"
        ).fetchall()

        business_items = []
        for r in rows:
            func, fp, cls = r[0], r[1], r[2] or ""
            layer = "unknown"
            for m in phase1.architecture.layer_mappings:
                if m.file_path == fp:
                    layer = m.layer.value
                    break

            if layer in ("business_logic", "data_access", "shared"):
                business_items.append({
                    "function": func,
                    "file": fp,
                    "class": cls,
                    "layer": layer,
                })

        if len(business_items) > 200:
            business_items = business_items[:200]

        return business_items

    _LANG_KEYWORDS = frozenset({
        "EXPORT", "FUNCTION", "ASYNC", "CONST", "LET", "VAR", "CLASS",
        "INTERFACE", "ENUM", "TYPE", "ABSTRACT", "STATIC", "READONLY",
        "OVERRIDE", "VIRTUAL", "SEALED", "PARTIAL", "VOID", "BOOL",
        "INT", "STRING", "FLOAT", "DOUBLE", "LONG", "BYTE", "CHAR",
        "OBJECT", "DYNAMIC", "DECIMAL", "TASK", "DEF", "SELF", "RETURN",
        "PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL", "NEW", "STRUCT",
        "NAMESPACE", "USING", "IMPORT", "FROM", "EXTENDS", "IMPLEMENTS",
    })

    def _index_new_codebase(self, config: ModernizationConfig) -> dict[str, list[str]]:
        """Build a function/class-name-to-file-paths index of the new codebase."""
        output_dir = Path(config.output_directory)
        index: dict[str, list[str]] = defaultdict(list)

        decl_prefixes = (
            "export function ", "export const ", "export async function ",
            "export default function ", "export class ", "export interface ",
            "function ", "async function ", "def ", "async def ", "class ",
            "public ", "private ", "protected ", "internal ", "interface ",
        )

        for root_dir, dirs, files in os.walk(output_dir):
            for filename in files:
                ext = PurePosixPath(filename).suffix.lower()
                if ext not in (".ts", ".tsx", ".cs", ".py", ".js", ".jsx"):
                    continue
                full_path = Path(root_dir) / filename
                try:
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                rel = str(full_path.relative_to(output_dir))
                for line in content.splitlines():
                    stripped = line.strip()
                    if not any(stripped.startswith(kw) for kw in decl_prefixes):
                        continue
                    for tok in stripped.split():
                        tok_clean = tok.split("(")[0].split(":")[0].split("<")[0].split("{")[0].strip()
                        if (
                            tok_clean
                            and len(tok_clean) > 2
                            and tok_clean[0].isalpha()
                            and tok_clean.upper() not in self._LANG_KEYWORDS
                        ):
                            index[tok_clean.upper()].append(rel)
                            break

        return index

    _CODEGEN_SUFFIXES = ("CONTROLLER", "SERVICE", "HANDLER", "PROVIDER", "REPOSITORY", "MANAGER")

    def _fuzzy_match(
        self, name: str, new_index: dict[str, list[str]],
    ) -> list[str] | None:
        """Try exact match, then suffix-stripped match, then substring containment."""
        if name in new_index:
            return new_index[name]

        for suffix in self._CODEGEN_SUFFIXES:
            if (name + suffix) in new_index:
                return new_index[name + suffix]

        for key, paths in new_index.items():
            if name in key or key in name:
                return paths

        return None

    async def _check_parity(
        self,
        legacy_items: list[dict],
        new_index: dict[str, list[str]],
        config: ModernizationConfig,
    ) -> list[ParityItem]:
        """Check each legacy business logic item against the new codebase."""
        parity_items: list[ParityItem] = []
        unmatched: list[dict] = []

        for item in legacy_items:
            func_upper = item["function"].upper()
            bare_name = func_upper.rsplit(".", 1)[-1] if "." in func_upper else func_upper

            matches = self._fuzzy_match(func_upper, new_index)
            if not matches and bare_name != func_upper:
                matches = self._fuzzy_match(bare_name, new_index)

            if matches:
                parity_items.append(ParityItem(
                    legacy_function=item["function"],
                    legacy_file=item["file"],
                    new_function=item["function"],
                    new_file=matches[0],
                    status=ParityStatus.MIGRATED,
                    notes=f"Found in {len(matches)} file(s)",
                ))
            else:
                unmatched.append(item)
                parity_items.append(ParityItem(
                    legacy_function=item["function"],
                    legacy_file=item["file"],
                    status=ParityStatus.MISSING,
                ))

        if unmatched:
            await self._llm_deep_match(unmatched, parity_items, config)

        return parity_items

    async def _llm_deep_match(
        self,
        unmatched: list[dict],
        parity_items: list[ParityItem],
        config: ModernizationConfig,
    ) -> None:
        """Use LLM to find renamed/refactored equivalents -- processes in batches."""
        output_dir = Path(config.output_directory)
        new_files = []
        for root_dir, _, files in os.walk(output_dir):
            for f in files:
                ext = PurePosixPath(f).suffix.lower()
                if ext in (".ts", ".tsx", ".cs", ".py", ".js"):
                    rel = str(Path(root_dir, f).relative_to(output_dir))
                    new_files.append(rel)

        new_files_text = "\n".join(f"- {f}" for f in new_files[:100])
        parity_lookup = {p.legacy_function: p for p in parity_items}
        semaphore = asyncio.Semaphore(settings.max_concurrent_llm_calls)

        batch_size = 40
        batches = [
            unmatched[i : i + batch_size]
            for i in range(0, len(unmatched), batch_size)
        ]

        async def _match_batch(batch: list[dict]) -> None:
            items_text = "\n".join(
                f"- {item['function']} ({item['file']})" for item in batch
            )
            prompt = (
                "The following legacy functions were NOT found by name in the new codebase.\n"
                "Check if they may have been renamed, refactored, or merged.\n\n"
                f"Legacy functions:\n{items_text}\n\n"
                f"New codebase files:\n{new_files_text}\n\n"
                "For each legacy function, respond:\n"
                "LEGACY: <function> | STATUS: <migrated/partial/missing> | "
                "NEW_FILE: <file_path or 'none'> | NOTES: <explanation>\n"
                "Output only these lines."
            )
            try:
                async with semaphore:
                    response = await litellm.acompletion(
                        model=settings.litellm_model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                        max_tokens=3000,
                        **settings.get_litellm_kwargs(),
                    )
                text = response.choices[0].message.content or ""
                self._apply_llm_matches(text, parity_lookup)
            except Exception as e:
                logger.warning("LLM deep match batch failed: %s", str(e)[:200])

        await asyncio.gather(*[_match_batch(b) for b in batches])

    def _apply_llm_matches(
        self, text: str, parity_lookup: dict[str, ParityItem],
    ) -> None:
        """Parse LLM deep-match response and update parity items."""
        for line in text.strip().splitlines():
            if not line.strip().startswith("LEGACY:"):
                continue
            parts = {}
            for segment in line.split("|"):
                segment = segment.strip()
                if ":" in segment:
                    key, val = segment.split(":", 1)
                    parts[key.strip().upper()] = val.strip()

            func_name = parts.get("LEGACY", "")
            status_str = parts.get("STATUS", "missing").lower()
            new_file = parts.get("NEW_FILE", "none")
            notes = parts.get("NOTES", "")

            if func_name in parity_lookup:
                try:
                    parity_lookup[func_name].status = ParityStatus(status_str)
                except ValueError:
                    pass
                if new_file and new_file.lower() != "none":
                    parity_lookup[func_name].new_file = new_file
                if notes:
                    parity_lookup[func_name].notes = notes

    def _generate_report_markdown(
        self,
        items: list[ParityItem],
        migrated: int,
        partial: int,
        missing: int,
    ) -> str:
        total = len(items)
        coverage = (migrated / total * 100) if total > 0 else 0.0

        lines = [
            "# Parity Verification Report",
            "",
            "## Summary",
            f"- **Total business logic items**: {total}",
            f"- **Migrated**: {migrated} ({migrated / total * 100:.1f}%)" if total else "- **Migrated**: 0",
            f"- **Partial**: {partial}",
            f"- **Missing**: {missing}",
            f"- **Overall coverage**: {coverage:.1f}%",
            "",
        ]

        if items:
            lines.extend(["## Coverage Matrix", ""])
            lines.append("| Legacy Function | Legacy File | Status | New File | Notes |")
            lines.append("|----------------|-------------|--------|----------|-------|")
            for item in items[:100]:
                status_icon = {
                    ParityStatus.MIGRATED: "MIGRATED",
                    ParityStatus.PARTIAL: "PARTIAL",
                    ParityStatus.MISSING: "MISSING",
                    ParityStatus.NOT_APPLICABLE: "N/A",
                }.get(item.status, "?")
                lines.append(
                    f"| `{item.legacy_function}` | `{item.legacy_file}` | "
                    f"{status_icon} | {item.new_file or '-'} | {item.notes or '-'} |"
                )
            if len(items) > 100:
                lines.append(f"| ... | +{len(items) - 100} more items | | | |")

        missing_items = [i for i in items if i.status == ParityStatus.MISSING]
        if missing_items:
            lines.extend([
                "",
                "## Missing Items (Action Required)",
                "",
                "These business logic items were not found in the new codebase:",
                "",
            ])
            for item in missing_items[:30]:
                lines.append(f"- `{item.legacy_function}` from `{item.legacy_file}`")
            if len(missing_items) > 30:
                lines.append(f"- ... and {len(missing_items) - 30} more")

        return "\n".join(lines)
