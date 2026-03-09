"""
Agent 3: Roadmap Generator

Analyzes dependency graph, estimates complexity, and generates a phased
migration plan with time estimates and critical-path analysis.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

import litellm

from trustbot.config import settings
from trustbot.prompts import get_prompt
from trustbot.models.modernization import (
    ArchitectureSpec,
    ComplexityLevel,
    FileInventory,
    MigrationPhase,
    MigrationPhaseItem,
    MigrationRoadmap,
    ModernizationConfig,
)

logger = logging.getLogger("trustbot.agents.modernization.roadmap")

_HOURS_PER_COMPLEXITY = {
    ComplexityLevel.LOW: 2.0,
    ComplexityLevel.MEDIUM: 6.0,
    ComplexityLevel.HIGH: 16.0,
    ComplexityLevel.VERY_HIGH: 40.0,
}


class RoadmapAgent:
    """Roadmap Generator -- creates phased migration plan with time estimates."""

    async def run(
        self,
        inventory: FileInventory,
        architecture: ArchitectureSpec,
        config: ModernizationConfig,
        progress_callback=None,
    ) -> MigrationRoadmap:
        if progress_callback:
            progress_callback(0.0, "Analyzing migration dependencies...")

        dep_graph = self._build_dependency_graph(inventory)

        if progress_callback:
            progress_callback(0.2, "Estimating complexity...")

        phase_items = self._create_phase_items(inventory, dep_graph)

        if progress_callback:
            progress_callback(0.4, "Generating migration phases...")

        phases = self._organize_into_phases(phase_items, dep_graph)

        total_hours = sum(p.estimated_total_hours for p in phases)
        critical_path = self._find_critical_path(phases, dep_graph)

        if progress_callback:
            progress_callback(0.6, "Generating roadmap document...")

        risk_factors = self._assess_risks(inventory, architecture)
        markdown = await self._generate_roadmap_doc(
            phases, total_hours, critical_path, risk_factors, config, inventory
        )

        return MigrationRoadmap(
            phases=phases,
            total_estimated_hours=total_hours,
            critical_path=critical_path,
            risk_factors=risk_factors,
            markdown_document=markdown,
        )

    def _build_dependency_graph(self, inventory: FileInventory) -> dict[str, set[str]]:
        """Build a dependency graph from related files."""
        deps: dict[str, set[str]] = defaultdict(set)
        for item in inventory.items:
            for related in item.related_files:
                deps[item.file_path].add(related)
        return deps

    def _create_phase_items(
        self,
        inventory: FileInventory,
        dep_graph: dict[str, set[str]],
    ) -> list[MigrationPhaseItem]:
        """Create migration items from inventory, one per component or file group."""
        items: list[MigrationPhaseItem] = []

        for comp in inventory.component_suggestions:
            hours = sum(
                _HOURS_PER_COMPLEXITY.get(
                    next(
                        (i.complexity for i in inventory.items if i.file_path in comp.source_files),
                        ComplexityLevel.MEDIUM,
                    ),
                    6.0,
                )
                for _ in comp.source_files
            ) if comp.source_files else 4.0

            deps_for_comp = set()
            for sf in comp.source_files:
                deps_for_comp.update(dep_graph.get(sf, set()))
            deps_for_comp -= set(comp.source_files)

            items.append(MigrationPhaseItem(
                name=comp.component_name,
                source_files=comp.source_files,
                target_files=[f"src/components/{comp.component_name}.tsx"],
                estimated_hours=min(hours, 80.0),
                dependencies=list(deps_for_comp)[:10],
                complexity=ComplexityLevel.MEDIUM,
            ))

        for item in inventory.backend_files:
            hours = _HOURS_PER_COMPLEXITY.get(item.complexity, 6.0)
            items.append(MigrationPhaseItem(
                name=f"Backend: {item.file_path}",
                source_files=[item.file_path],
                target_files=[],
                estimated_hours=hours,
                dependencies=list(dep_graph.get(item.file_path, set()))[:10],
                complexity=item.complexity,
            ))

        return items

    def _organize_into_phases(
        self,
        items: list[MigrationPhaseItem],
        dep_graph: dict[str, set[str]],
    ) -> list[MigrationPhase]:
        """Organize items into numbered phases based on dependencies and type."""
        shared_items = [i for i in items if i.name.startswith("Backend:") and
                        any("shared" in s.lower() or "common" in s.lower() for s in i.source_files)]
        backend_items = [i for i in items if i.name.startswith("Backend:") and i not in shared_items]
        frontend_items = [i for i in items if not i.name.startswith("Backend:")]

        phases = []

        if shared_items:
            total_h = sum(i.estimated_hours for i in shared_items)
            phases.append(MigrationPhase(
                phase_number=1,
                name="Foundation & Shared Components",
                description="Migrate shared utilities, DTOs, and common infrastructure",
                items=shared_items,
                estimated_total_hours=total_h,
            ))

        if backend_items:
            total_h = sum(i.estimated_hours for i in backend_items)
            phases.append(MigrationPhase(
                phase_number=len(phases) + 1,
                name="Backend API Layer",
                description="Migrate business logic and data access to new backend framework",
                items=backend_items,
                estimated_total_hours=total_h,
            ))

        if frontend_items:
            hi_reuse = [i for i in frontend_items if
                        any(c.reuse_potential == "high" for c in [])]
            remaining = [i for i in frontend_items if i not in hi_reuse]

            batch_size = max(len(frontend_items) // 3, 1)
            batches = [
                frontend_items[i:i + batch_size]
                for i in range(0, len(frontend_items), batch_size)
            ]

            for idx, batch in enumerate(batches):
                total_h = sum(i.estimated_hours for i in batch)
                phases.append(MigrationPhase(
                    phase_number=len(phases) + 1,
                    name=f"Frontend Migration Batch {idx + 1}",
                    description=f"Convert {len(batch)} frontend components to React",
                    items=batch,
                    estimated_total_hours=total_h,
                ))

        phases.append(MigrationPhase(
            phase_number=len(phases) + 1,
            name="Integration Testing & Parity Verification",
            description="Run integration tests and verify feature parity with legacy system",
            items=[],
            estimated_total_hours=max(sum(p.estimated_total_hours for p in phases) * 0.15, 8.0),
        ))

        return phases

    def _find_critical_path(
        self,
        phases: list[MigrationPhase],
        dep_graph: dict[str, set[str]],
    ) -> list[str]:
        """Identify the critical path (longest sequential chain)."""
        return [p.name for p in phases]

    def _assess_risks(
        self,
        inventory: FileInventory,
        architecture: ArchitectureSpec,
    ) -> list[str]:
        risks = []
        high_complexity = sum(
            1 for i in inventory.items
            if i.complexity in (ComplexityLevel.HIGH, ComplexityLevel.VERY_HIGH)
        )
        if high_complexity > 10:
            risks.append(
                f"{high_complexity} files have high/very-high complexity -- consider extra review cycles"
            )

        unknown_layers = sum(
            1 for m in architecture.layer_mappings
            if m.layer.value == "unknown"
        )
        if unknown_layers > 0:
            risks.append(
                f"{unknown_layers} files could not be classified into a layer -- manual triage needed"
            )

        total_loc = sum(i.loc for i in inventory.items)
        if total_loc > 100_000:
            risks.append(
                f"Large codebase ({total_loc:,} LOC) -- consider incremental migration"
            )

        if not inventory.api_endpoints:
            risks.append("No API endpoints detected -- API surface may need manual definition")

        return risks

    async def _generate_roadmap_doc(
        self,
        phases: list[MigrationPhase],
        total_hours: float,
        critical_path: list[str],
        risk_factors: list[str],
        config: ModernizationConfig,
        inventory: FileInventory,
    ) -> str:
        """Generate the roadmap as a markdown document using LLM."""
        phases_text = ""
        for p in phases:
            phases_text += f"\n### Phase {p.phase_number}: {p.name}\n"
            phases_text += f"- Description: {p.description}\n"
            phases_text += f"- Items: {len(p.items)}\n"
            phases_text += f"- Estimated hours: {p.estimated_total_hours:.0f}\n"

        prompt = get_prompt(
            "modernization.roadmap_generation",
            target_stack=f"{config.target_frontend} + {config.target_backend}",
            total_files=len(inventory.items),
            frontend_components=len(inventory.component_suggestions),
            total_hours=f"{total_hours:.0f}",
            phases_text=phases_text,
            critical_path=" -> ".join(critical_path),
            risk_factors="; ".join(risk_factors) if risk_factors else "None identified",
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
            logger.warning("LLM roadmap generation failed: %s", str(e)[:200])
            return self._fallback_roadmap_doc(phases, total_hours, critical_path, risk_factors)

    def _fallback_roadmap_doc(
        self,
        phases: list[MigrationPhase],
        total_hours: float,
        critical_path: list[str],
        risk_factors: list[str],
    ) -> str:
        lines = [
            "# Migration Roadmap",
            "",
            "## Executive Summary",
            f"Total estimated effort: **{total_hours:.0f} hours**",
            f"Number of phases: **{len(phases)}**",
            "",
            "## Phases",
        ]
        for p in phases:
            lines.extend([
                f"\n### Phase {p.phase_number}: {p.name}",
                f"{p.description}",
                f"- **Items**: {len(p.items)}",
                f"- **Estimated hours**: {p.estimated_total_hours:.0f}",
            ])
            if p.items:
                for item in p.items[:10]:
                    lines.append(f"  - {item.name} ({item.estimated_hours:.0f}h)")
                if len(p.items) > 10:
                    lines.append(f"  - ... and {len(p.items) - 10} more")
        lines.extend([
            "",
            "## Critical Path",
            " -> ".join(critical_path),
            "",
            "## Risk Factors",
        ])
        for r in risk_factors:
            lines.append(f"- {r}")
        return "\n".join(lines)
