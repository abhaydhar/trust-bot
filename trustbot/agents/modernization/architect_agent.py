"""
Agent 1: Modernization Architect

Queries the CodeIndex for all indexed files, functions, and call edges.
Classifies files by layer (presentation, business logic, data access)
using heuristics + LLM, then generates a proposed to-be architecture
as a structured markdown document.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from pathlib import PurePosixPath

import litellm

from trustbot.config import settings
from trustbot.prompts import get_prompt
from trustbot.index.code_index import CodeIndex
from trustbot.models.modernization import (
    ArchitectureSpec,
    LayerClassification,
    LayerMapping,
    ModernizationConfig,
)

logger = logging.getLogger("trustbot.agents.modernization.architect")

# Extension-based heuristics for layer classification
_PRESENTATION_EXTENSIONS = {
    ".cshtml", ".aspx", ".ascx", ".razor", ".master", ".html", ".htm",
    ".jsx", ".tsx", ".vue", ".svelte", ".css", ".scss", ".less", ".sass",
    ".xaml", ".resx",
}
_DATA_ACCESS_PATTERNS = {
    "repository", "repo", "dal", "dataaccess", "dbcontext", "migration",
    "entityframework", "dapper", "ado", "sqlhelper",
    "storage", "database", "dbwriter", "sqlite", "dbfactory",
}
_BUSINESS_LOGIC_PATTERNS = {
    "service", "manager", "handler", "processor", "engine", "validator",
    "workflow", "rule", "policy", "usecase",
    "archiver", "communication", "runtime", "server", "client", "plug",
    "timer", "channel", "converter", "condition",
}
_CONFIG_PATTERNS = {
    "startup", "program", "appsettings", "web.config", "global.asax",
    "bundleconfig", "webpack", "package.json", "tsconfig",
    "assemblyinfo", "app.config",
}

_PRESENTATION_DIR_PATTERNS = {
    "views", "pages", "components", "ui", "frontend", "wwwroot",
    "designer", "visualcontrols", "controls", "design", "images",
}
_BUSINESS_LOGIC_DIR_PATTERNS = {
    "controllers", "api", "endpoints",
    "communication", "runtime", "samples",
}
_DATA_ACCESS_DIR_PATTERNS = {
    "data", "models", "entities", "dal", "repositories",
}
_SHARED_DIR_PATTERNS = {
    "shared", "common", "utils", "helpers", "lib",
}

# .cs filename suffixes that indicate presentation (WinForms/WPF code-behind)
_CS_PRESENTATION_SUFFIXES = (
    ".designer.cs", "form.cs", "control.cs", "usercontrol.cs",
    "dialog.cs", "window.cs", "view.cs", "panel.cs",
)


def _classify_by_extension(file_path: str) -> LayerClassification | None:
    ext = PurePosixPath(file_path).suffix.lower()
    if ext in _PRESENTATION_EXTENSIONS:
        return LayerClassification.PRESENTATION
    return None


def _classify_cs_file(file_path: str) -> LayerClassification | None:
    """Classify .cs files using path/name heuristics specific to .NET projects."""
    lower = file_path.lower().replace("\\", "/")
    if not lower.endswith(".cs"):
        return None

    if any(lower.endswith(s) for s in _CS_PRESENTATION_SUFFIXES):
        return LayerClassification.PRESENTATION

    parts = lower.split("/")
    if any(p in _PRESENTATION_DIR_PATTERNS for p in parts):
        return LayerClassification.PRESENTATION

    name_stem = PurePosixPath(lower).stem.lower()
    if name_stem in _DATA_ACCESS_PATTERNS:
        return LayerClassification.DATA_ACCESS
    if name_stem in _BUSINESS_LOGIC_PATTERNS:
        return LayerClassification.BUSINESS_LOGIC
    if name_stem in _CONFIG_PATTERNS:
        return LayerClassification.CONFIGURATION

    if any(p in _DATA_ACCESS_DIR_PATTERNS for p in parts):
        return LayerClassification.DATA_ACCESS
    if any(p in _BUSINESS_LOGIC_DIR_PATTERNS for p in parts):
        return LayerClassification.BUSINESS_LOGIC
    if any(p in _SHARED_DIR_PATTERNS for p in parts):
        return LayerClassification.SHARED

    # .cs files that don't match anything specific default to business logic
    # rather than UNKNOWN -- the whole purpose is to modernize this code
    return LayerClassification.BUSINESS_LOGIC


def _classify_by_path_patterns(file_path: str) -> LayerClassification | None:
    lower = file_path.lower().replace("\\", "/")
    parts = lower.split("/")
    name_stem = PurePosixPath(lower).stem

    for part in parts:
        if part in _DATA_ACCESS_PATTERNS or name_stem in _DATA_ACCESS_PATTERNS:
            return LayerClassification.DATA_ACCESS
        if part in _BUSINESS_LOGIC_PATTERNS or name_stem in _BUSINESS_LOGIC_PATTERNS:
            return LayerClassification.BUSINESS_LOGIC
        if part in _CONFIG_PATTERNS or name_stem in _CONFIG_PATTERNS:
            return LayerClassification.CONFIGURATION

    if any(p in _PRESENTATION_DIR_PATTERNS for p in parts):
        return LayerClassification.PRESENTATION
    if any(p in _BUSINESS_LOGIC_DIR_PATTERNS for p in parts):
        return LayerClassification.BUSINESS_LOGIC
    if any(p in _DATA_ACCESS_DIR_PATTERNS for p in parts):
        return LayerClassification.DATA_ACCESS
    if any(p in _SHARED_DIR_PATTERNS for p in parts):
        return LayerClassification.SHARED

    return None


def _classify_file(file_path: str) -> tuple[LayerClassification, float]:
    """Classify a file using heuristics. Returns (layer, confidence)."""
    by_ext = _classify_by_extension(file_path)
    if by_ext is not None:
        return by_ext, 0.9

    # .cs files need special handling -- extension alone is ambiguous
    cs_result = _classify_cs_file(file_path)
    if cs_result is not None:
        return cs_result, 0.7

    by_path = _classify_by_path_patterns(file_path)
    if by_path is not None:
        return by_path, 0.75

    return LayerClassification.UNKNOWN, 0.3


class ArchitectAgent:
    """Modernization Architect -- analyzes codebase and proposes target architecture."""

    def __init__(self, code_index: CodeIndex) -> None:
        self._index = code_index

    async def run(
        self,
        config: ModernizationConfig,
        progress_callback=None,
    ) -> ArchitectureSpec:
        if progress_callback:
            progress_callback(0.0, "Querying code index...")

        conn = self._index.get_cache_conn()

        rows = conn.execute(
            "SELECT DISTINCT file_path, language, function_name, class_name FROM code_index"
        ).fetchall()
        edges = self._index.get_edges()

        all_files: dict[str, dict] = {}
        func_count = 0
        lang_counter: Counter = Counter()

        for r in rows:
            fp = r[0]
            lang = r[1] or ""
            if fp not in all_files:
                all_files[fp] = {"language": lang, "functions": [], "classes": set()}
            all_files[fp]["functions"].append(r[2])
            if r[3]:
                all_files[fp]["classes"].add(r[3])
            func_count += 1
            if lang:
                lang_counter[lang] += 1

        if progress_callback:
            progress_callback(0.2, "Classifying files by layer...")

        layer_mappings: list[LayerMapping] = []
        layer_counts: Counter = Counter()

        for fp in all_files:
            layer, confidence = _classify_file(fp)
            layer_mappings.append(LayerMapping(
                file_path=fp,
                layer=layer,
                confidence=confidence,
            ))
            layer_counts[layer] += 1

        unknown_files = [m for m in layer_mappings if m.layer == LayerClassification.UNKNOWN]
        if unknown_files:
            batch_size = 80
            total_batches = (len(unknown_files) + batch_size - 1) // batch_size
            for batch_idx in range(total_batches):
                batch = unknown_files[batch_idx * batch_size:(batch_idx + 1) * batch_size]
                if progress_callback:
                    progress_callback(
                        0.3 + 0.15 * (batch_idx / total_batches),
                        f"Using LLM to classify ambiguous files (batch {batch_idx + 1}/{total_batches})...",
                    )
                await self._llm_classify_batch(batch, config)

        if progress_callback:
            progress_callback(0.6, "Analyzing coupling...")

        coupling_summary = self._analyze_coupling(edges, layer_mappings)

        if progress_callback:
            progress_callback(0.8, "Generating architecture document...")

        markdown = await self._generate_architecture_doc(
            config, all_files, layer_mappings, edges, lang_counter, coupling_summary
        )

        return ArchitectureSpec(
            markdown_document=markdown,
            layer_mappings=layer_mappings,
            total_files=len(all_files),
            total_functions=func_count,
            total_edges=len(edges),
            languages_detected=list(lang_counter.keys()),
            coupling_summary=coupling_summary,
        )

    async def _llm_classify_batch(
        self,
        unknown_mappings: list[LayerMapping],
        config: ModernizationConfig,
    ) -> None:
        """Use the LLM to classify files that heuristics couldn't resolve."""
        file_list = "\n".join(f"- {m.file_path}" for m in unknown_mappings)
        prompt = get_prompt(
            "modernization.architect_analysis",
            file_list=file_list,
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=2000,
                **settings.get_litellm_kwargs(),
            )
            text = response.choices[0].message.content or ""
            mapping_lookup = {m.file_path: m for m in unknown_mappings}

            for line in text.strip().splitlines():
                if " -> " not in line:
                    continue
                parts = line.split(" -> ", 1)
                fp = parts[0].strip().lstrip("- ")
                layer_str = parts[1].strip().lower()

                if fp in mapping_lookup:
                    try:
                        mapping_lookup[fp].layer = LayerClassification(layer_str)
                        mapping_lookup[fp].confidence = 0.7
                        mapping_lookup[fp].reasoning = "LLM classification"
                    except ValueError:
                        pass
        except Exception as e:
            logger.warning("LLM classification failed, keeping UNKNOWN: %s", str(e)[:200])

    def _analyze_coupling(
        self,
        edges: list[dict],
        layer_mappings: list[LayerMapping],
    ) -> str:
        """Analyze coupling between layers via call graph edges."""
        file_to_layer: dict[str, LayerClassification] = {}
        for m in layer_mappings:
            file_to_layer[m.file_path] = m.layer

        cross_layer: Counter = Counter()
        for edge in edges:
            caller = edge.get("from", "")
            callee = edge.get("to", "")
            caller_file = caller.split("::")[0] if "::" in caller else ""
            callee_file = callee.split("::")[0] if "::" in callee else ""
            l1 = file_to_layer.get(caller_file, LayerClassification.UNKNOWN)
            l2 = file_to_layer.get(callee_file, LayerClassification.UNKNOWN)
            if l1 != l2 and l1 != LayerClassification.UNKNOWN and l2 != LayerClassification.UNKNOWN:
                cross_layer[f"{l1.value} -> {l2.value}"] += 1

        if not cross_layer:
            return "Minimal cross-layer coupling detected."

        lines = ["Cross-layer call relationships:"]
        for pair, count in cross_layer.most_common(10):
            lines.append(f"  {pair}: {count} edges")
        return "\n".join(lines)

    async def _generate_architecture_doc(
        self,
        config: ModernizationConfig,
        all_files: dict,
        layer_mappings: list[LayerMapping],
        edges: list[dict],
        lang_counter: Counter,
        coupling_summary: str,
    ) -> str:
        """Generate the to-be architecture markdown using LLM."""
        layer_counts = Counter(m.layer.value for m in layer_mappings)
        layer_summary = "\n".join(f"  - {k}: {v} files" for k, v in layer_counts.most_common())
        lang_summary = "\n".join(f"  - {k}: {v} functions" for k, v in lang_counter.most_common())

        sample_files = {}
        for m in layer_mappings[:5]:
            sample_files[m.file_path] = m.layer.value

        prompt = get_prompt(
            "modernization.architect_fallback",
            total_files=len(all_files),
            total_functions=sum(len(f["functions"]) for f in all_files.values()),
            total_edges=len(edges),
            lang_summary=lang_summary,
            layer_summary=layer_summary,
            coupling_summary=coupling_summary,
            target_frontend=config.target_frontend,
            target_backend=config.target_backend,
            component_strategy=config.component_strategy.value,
            state_management=config.state_management,
            css_framework=config.css_framework,
            api_style=config.api_style.value,
            additional_requirements=config.additional_requirements or "None",
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=settings.llm_max_tokens,
                **settings.get_litellm_kwargs(),
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.warning("LLM architecture doc generation failed: %s", str(e)[:200])
            return self._generate_fallback_doc(config, all_files, layer_mappings, edges, lang_counter)

    def _generate_fallback_doc(
        self,
        config: ModernizationConfig,
        all_files: dict,
        layer_mappings: list[LayerMapping],
        edges: list[dict],
        lang_counter: Counter,
    ) -> str:
        """Template-based fallback when LLM is unavailable."""
        layer_counts = Counter(m.layer.value for m in layer_mappings)
        lines = [
            "# Proposed Modernization Architecture",
            "",
            "## Executive Summary",
            f"Modernization of a {len(all_files)}-file legacy codebase to "
            f"{config.target_frontend} (frontend) + {config.target_backend} (backend).",
            "",
            "## Current State (AS-IS)",
            f"- **Files**: {len(all_files)}",
            f"- **Call edges**: {len(edges)}",
            f"- **Languages**: {', '.join(lang_counter.keys())}",
            "",
            "### Layer Distribution",
        ]
        for layer, count in layer_counts.most_common():
            lines.append(f"- **{layer}**: {count} files")
        lines.extend([
            "",
            "## Proposed Architecture (TO-BE)",
            f"- **Frontend**: {config.target_frontend}",
            f"- **Backend**: {config.target_backend}",
            f"- **Component Strategy**: {config.component_strategy.value}",
            f"- **State Management**: {config.state_management}",
            f"- **CSS Framework**: {config.css_framework}",
            f"- **API Style**: {config.api_style.value}",
            "",
            "## Migration Considerations",
            f"- {layer_counts.get('presentation', 0)} presentation files to convert to React components",
            f"- {layer_counts.get('business_logic', 0)} business logic files to extract as API services",
            f"- {layer_counts.get('data_access', 0)} data access files to refactor",
            "",
        ])
        return "\n".join(lines)
