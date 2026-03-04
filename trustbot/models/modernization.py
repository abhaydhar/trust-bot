"""
Pydantic models for the Modernization Agent pipeline.

Covers all inter-agent data contracts: user configuration, architecture specs,
file inventories, component suggestions, roadmaps, generated code artifacts,
build results, test specs/results, parity reports, and pipeline state.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LayerClassification(str, Enum):
    PRESENTATION = "presentation"
    BUSINESS_LOGIC = "business_logic"
    DATA_ACCESS = "data_access"
    SHARED = "shared"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


class ComponentStrategy(str, Enum):
    MAXIMIZE_REUSE = "maximize_reuse"
    PAGE_PER_COMPONENT = "page_per_component"
    ATOMIC_DESIGN = "atomic_design"


class APIStyle(str, Enum):
    REST = "rest"
    GRAPHQL = "graphql"


class PipelinePhase(str, Enum):
    NOT_STARTED = "not_started"
    PHASE1_RUNNING = "phase1_running"
    PHASE1_COMPLETE = "phase1_complete"
    PHASE2_RUNNING = "phase2_running"
    PHASE2_COMPLETE = "phase2_complete"
    PHASE3_RUNNING = "phase3_running"
    PHASE3_COMPLETE = "phase3_complete"


class ParityStatus(str, Enum):
    MIGRATED = "migrated"
    PARTIAL = "partial"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class ComplexityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class TestCategory(str, Enum):
    FUNCTIONAL = "functional"
    SANITY = "sanity"
    INTEGRATION = "integration"
    UNIT = "unit"


# ---------------------------------------------------------------------------
# User Configuration (from UI form)
# ---------------------------------------------------------------------------


class ModernizationConfig(BaseModel):
    """User-provided configuration for the modernization pipeline."""

    source_index_path: str = Field(
        description="Path to the SQLite code index from the Git Indexer"
    )
    codebase_root: str = Field(
        description="Root directory of the cloned legacy codebase"
    )
    target_frontend: str = Field(
        default="react-typescript",
        description="Target frontend framework (react, react-typescript, angular, vue)",
    )
    target_backend: str = Field(
        default="aspnet-core-webapi",
        description="Target backend framework (aspnet-core-webapi, aspnet-minimal, nodejs-express, fastapi)",
    )
    component_strategy: ComponentStrategy = Field(
        default=ComponentStrategy.MAXIMIZE_REUSE,
    )
    state_management: str = Field(
        default="zustand",
        description="Frontend state management (redux, zustand, react-context, none)",
    )
    css_framework: str = Field(
        default="tailwind",
        description="CSS framework (tailwind, mui, bootstrap, custom)",
    )
    api_style: APIStyle = Field(default=APIStyle.REST)
    output_directory: str = Field(
        default="./modernization_output",
        description="Directory where generated code will be written",
    )
    max_build_retries: int = Field(default=5, ge=1, le=20)
    additional_requirements: str = Field(
        default="",
        description="Free-form additional requirements from the user",
    )


# ---------------------------------------------------------------------------
# Agent 1: Architecture Spec
# ---------------------------------------------------------------------------


class LayerMapping(BaseModel):
    """Maps a source file to its classified layer."""

    file_path: str
    layer: LayerClassification
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reasoning: str = ""


class ArchitectureSpec(BaseModel):
    """Output of the Modernization Architect Agent."""

    markdown_document: str = Field(
        description="Full proposed to-be architecture as markdown"
    )
    layer_mappings: list[LayerMapping] = Field(default_factory=list)
    total_files: int = 0
    total_functions: int = 0
    total_edges: int = 0
    languages_detected: list[str] = Field(default_factory=list)
    coupling_summary: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Agent 2: File Inventory & Component Suggestions
# ---------------------------------------------------------------------------


class FileInventoryItem(BaseModel):
    """Single file entry in the inventory."""

    file_path: str
    file_type: str = ""
    language: str = ""
    layer: LayerClassification = LayerClassification.UNKNOWN
    loc: int = 0
    function_count: int = 0
    complexity: ComplexityLevel = ComplexityLevel.MEDIUM
    has_ui_markup: bool = False
    has_code_behind: bool = False
    related_files: list[str] = Field(default_factory=list)


class ComponentSuggestion(BaseModel):
    """Proposed React component derived from legacy UI files."""

    component_name: str
    component_type: str = Field(
        default="page",
        description="page, layout, reusable, or atomic",
    )
    source_files: list[str] = Field(default_factory=list)
    props: list[str] = Field(default_factory=list)
    state_fields: list[str] = Field(default_factory=list)
    api_dependencies: list[str] = Field(default_factory=list)
    reuse_potential: str = "medium"
    notes: str = ""


class FileInventory(BaseModel):
    """Complete inventory of the legacy codebase for migration."""

    items: list[FileInventoryItem] = Field(default_factory=list)
    frontend_files: list[FileInventoryItem] = Field(default_factory=list)
    backend_files: list[FileInventoryItem] = Field(default_factory=list)
    shared_files: list[FileInventoryItem] = Field(default_factory=list)
    component_suggestions: list[ComponentSuggestion] = Field(default_factory=list)
    api_endpoints: list[str] = Field(default_factory=list)
    markdown_document: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Agent 3: Migration Roadmap
# ---------------------------------------------------------------------------


class MigrationPhaseItem(BaseModel):
    """Single item in a migration phase."""

    name: str
    source_files: list[str] = Field(default_factory=list)
    target_files: list[str] = Field(default_factory=list)
    estimated_hours: float = 0.0
    dependencies: list[str] = Field(default_factory=list)
    complexity: ComplexityLevel = ComplexityLevel.MEDIUM


class MigrationPhase(BaseModel):
    """A phase in the migration roadmap."""

    phase_number: int
    name: str
    description: str = ""
    items: list[MigrationPhaseItem] = Field(default_factory=list)
    estimated_total_hours: float = 0.0


class MigrationRoadmap(BaseModel):
    """Output of the Roadmap Generator Agent."""

    phases: list[MigrationPhase] = Field(default_factory=list)
    total_estimated_hours: float = 0.0
    critical_path: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    markdown_document: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Agent 4: Generated Code Artifacts
# ---------------------------------------------------------------------------


class GeneratedCodeArtifact(BaseModel):
    """A single file generated by the Code Generation Agent."""

    file_path: str = Field(description="Relative path within the output directory")
    content: str
    layer: LayerClassification
    source_files: list[str] = Field(
        default_factory=list,
        description="Legacy files this was derived from",
    )
    language: str = ""


class CodeGenResult(BaseModel):
    """Output of the Code Generation Agent."""

    artifacts: list[GeneratedCodeArtifact] = Field(default_factory=list)
    frontend_dir: str = ""
    backend_dir: str = ""
    summary_markdown: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Agent 5: Build Results
# ---------------------------------------------------------------------------


class BuildAttempt(BaseModel):
    """Record of a single build attempt."""

    iteration: int
    target: str = Field(description="frontend or backend")
    command: str
    success: bool
    stdout: str = ""
    stderr: str = ""
    errors_found: int = 0
    fixes_applied: list[str] = Field(default_factory=list)


class BuildResult(BaseModel):
    """Output of the Code Build Agent."""

    frontend_success: bool = False
    backend_success: bool = False
    total_iterations: int = 0
    attempts: list[BuildAttempt] = Field(default_factory=list)
    final_frontend_errors: list[str] = Field(default_factory=list)
    final_backend_errors: list[str] = Field(default_factory=list)
    summary_markdown: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Agent 6: Test Spec & Results
# ---------------------------------------------------------------------------


class TestSpec(BaseModel):
    """A single test specification generated from legacy analysis."""

    test_name: str
    category: TestCategory
    description: str = ""
    source_function: str = ""
    source_file: str = ""
    expected_behavior: str = ""


class TestFileOutput(BaseModel):
    """A generated test file."""

    file_path: str
    content: str
    test_count: int = 0
    framework: str = ""


class TestResult(BaseModel):
    """Output of the Code Test Agent."""

    specs: list[TestSpec] = Field(default_factory=list)
    generated_test_files: list[TestFileOutput] = Field(default_factory=list)
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    coverage_pct: float = 0.0
    failing_details: list[str] = Field(default_factory=list)
    summary_markdown: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Agent 7: Parity Report
# ---------------------------------------------------------------------------


class ParityItem(BaseModel):
    """Single business logic item tracked for parity."""

    legacy_function: str
    legacy_file: str
    description: str = ""
    new_function: str = ""
    new_file: str = ""
    status: ParityStatus = ParityStatus.MISSING
    notes: str = ""


class ParityReport(BaseModel):
    """Output of the Parity Verification Agent."""

    items: list[ParityItem] = Field(default_factory=list)
    total_items: int = 0
    migrated_count: int = 0
    partial_count: int = 0
    missing_count: int = 0
    coverage_pct: float = 0.0
    markdown_document: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Pipeline State & Phase Results
# ---------------------------------------------------------------------------


class Phase1Result(BaseModel):
    """Result of Phase 1: Planning."""

    architecture: ArchitectureSpec
    inventory: FileInventory
    roadmap: MigrationRoadmap


class Phase2Result(BaseModel):
    """Result of Phase 2: Code Generation & Build."""

    codegen: CodeGenResult
    build: BuildResult


class Phase3Result(BaseModel):
    """Result of Phase 3: Testing & Parity."""

    tests: TestResult
    parity: ParityReport


class ModernizationPipelineState(BaseModel):
    """Tracks the full pipeline state across approval gates."""

    phase: PipelinePhase = PipelinePhase.NOT_STARTED
    config: ModernizationConfig | None = None
    phase1_result: Phase1Result | None = None
    phase2_result: Phase2Result | None = None
    phase3_result: Phase3Result | None = None
    started_at: datetime | None = None
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    error: str = ""
