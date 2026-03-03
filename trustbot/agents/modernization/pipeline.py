"""
Modernization Pipeline -- orchestrates all 7 agents across 3 phases.

Phase 1 (Planning):  Architect -> Inventory -> Roadmap
Phase 2 (Execution): CodeGen -> Build
Phase 3 (Validation): Test -> Parity

Approval gates between phases; state persisted so pipeline can resume
after user review.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.models.modernization import (
    ModernizationConfig,
    ModernizationPipelineState,
    Phase1Result,
    Phase2Result,
    Phase3Result,
    PipelinePhase,
)
from trustbot.tools.build_tool import BuildTool

from trustbot.agents.modernization.architect_agent import ArchitectAgent
from trustbot.agents.modernization.inventory_agent import InventoryAgent
from trustbot.agents.modernization.roadmap_agent import RoadmapAgent
from trustbot.agents.modernization.codegen_agent import CodeGenAgent
from trustbot.agents.modernization.build_agent import BuildAgent
from trustbot.agents.modernization.test_agent import TestAgent
from trustbot.agents.modernization.parity_agent import ParityAgent

logger = logging.getLogger("trustbot.agents.modernization.pipeline")


class ModernizationPipeline:
    """
    3-phase modernization pipeline with approval gates.

    Usage:
        pipeline = ModernizationPipeline(code_index, build_tool)
        phase1 = await pipeline.run_phase1(config, progress_cb)
        # User reviews phase1 results...
        phase2 = await pipeline.run_phase2(phase1, config, progress_cb)
        # User reviews phase2 results...
        phase3 = await pipeline.run_phase3(phase1, phase2, config, progress_cb)
    """

    def __init__(
        self,
        code_index: CodeIndex,
        build_tool: BuildTool,
        filesystem_tool=None,
    ) -> None:
        self._code_index = code_index
        self._build_tool = build_tool
        self._fs_tool = filesystem_tool

        self._architect = ArchitectAgent(code_index)
        self._inventory = InventoryAgent(code_index)
        self._roadmap = RoadmapAgent()
        self._codegen = CodeGenAgent(filesystem_tool)
        self._build = BuildAgent(build_tool)
        self._test = TestAgent(code_index, build_tool)
        self._parity = ParityAgent(code_index)

        self._state = ModernizationPipelineState()

    @property
    def state(self) -> ModernizationPipelineState:
        return self._state

    def set_code_index(self, code_index: CodeIndex) -> None:
        """Update code index (e.g. after re-indexing)."""
        self._code_index = code_index
        self._architect = ArchitectAgent(code_index)
        self._inventory = InventoryAgent(code_index)
        self._test = TestAgent(code_index, self._build_tool)
        self._parity = ParityAgent(code_index)

    async def run_phase1(
        self,
        config: ModernizationConfig,
        progress_callback=None,
    ) -> Phase1Result:
        """
        Phase 1: Planning
        Runs Architect -> Inventory -> Roadmap agents.
        """
        self._state.phase = PipelinePhase.PHASE1_RUNNING
        self._state.config = config
        self._state.started_at = datetime.utcnow()
        self._state.error = ""

        try:
            def _arch_progress(pct, msg):
                if progress_callback:
                    progress_callback(pct * 0.33, f"[Architect] {msg}")

            architecture = await self._architect.run(config, progress_callback=_arch_progress)
            logger.info(
                "Architect complete: %d files, %d functions, %d edges",
                architecture.total_files,
                architecture.total_functions,
                architecture.total_edges,
            )

            def _inv_progress(pct, msg):
                if progress_callback:
                    progress_callback(0.33 + pct * 0.33, f"[Inventory] {msg}")

            inventory = await self._inventory.run(
                architecture, config, progress_callback=_inv_progress
            )
            logger.info(
                "Inventory complete: %d items, %d frontend, %d backend, %d components",
                len(inventory.items),
                len(inventory.frontend_files),
                len(inventory.backend_files),
                len(inventory.component_suggestions),
            )

            def _road_progress(pct, msg):
                if progress_callback:
                    progress_callback(0.66 + pct * 0.34, f"[Roadmap] {msg}")

            roadmap = await self._roadmap.run(
                inventory, architecture, config, progress_callback=_road_progress
            )
            logger.info(
                "Roadmap complete: %d phases, %.0f total hours",
                len(roadmap.phases),
                roadmap.total_estimated_hours,
            )

            result = Phase1Result(
                architecture=architecture,
                inventory=inventory,
                roadmap=roadmap,
            )
            self._state.phase1_result = result
            self._state.phase = PipelinePhase.PHASE1_COMPLETE
            self._state.last_updated = datetime.utcnow()

            self._save_state(config)
            return result

        except Exception as e:
            self._state.error = str(e)
            self._state.phase = PipelinePhase.PHASE1_COMPLETE
            logger.exception("Phase 1 failed: %s", e)
            raise

    async def run_phase2(
        self,
        phase1: Phase1Result,
        config: ModernizationConfig,
        progress_callback=None,
        log_callback=None,
    ) -> Phase2Result:
        """
        Phase 2: Code Generation & Build
        Runs CodeGen -> Build agents.
        """
        self._state.phase = PipelinePhase.PHASE2_RUNNING
        self._state.error = ""

        try:
            def _cg_progress(pct, msg):
                if progress_callback:
                    progress_callback(pct * 0.6, f"[CodeGen] {msg}")

            codegen = await self._codegen.run(
                phase1, config, progress_callback=_cg_progress,
                log_callback=log_callback,
            )
            logger.info(
                "CodeGen complete: %d artifacts generated",
                len(codegen.artifacts),
            )

            def _build_progress(pct, msg):
                if progress_callback:
                    progress_callback(0.6 + pct * 0.4, f"[Build] {msg}")

            build = await self._build.run(
                codegen, config, progress_callback=_build_progress,
                log_callback=log_callback,
            )
            logger.info(
                "Build complete: frontend=%s, backend=%s, iterations=%d",
                build.frontend_success,
                build.backend_success,
                build.total_iterations,
            )

            result = Phase2Result(codegen=codegen, build=build)
            self._state.phase2_result = result
            self._state.phase = PipelinePhase.PHASE2_COMPLETE
            self._state.last_updated = datetime.utcnow()

            self._save_state(config)
            return result

        except Exception as e:
            self._state.error = str(e)
            self._state.phase = PipelinePhase.PHASE2_COMPLETE
            logger.exception("Phase 2 failed: %s", e)
            raise

    async def run_phase3(
        self,
        phase1: Phase1Result,
        phase2: Phase2Result,
        config: ModernizationConfig,
        progress_callback=None,
        log_callback=None,
    ) -> Phase3Result:
        """
        Phase 3: Testing & Parity Verification
        Runs Test -> Parity agents.
        """
        self._state.phase = PipelinePhase.PHASE3_RUNNING
        self._state.error = ""

        try:
            def _test_progress(pct, msg):
                if progress_callback:
                    progress_callback(pct * 0.5, f"[Test] {msg}")

            tests = await self._test.run(
                phase1, phase2, config, progress_callback=_test_progress,
                log_callback=log_callback,
            )
            logger.info(
                "Tests complete: %d total, %d passed, %d failed",
                tests.total_tests,
                tests.passed,
                tests.failed,
            )

            def _parity_progress(pct, msg):
                if progress_callback:
                    progress_callback(0.5 + pct * 0.5, f"[Parity] {msg}")

            parity = await self._parity.run(
                phase1, phase2, tests, config, progress_callback=_parity_progress
            )
            logger.info(
                "Parity complete: %d items, %d migrated, %d missing, %.1f%% coverage",
                parity.total_items,
                parity.migrated_count,
                parity.missing_count,
                parity.coverage_pct,
            )

            result = Phase3Result(tests=tests, parity=parity)
            self._state.phase3_result = result
            self._state.phase = PipelinePhase.PHASE3_COMPLETE
            self._state.last_updated = datetime.utcnow()

            self._save_state(config)
            return result

        except Exception as e:
            self._state.error = str(e)
            self._state.phase = PipelinePhase.PHASE3_COMPLETE
            logger.exception("Phase 3 failed: %s", e)
            raise

    def _save_state(self, config: ModernizationConfig) -> None:
        """Persist pipeline state to disk for resume after user review."""
        try:
            state_file = Path(config.output_directory) / ".modernization_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(
                self._state.model_dump_json(indent=2),
                encoding="utf-8",
            )
            logger.info("Pipeline state saved to %s", state_file)
        except Exception as e:
            logger.warning("Could not save pipeline state: %s", e)

    def load_state(self, config: ModernizationConfig) -> bool:
        """Load pipeline state from disk. Returns True if state was found."""
        try:
            state_file = Path(config.output_directory) / ".modernization_state.json"
            if state_file.exists():
                data = state_file.read_text(encoding="utf-8")
                self._state = ModernizationPipelineState.model_validate_json(data)
                logger.info("Pipeline state loaded from %s (phase=%s)", state_file, self._state.phase)
                return True
        except Exception as e:
            logger.warning("Could not load pipeline state: %s", e)
        return False
