"""
Agent 2: Inventory Analyst

Scans all frontend files (.cshtml, .aspx, .ascx, .razor, etc.), extracts
controls/components, classifies complexity, groups related files into logical
UI components, and suggests a React component hierarchy.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path, PurePosixPath

import litellm

from trustbot.config import settings
from trustbot.prompts import get_prompt
from trustbot.index.code_index import CodeIndex
from trustbot.models.modernization import (
    ArchitectureSpec,
    ComponentSuggestion,
    ComplexityLevel,
    FileInventory,
    FileInventoryItem,
    LayerClassification,
)

logger = logging.getLogger("trustbot.agents.modernization.inventory")

_FRONTEND_EXTENSIONS = {
    ".cshtml", ".aspx", ".ascx", ".razor", ".master",
    ".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte",
    ".css", ".scss", ".less", ".sass", ".js", ".ts",
}

_CODE_BEHIND_EXTENSIONS = {
    ".cshtml.cs", ".aspx.cs", ".ascx.cs", ".aspx.vb",
    ".razor.cs", ".cs", ".vb",
}


def _count_loc(file_path: Path) -> int:
    try:
        return sum(1 for _ in file_path.open(encoding="utf-8", errors="replace"))
    except OSError:
        return 0


def _estimate_complexity(loc: int, function_count: int) -> ComplexityLevel:
    if loc > 1000 or function_count > 20:
        return ComplexityLevel.VERY_HIGH
    if loc > 500 or function_count > 10:
        return ComplexityLevel.HIGH
    if loc > 150 or function_count > 5:
        return ComplexityLevel.MEDIUM
    return ComplexityLevel.LOW


def _has_code_behind(file_path: str, codebase_root: Path) -> bool:
    """Check if the file has an associated code-behind file."""
    for ext in (".cs", ".vb"):
        candidate = codebase_root / (file_path + ext)
        if candidate.exists():
            return True
    return False


def _find_related_files(file_path: str, all_files: set[str]) -> list[str]:
    """Find files related to the given file (code-behind, CSS, JS partials)."""
    stem = PurePosixPath(file_path).stem
    parent = str(PurePosixPath(file_path).parent)
    related = []
    for f in all_files:
        if f == file_path:
            continue
        f_stem = PurePosixPath(f).stem
        f_parent = str(PurePosixPath(f).parent)
        if f_parent == parent and (f_stem == stem or f_stem.startswith(stem + ".") or f.startswith(file_path + ".")):
            related.append(f)
    return related


class InventoryAgent:
    """Inventory Analyst -- catalogs all files and suggests React component mappings."""

    def __init__(self, code_index: CodeIndex) -> None:
        self._index = code_index

    async def run(
        self,
        architecture: ArchitectureSpec,
        config: ModernizationConfig,
        progress_callback=None,
    ) -> FileInventory:
        if progress_callback:
            progress_callback(0.0, "Scanning indexed files...")

        conn = self._index.get_cache_conn()
        rows = conn.execute(
            "SELECT file_path, language, function_name, class_name FROM code_index"
        ).fetchall()

        file_functions: dict[str, list[str]] = defaultdict(list)
        file_languages: dict[str, str] = {}
        for r in rows:
            fp, lang, func, cls = r[0], r[1], r[2], r[3]
            file_functions[fp].append(func)
            if lang:
                file_languages[fp] = lang

        codebase_root = Path(config.codebase_root)
        all_file_paths = set(file_functions.keys())

        layer_map: dict[str, LayerClassification] = {
            m.file_path: m.layer for m in architecture.layer_mappings
        }

        if progress_callback:
            progress_callback(0.2, "Building file inventory...")

        items: list[FileInventoryItem] = []
        frontend_items: list[FileInventoryItem] = []
        backend_items: list[FileInventoryItem] = []
        shared_items: list[FileInventoryItem] = []

        for fp, funcs in file_functions.items():
            ext = PurePosixPath(fp).suffix.lower()
            full_path = codebase_root / fp
            loc = _count_loc(full_path)
            layer = layer_map.get(fp, LayerClassification.UNKNOWN)
            has_markup = ext in _FRONTEND_EXTENSIONS
            has_cb = _has_code_behind(fp, codebase_root)
            related = _find_related_files(fp, all_file_paths)

            item = FileInventoryItem(
                file_path=fp,
                file_type=ext,
                language=file_languages.get(fp, ""),
                layer=layer,
                loc=loc,
                function_count=len(funcs),
                complexity=_estimate_complexity(loc, len(funcs)),
                has_ui_markup=has_markup,
                has_code_behind=has_cb,
                related_files=related,
            )
            items.append(item)

            if layer == LayerClassification.PRESENTATION:
                frontend_items.append(item)
            elif layer in (LayerClassification.BUSINESS_LOGIC, LayerClassification.DATA_ACCESS):
                backend_items.append(item)
            elif layer == LayerClassification.SHARED:
                shared_items.append(item)
            elif layer in (LayerClassification.UNKNOWN, LayerClassification.CONFIGURATION):
                backend_items.append(item)

        if progress_callback:
            progress_callback(0.5, "Generating component suggestions...")

        component_suggestions = await self._suggest_components(
            frontend_items, config, architecture
        )

        if progress_callback:
            progress_callback(0.7, "Extracting API endpoints...")

        api_endpoints = self._extract_api_endpoints(backend_items, file_functions)

        if progress_callback:
            progress_callback(0.9, "Generating inventory document...")

        markdown = self._generate_inventory_markdown(
            items, frontend_items, backend_items, shared_items,
            component_suggestions, api_endpoints,
        )

        return FileInventory(
            items=items,
            frontend_files=frontend_items,
            backend_files=backend_items,
            shared_files=shared_items,
            component_suggestions=component_suggestions,
            api_endpoints=api_endpoints,
            markdown_document=markdown,
        )

    async def _suggest_components(
        self,
        frontend_items: list[FileInventoryItem],
        config: ModernizationConfig,
        architecture: ArchitectureSpec,
    ) -> list[ComponentSuggestion]:
        """Use LLM to suggest React component hierarchy from frontend files."""
        if not frontend_items:
            return []

        file_list = "\n".join(
            f"- {item.file_path} (type={item.file_type}, loc={item.loc}, "
            f"functions={item.function_count}, complexity={item.complexity.value})"
            for item in frontend_items[:80]
        )

        prompt = get_prompt(
            "modernization.inventory_extraction",
            target_frontend=config.target_frontend,
            component_strategy=config.component_strategy.value,
            state_management=config.state_management,
            file_list=file_list,
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=3000,
                **settings.get_litellm_kwargs(),
            )
            text = response.choices[0].message.content or ""
            return self._parse_component_suggestions(text)
        except Exception as e:
            logger.warning("LLM component suggestion failed: %s", str(e)[:200])
            return self._fallback_component_suggestions(frontend_items)

    def _parse_component_suggestions(self, text: str) -> list[ComponentSuggestion]:
        suggestions = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line.startswith("COMPONENT:"):
                continue
            try:
                parts = {}
                for segment in line.split("|"):
                    segment = segment.strip()
                    if ":" in segment:
                        key, val = segment.split(":", 1)
                        parts[key.strip().upper()] = val.strip()

                suggestions.append(ComponentSuggestion(
                    component_name=parts.get("COMPONENT", "Unknown"),
                    component_type=parts.get("TYPE", "page").lower(),
                    source_files=[s.strip() for s in parts.get("SOURCES", "").split(",") if s.strip()],
                    props=[p.strip() for p in parts.get("PROPS", "").split(",") if p.strip()],
                    reuse_potential=parts.get("REUSE", "medium").lower(),
                ))
            except Exception:
                continue
        return suggestions

    def _fallback_component_suggestions(
        self, frontend_items: list[FileInventoryItem],
    ) -> list[ComponentSuggestion]:
        """Simple 1:1 mapping when LLM is unavailable."""
        suggestions = []
        for item in frontend_items:
            stem = PurePosixPath(item.file_path).stem
            name = "".join(word.capitalize() for word in stem.replace("-", "_").split("_"))
            suggestions.append(ComponentSuggestion(
                component_name=name or "UnnamedComponent",
                component_type="page",
                source_files=[item.file_path],
                reuse_potential="medium",
            ))
        return suggestions

    def _extract_api_endpoints(
        self,
        backend_items: list[FileInventoryItem],
        file_functions: dict[str, list[str]],
    ) -> list[str]:
        """Extract likely API endpoint names from backend files."""
        endpoints = []
        api_patterns = {"get", "post", "put", "delete", "patch", "create", "update", "list", "fetch"}
        for item in backend_items:
            funcs = file_functions.get(item.file_path, [])
            for f in funcs:
                lower = f.lower()
                if any(lower.startswith(p) or lower.endswith(p) for p in api_patterns):
                    endpoints.append(f"{item.file_path}::{f}")
        return endpoints

    def _generate_inventory_markdown(
        self,
        items: list[FileInventoryItem],
        frontend: list[FileInventoryItem],
        backend: list[FileInventoryItem],
        shared: list[FileInventoryItem],
        components: list[ComponentSuggestion],
        endpoints: list[str],
    ) -> str:
        lines = [
            "# Codebase Inventory Report",
            "",
            "## Summary",
            f"- **Total files indexed**: {len(items)}",
            f"- **Frontend/presentation files**: {len(frontend)}",
            f"- **Backend/business logic files**: {len(backend)}",
            f"- **Shared/utility files**: {len(shared)}",
            f"- **Suggested React components**: {len(components)}",
            f"- **API endpoints detected**: {len(endpoints)}",
            "",
        ]

        if frontend:
            lines.extend(["## Frontend Files", ""])
            lines.append("| File | Type | LOC | Complexity | Code-Behind |")
            lines.append("|------|------|-----|------------|-------------|")
            for item in frontend[:50]:
                lines.append(
                    f"| `{item.file_path}` | {item.file_type} | {item.loc} | "
                    f"{item.complexity.value} | {'Yes' if item.has_code_behind else 'No'} |"
                )
            if len(frontend) > 50:
                lines.append(f"| ... | +{len(frontend) - 50} more | | | |")
            lines.append("")

        if components:
            lines.extend(["## Suggested React Components", ""])
            lines.append("| Component | Type | Source Files | Reuse |")
            lines.append("|-----------|------|-------------|-------|")
            for c in components[:30]:
                sources = ", ".join(c.source_files[:3])
                if len(c.source_files) > 3:
                    sources += f" +{len(c.source_files) - 3}"
                lines.append(f"| `{c.component_name}` | {c.component_type} | {sources} | {c.reuse_potential} |")
            lines.append("")

        if backend:
            lines.extend(["## Backend Files", ""])
            lines.append("| File | Language | LOC | Functions | Complexity |")
            lines.append("|------|----------|-----|-----------|------------|")
            for item in backend[:50]:
                lines.append(
                    f"| `{item.file_path}` | {item.language} | {item.loc} | "
                    f"{item.function_count} | {item.complexity.value} |"
                )
            if len(backend) > 50:
                lines.append(f"| ... | +{len(backend) - 50} more | | | |")
            lines.append("")

        if endpoints:
            lines.extend(["## Detected API Endpoints", ""])
            for ep in endpoints[:30]:
                lines.append(f"- `{ep}`")
            if len(endpoints) > 30:
                lines.append(f"- ... and {len(endpoints) - 30} more")
            lines.append("")

        return "\n".join(lines)
