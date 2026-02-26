"""
Tests for the multi-agent validation pipeline and call graph matching logic.

Covers:
- File path normalization (absolute Windows → filename)
- Edge comparison with 3-tier matching (full, name+file, name-only)
- Normalization agent (uppercase, aliases, file paths)
- Verification agent (confirmed, phantom, missing, match tiers)
- Agent 2 chunk ID parsing
"""

from __future__ import annotations

import pytest

from trustbot.models.agentic import (
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
    SpecFlowDocument,
    normalize_file_path,
)
from trustbot.agents.normalization import NormalizationAgent
from trustbot.agents.verification import VerificationAgent
from trustbot.agents.agent2_index import (
    _parse_chunk_id,
    _extract_func_name,
    _derive_project_prefix,
    _path_matches_prefix,
    _to_bare_name as agent2_to_bare_name,
)
from trustbot.indexing.call_graph_builder import _common_prefix_length, _resolve_callee
from trustbot.indexing.chunker import CodeChunk


# ─── normalize_file_path ─────────────────────────────────────────────


class TestNormalizeFilePath:
    """Verify that absolute/relative paths all reduce to FILENAME.EXT uppercase."""

    def test_absolute_windows_path(self):
        path = r"C:\Projects\Delphi\src\services\PaymentService.pas"
        assert normalize_file_path(path) == "PAYMENTSERVICE.PAS"

    def test_absolute_windows_forward_slash(self):
        path = "C:/Projects/Delphi/src/services/PaymentService.pas"
        assert normalize_file_path(path) == "PAYMENTSERVICE.PAS"

    def test_relative_unix_path(self):
        path = "services/PaymentService.pas"
        assert normalize_file_path(path) == "PAYMENTSERVICE.PAS"

    def test_just_filename(self):
        assert normalize_file_path("PaymentService.pas") == "PAYMENTSERVICE.PAS"

    def test_empty_path(self):
        assert normalize_file_path("") == ""

    def test_path_with_spaces(self):
        path = r"C:\My Projects\app\Main Form.pas"
        assert normalize_file_path(path) == "MAIN FORM.PAS"

    def test_mixed_separators(self):
        path = r"src\services/nested\PaymentService.pas"
        assert normalize_file_path(path) == "PAYMENTSERVICE.PAS"


# ─── Chunk ID parsing (Agent 2) ──────────────────────────────────────


class TestChunkIdParsing:
    """Verify chunk ID → (file_path, class_name, function_name) extraction."""

    def test_three_part_id(self):
        file_path, class_name, func_name = _parse_chunk_id(
            "services/payment_service.pas::TPaymentService::ProcessPayment"
        )
        assert file_path == "services/payment_service.pas"
        assert class_name == "TPaymentService"
        assert func_name == "ProcessPayment"

    def test_two_part_id_no_class(self):
        file_path, class_name, func_name = _parse_chunk_id(
            "services/payment_service.pas::::ProcessPayment"
        )
        assert file_path == "services/payment_service.pas"
        assert class_name == ""
        assert func_name == "ProcessPayment"

    def test_extract_func_name_three_part(self):
        name = _extract_func_name(
            "services/payment_service.pas::TPaymentService::ProcessPayment"
        )
        assert name == "ProcessPayment"

    def test_extract_func_name_two_part(self):
        name = _extract_func_name("services/payment_service.pas::::ProcessPayment")
        assert name == "ProcessPayment"

    def test_extract_func_name_bare(self):
        name = _extract_func_name("ProcessPayment")
        assert name == "ProcessPayment"


class TestAgent2BareNameResolution:
    """Verify Agent 2 bare-name stripping for qualified Neo4j root names."""

    def test_bare_from_qualified(self):
        assert agent2_to_bare_name("TForm1.Button2Click") == "Button2Click"

    def test_bare_from_bare(self):
        assert agent2_to_bare_name("Button2Click") == "Button2Click"

    def test_bare_empty(self):
        assert agent2_to_bare_name("") == ""

    def test_bare_dotted_multi(self):
        assert agent2_to_bare_name("Namespace.Class.Method") == "Method"

    def test_bare_preserves_case(self):
        assert agent2_to_bare_name("TForm1.InitialiseEcran") == "InitialiseEcran"


# ─── to_comparable_edges ─────────────────────────────────────────────


class TestComparableEdges:
    """Verify that CallGraphOutput produces the correct comparison tuples."""

    def _make_output(self, edges, source=GraphSource.NEO4J):
        return CallGraphOutput(
            execution_flow_id="EF-001",
            source=source,
            root_function="main",
            edges=edges,
        )

    def test_full_6tuple_comparison(self):
        output = self._make_output([
            CallGraphEdge(
                caller="ProcessPayment", callee="ValidateCard",
                caller_class="TPaymentService", callee_class="TCardValidator",
                caller_file=r"C:\src\PaymentService.pas",
                callee_file=r"C:\src\CardValidator.pas",
            ),
        ])
        edges = output.to_comparable_edges()
        assert edges == {(
            "PROCESSPAYMENT", "TPAYMENTSERVICE", "PAYMENTSERVICE.PAS",
            "VALIDATECARD", "TCARDVALIDATOR", "CARDVALIDATOR.PAS",
        )}

    def test_name_only_comparison(self):
        output = self._make_output([
            CallGraphEdge(caller="ProcessPayment", callee="ValidateCard"),
        ])
        edges = output.to_comparable_edges_by_name()
        assert edges == {("PROCESSPAYMENT", "VALIDATECARD")}

    def test_case_insensitive(self):
        output = self._make_output([
            CallGraphEdge(caller="processPayment", callee="validateCard"),
        ])
        edges = output.to_comparable_edges_by_name()
        assert ("PROCESSPAYMENT", "VALIDATECARD") in edges


# ─── NormalizationAgent ──────────────────────────────────────────────


class TestNormalizationAgent:
    """Verify normalization uppercases names and normalizes file paths."""

    def test_normalizes_names_and_files(self):
        agent = NormalizationAgent()
        output = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="ProcessPayment",
            edges=[
                CallGraphEdge(
                    caller="ProcessPayment", callee="ValidateCard",
                    caller_file=r"C:\Projects\src\PaymentService.pas",
                    callee_file=r"C:\Projects\src\CardValidator.pas",
                    caller_class="TPaymentService",
                    callee_class="TCardValidator",
                ),
            ],
        )
        norm = agent.normalize(output)

        assert norm.root_function == "PROCESSPAYMENT"
        e = norm.edges[0]
        assert e.caller == "PROCESSPAYMENT"
        assert e.callee == "VALIDATECARD"
        assert e.caller_file == "PAYMENTSERVICE.PAS"
        assert e.callee_file == "CARDVALIDATOR.PAS"
        assert e.caller_class == "TPAYMENTSERVICE"
        assert e.callee_class == "TCARDVALIDATOR"


# ─── VerificationAgent — Tier matching ────────────────────────────────


class TestVerificationAgentTierMatching:
    """
    Core tests for the 3-tier matching logic:
    1. Full match: name + class + file all agree
    2. Name+file match: names and files agree, class differs or missing
    3. Name-only match: only function names agree
    """

    def _neo4j_edge(self, caller, callee, caller_file="", callee_file="",
                     caller_class="", callee_class=""):
        return CallGraphEdge(
            caller=caller, callee=callee,
            caller_file=caller_file, callee_file=callee_file,
            caller_class=caller_class, callee_class=callee_class,
            extraction_method=ExtractionMethod.NEO4J,
        )

    def _index_edge(self, caller, callee, caller_file="", callee_file="",
                     caller_class="", callee_class=""):
        return CallGraphEdge(
            caller=caller, callee=callee,
            caller_file=caller_file, callee_file=callee_file,
            caller_class=caller_class, callee_class=callee_class,
            extraction_method=ExtractionMethod.REGEX,
        )

    def test_full_match_absolute_vs_relative_paths(self):
        """
        Neo4j has absolute path, Index has relative path.
        Both should normalize to the same filename → full match.
        """
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="A",
            edges=[self._neo4j_edge(
                "ProcessPayment", "ValidateCard",
                caller_file=r"C:\Projects\Delphi\src\PaymentService.pas",
                callee_file=r"C:\Projects\Delphi\src\CardValidator.pas",
                caller_class="TPaymentService",
                callee_class="TCardValidator",
            )],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="A",
            edges=[self._index_edge(
                "ProcessPayment", "ValidateCard",
                caller_file="src/PaymentService.pas",
                callee_file="src/CardValidator.pas",
                caller_class="TPaymentService",
                callee_class="TCardValidator",
            )],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 1
        assert len(result.phantom_edges) == 0
        assert len(result.missing_edges) == 0
        assert "Full match" in result.confirmed_edges[0].details
        assert result.metadata["match_full"] == 1

    def test_name_file_match_class_missing(self):
        """
        Neo4j has class_name, Index doesn't. Should still match at tier 2.
        """
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="A",
            edges=[self._neo4j_edge(
                "ProcessPayment", "ValidateCard",
                caller_file=r"C:\src\PaymentService.pas",
                callee_file=r"C:\src\CardValidator.pas",
                caller_class="TPaymentService",
                callee_class="TCardValidator",
            )],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="A",
            edges=[self._index_edge(
                "ProcessPayment", "ValidateCard",
                caller_file="src/PaymentService.pas",
                callee_file="src/CardValidator.pas",
                caller_class="",
                callee_class="",
            )],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 1
        assert len(result.phantom_edges) == 0
        assert "name + file" in result.confirmed_edges[0].details.lower()

    def test_name_only_match_no_files(self):
        """
        When no file paths are available on one side, fall back to name-only.
        """
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="A",
            edges=[self._neo4j_edge(
                "ProcessPayment", "ValidateCard",
                caller_file=r"C:\src\PaymentService.pas",
                callee_file=r"C:\src\CardValidator.pas",
            )],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="A",
            edges=[self._index_edge(
                "ProcessPayment", "ValidateCard",
            )],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 1
        assert len(result.phantom_edges) == 0
        assert "name only" in result.confirmed_edges[0].details.lower()

    def test_phantom_edge_no_match(self):
        """Edge in Neo4j not in Index at any tier → phantom."""
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="A",
            edges=[
                self._neo4j_edge("A", "B"),
                self._neo4j_edge("A", "X"),
            ],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="A",
            edges=[self._index_edge("A", "B")],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 1
        assert len(result.phantom_edges) == 1
        assert result.phantom_edges[0].callee == "X"

    def test_bare_name_match_qualified_neo4j(self):
        """Neo4j uses qualified name (TForm1.Button2Click), index uses bare (Button2Click) → match on bare."""
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="TForm1",
            edges=[self._neo4j_edge("TForm1.Button2Click", "InitialiseEcran")],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="Button2Click",
            edges=[self._index_edge("Button2Click", "InitialiseEcran")],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 1
        assert result.confirmed_edges[0].caller == "TFORM1.BUTTON2CLICK"
        assert result.confirmed_edges[0].callee == "INITIALISEECRAN"
        assert "bare name" in result.confirmed_edges[0].details.lower()
        assert len(result.phantom_edges) == 0
        assert len(result.missing_edges) == 0

    def test_bare_name_file_match_qualified_neo4j(self):
        """Bare name + file tier: Neo4j qualified names match index bare names when files match."""
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="Root",
            edges=[
                self._neo4j_edge(
                    "TForm1.Button2Click", "TForm1.InitialiseEcran",
                    caller_file=r"C:\src\Unit1.pas", callee_file=r"C:\src\Unit1.pas",
                ),
            ],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="Button2Click",
            edges=[
                self._index_edge(
                    "Button2Click", "InitialiseEcran",
                    caller_file="src/Unit1.pas", callee_file="src/Unit1.pas",
                ),
            ],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 1
        assert "bare name" in result.confirmed_edges[0].details.lower()
        assert "file" in result.confirmed_edges[0].details.lower()
        assert len(result.phantom_edges) == 0
        assert len(result.missing_edges) == 0

    def test_multiple_qualified_edges_all_match_bare(self):
        """Multiple Neo4j edges with qualified names all match bare index edges."""
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="TForm1",
            edges=[
                self._neo4j_edge("TForm1.Button2Click", "InitialiseEcran"),
                self._neo4j_edge("TForm1.FormCreate", "LoadData"),
            ],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="Button2Click",
            edges=[
                self._index_edge("Button2Click", "InitialiseEcran"),
                self._index_edge("FormCreate", "LoadData"),
            ],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 2
        assert len(result.phantom_edges) == 0
        assert len(result.missing_edges) == 0

    def test_missing_edge_in_index_only(self):
        """Edge in Index but not in Neo4j → missing."""
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="A",
            edges=[self._neo4j_edge("A", "B")],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="A",
            edges=[
                self._index_edge("A", "B"),
                self._index_edge("B", "Z"),
            ],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 1
        assert len(result.missing_edges) == 1
        assert result.missing_edges[0].callee == "Z"

    def test_trust_scores_decrease_by_tier(self):
        """Full match should have higher trust than name+file, which is higher than name-only."""
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="A",
            edges=[
                self._neo4j_edge(
                    "A", "B",
                    caller_file=r"C:\src\A.pas", callee_file=r"C:\src\B.pas",
                    caller_class="TA", callee_class="TB",
                ),
                self._neo4j_edge(
                    "A", "C",
                    caller_file=r"C:\src\A.pas", callee_file=r"C:\src\C.pas",
                    caller_class="TA", callee_class="TC",
                ),
                self._neo4j_edge(
                    "A", "D",
                    caller_file=r"C:\src\A.pas", callee_file=r"C:\src\D.pas",
                ),
            ],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="A",
            edges=[
                # Full match for A→B
                self._index_edge(
                    "A", "B",
                    caller_file="src/A.pas", callee_file="src/B.pas",
                    caller_class="TA", callee_class="TB",
                ),
                # Name+file match for A→C (class differs)
                self._index_edge(
                    "A", "C",
                    caller_file="src/A.pas", callee_file="src/C.pas",
                    caller_class="", callee_class="",
                ),
                # Name-only for A→D (no file in index)
                self._index_edge("A", "D"),
            ],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 3
        assert len(result.phantom_edges) == 0

        scores_by_callee = {e.callee: e.trust_score for e in result.confirmed_edges}
        # Full match > name+file > name-only
        assert scores_by_callee["B"] > scores_by_callee["C"]
        assert scores_by_callee["C"] > scores_by_callee["D"]


# ─── Realistic Delphi scenario ────────────────────────────────────────


class TestDelphiRealisticScenario:
    """
    Simulate a real Delphi project where:
    - Neo4j stores absolute Windows paths and bare function names
    - Index stores relative paths and bare function names
    - Both should match after normalization
    """

    def test_delphi_full_flow(self):
        neo = CallGraphOutput(
            execution_flow_id="EF-DELPHI-001",
            source=GraphSource.NEO4J,
            root_function="FormCreate",
            edges=[
                CallGraphEdge(
                    caller="FormCreate", callee="InitializeDatabase",
                    caller_file=r"C:\DelphiProject\Source\MainForm.pas",
                    callee_file=r"C:\DelphiProject\Source\DatabaseModule.pas",
                    caller_class="TMainForm", callee_class="TDatabaseModule",
                    extraction_method=ExtractionMethod.NEO4J,
                ),
                CallGraphEdge(
                    caller="InitializeDatabase", callee="OpenConnection",
                    caller_file=r"C:\DelphiProject\Source\DatabaseModule.pas",
                    callee_file=r"C:\DelphiProject\Source\DatabaseModule.pas",
                    caller_class="TDatabaseModule", callee_class="TDatabaseModule",
                    extraction_method=ExtractionMethod.NEO4J,
                ),
                CallGraphEdge(
                    caller="FormCreate", callee="LoadSettings",
                    caller_file=r"C:\DelphiProject\Source\MainForm.pas",
                    callee_file=r"C:\DelphiProject\Source\SettingsUnit.pas",
                    caller_class="TMainForm", callee_class="TSettings",
                    extraction_method=ExtractionMethod.NEO4J,
                ),
            ],
        )

        fs = CallGraphOutput(
            execution_flow_id="EF-DELPHI-001",
            source=GraphSource.FILESYSTEM,
            root_function="FormCreate",
            edges=[
                CallGraphEdge(
                    caller="FormCreate", callee="InitializeDatabase",
                    caller_file="Source/MainForm.pas",
                    callee_file="Source/DatabaseModule.pas",
                    caller_class="TMainForm", callee_class="TDatabaseModule",
                    extraction_method=ExtractionMethod.REGEX,
                ),
                CallGraphEdge(
                    caller="InitializeDatabase", callee="OpenConnection",
                    caller_file="Source/DatabaseModule.pas",
                    callee_file="Source/DatabaseModule.pas",
                    caller_class="TDatabaseModule", callee_class="TDatabaseModule",
                    extraction_method=ExtractionMethod.REGEX,
                ),
                CallGraphEdge(
                    caller="FormCreate", callee="LoadSettings",
                    caller_file="Source/MainForm.pas",
                    callee_file="Source/SettingsUnit.pas",
                    caller_class="TMainForm", callee_class="TSettings",
                    extraction_method=ExtractionMethod.REGEX,
                ),
            ],
        )

        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))

        assert len(result.confirmed_edges) == 3
        assert len(result.phantom_edges) == 0
        assert len(result.missing_edges) == 0
        assert result.flow_trust_score > 0.8
        assert result.metadata["match_full"] == 3


class TestEmptyGraphs:
    """Edge cases with empty or mismatched graphs."""

    def test_both_empty(self):
        neo = CallGraphOutput(
            execution_flow_id="EF-001", source=GraphSource.NEO4J,
            root_function="A", edges=[],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001", source=GraphSource.FILESYSTEM,
            root_function="A", edges=[],
        )
        result = VerificationAgent().verify(neo, fs)
        assert len(result.confirmed_edges) == 0
        assert len(result.phantom_edges) == 0
        assert len(result.missing_edges) == 0

    def test_neo4j_has_edges_index_empty(self):
        """All Neo4j edges become phantom when index is empty."""
        neo = CallGraphOutput(
            execution_flow_id="EF-001", source=GraphSource.NEO4J,
            root_function="A",
            edges=[
                CallGraphEdge(caller="A", callee="B"),
                CallGraphEdge(caller="B", callee="C"),
            ],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001", source=GraphSource.FILESYSTEM,
            root_function="A", edges=[],
        )
        result = VerificationAgent().verify(neo, fs)
        assert len(result.confirmed_edges) == 0
        assert len(result.phantom_edges) == 2


def test_spec_flow_document() -> None:
    """SpecFlowDocument model validation."""
    spec = SpecFlowDocument(
        root_function="main",
        root_file_path="src/main.py",
        language="python",
        execution_flow_id="EF-001",
    )
    assert spec.root_function == "main"
    assert spec.root_file_path == "src/main.py"


# ─── Project prefix derivation (Agent 2 scoping) ─────────────────────


class TestDeriveProjectPrefix:
    """Verify project prefix extraction from Neo4j root_file + index paths."""

    def test_matches_dfm_to_pas_by_stem(self):
        root_file = "/mnt/storage/Delphi-Test/011-MultiLevelList/src/Unit1.dfm"
        index_paths = [
            "011-MultiLevelList\\src\\Unit1.pas",
            "015-MVC-En-Delphi\\Unit1.pas",
        ]
        assert _derive_project_prefix(root_file, index_paths) == "011-MultiLevelList"

    def test_exact_filename_match(self):
        root_file = "/mnt/storage/MyProject/src/Main.pas"
        index_paths = [
            "MyProject\\src\\Main.pas",
            "OtherProject\\Main.pas",
        ]
        assert _derive_project_prefix(root_file, index_paths) == "MyProject"

    def test_empty_root_file(self):
        assert _derive_project_prefix("", ["a/b.pas"]) == ""

    def test_no_matching_paths(self):
        root_file = "/mnt/storage/Proj/Unique.pas"
        index_paths = ["Other\\Something.pas"]
        assert _derive_project_prefix(root_file, index_paths) == ""

    def test_single_component_path(self):
        root_file = "/mnt/Unit1.pas"
        index_paths = ["Unit1.pas"]
        # No directory component → empty prefix
        assert _derive_project_prefix(root_file, index_paths) == ""


class TestPathMatchesPrefix:
    """Verify project path matching."""

    def test_matches_forward_slash(self):
        assert _path_matches_prefix("011-MultiLevelList/src/Unit1.pas", "011-MultiLevelList")

    def test_matches_backslash(self):
        assert _path_matches_prefix("011-MultiLevelList\\src\\Unit1.pas", "011-MultiLevelList")

    def test_case_insensitive(self):
        assert _path_matches_prefix("011-multilevellist/src/Unit1.pas", "011-MultiLevelList")

    def test_no_match(self):
        assert not _path_matches_prefix("015-MVC-En-Delphi/Unit1.pas", "011-MultiLevelList")

    def test_empty_prefix_always_matches(self):
        assert _path_matches_prefix("anything/at/all.pas", "")


# ─── Call graph builder proximity resolution ──────────────────────────


class TestCallGraphBuilderProximity:
    """Verify that callee resolution prefers same-project chunks."""

    def _make_chunk(self, name: str, file_path: str) -> CodeChunk:
        return CodeChunk(
            chunk_id=f"{file_path}::::{name}",
            file_path=file_path,
            function_name=name,
            class_name="",
            language="delphi",
            line_start=1,
            line_end=10,
            content=f"procedure {name}; begin end;",
        )

    def test_common_prefix_length_same_project(self):
        assert _common_prefix_length(
            "011-MultiLevelList/src/Unit1.pas",
            "011-MultiLevelList/src/Unit3.pas",
        ) == 2  # "011-MultiLevelList" + "src"

    def test_common_prefix_length_diff_project(self):
        assert _common_prefix_length(
            "011-MultiLevelList/src/Unit1.pas",
            "015-MVC-En-Delphi/Unit1.pas",
        ) == 0

    def test_resolve_callee_prefers_same_project(self):
        chunk_011 = self._make_chunk("Button1Click", "011-MultiLevelList/src/Unit1.pas")
        chunk_015 = self._make_chunk("Button1Click", "015-MVC-En-Delphi/Unit1.pas")
        func_map = {"BUTTON1CLICK": [chunk_011, chunk_015]}

        result = _resolve_callee("Button1Click", func_map, "011-MultiLevelList/src/fMain.pas")
        assert result is chunk_011

    def test_resolve_callee_single_candidate(self):
        chunk = self._make_chunk("UniqueFunc", "proj/src/Utils.pas")
        func_map = {"UNIQUEFUNC": [chunk]}

        result = _resolve_callee("UniqueFunc", func_map, "other/caller.pas")
        assert result is chunk

    def test_resolve_callee_not_found(self):
        result = _resolve_callee("NonExistent", {}, "any/path.pas")
        assert result is None


# ─── DFM file parsing ────────────────────────────────────────────────


class TestDfmFileParsing:
    """Verify .dfm form file parsing extracts forms and event handlers."""

    def test_basic_form_with_events(self):
        from trustbot.indexing.chunker import _parse_dfm_file

        dfm = (
            "object Form1: TForm1\n"
            "  OnCreate = FormCreate\n"
            "  object Button1: TButton\n"
            "    OnClick = Button1Click\n"
            "  end\n"
            "  object Button2: TButton\n"
            "    OnClick = Button2Click\n"
            "  end\n"
            "end\n"
        )
        chunks = _parse_dfm_file(dfm, "011/src/Unit1.dfm")
        assert len(chunks) == 1
        c = chunks[0]
        assert c.function_name == "Form1"
        assert c.class_name == "TForm1"
        assert c.metadata["is_dfm_form"] is True
        handlers = c.metadata["event_handlers"]
        assert "FormCreate" in handlers
        assert "Button1Click" in handlers
        assert "Button2Click" in handlers

    def test_nested_objects(self):
        from trustbot.indexing.chunker import _parse_dfm_file

        dfm = (
            "object MainForm: TMainForm\n"
            "  object Panel1: TPanel\n"
            "    object SubBtn: TButton\n"
            "      OnClick = SubBtnClick\n"
            "    end\n"
            "  end\n"
            "end\n"
        )
        chunks = _parse_dfm_file(dfm, "proj/Main.dfm")
        assert len(chunks) == 1
        assert "SubBtnClick" in chunks[0].metadata["event_handlers"]

    def test_no_events(self):
        from trustbot.indexing.chunker import _parse_dfm_file

        dfm = (
            "object Form1: TForm1\n"
            "  Caption = 'Hello'\n"
            "end\n"
        )
        chunks = _parse_dfm_file(dfm, "proj/Unit1.dfm")
        assert len(chunks) == 1
        assert chunks[0].metadata["event_handlers"] == []

    def test_empty_dfm(self):
        from trustbot.indexing.chunker import _parse_dfm_file

        chunks = _parse_dfm_file("", "proj/Empty.dfm")
        assert len(chunks) == 0


# ─── DFM form-to-handler edges in call graph builder ─────────────────


class TestDfmCallGraphEdges:
    """Verify call graph builder creates edges from .dfm forms to handlers."""

    def test_dfm_form_creates_edges_to_handlers(self):
        from trustbot.indexing.chunker import CodeChunk
        from trustbot.indexing.call_graph_builder import build_call_graph_from_chunks_sync

        form_chunk = CodeChunk(
            file_path="proj/Unit1.dfm",
            language="delphi",
            function_name="Form1",
            class_name="TForm1",
            line_start=1, line_end=10,
            content="object Form1: TForm1\n  OnClick = Button1Click\nend",
            metadata={"event_handlers": ["Button1Click", "Button2Click"], "is_dfm_form": True},
        )
        handler1 = CodeChunk(
            file_path="proj/Unit1.pas",
            language="delphi",
            function_name="Button1Click",
            class_name="TForm1",
            line_start=10, line_end=20,
            content="procedure TForm1.Button1Click(Sender: TObject);\nbegin\nend;",
        )
        handler2 = CodeChunk(
            file_path="proj/Unit1.pas",
            language="delphi",
            function_name="Button2Click",
            class_name="TForm1",
            line_start=22, line_end=30,
            content="procedure TForm1.Button2Click(Sender: TObject);\nbegin\nend;",
        )
        edges = build_call_graph_from_chunks_sync([form_chunk, handler1, handler2])
        edge_pairs = [(e.from_chunk, e.to_chunk) for e in edges]

        # Form1 should have edges to both handlers
        assert any("Form1" in fc and "Button1Click" in tc for fc, tc in edge_pairs)
        assert any("Form1" in fc and "Button2Click" in tc for fc, tc in edge_pairs)


# ─── Execution order comparison ──────────────────────────────────────


class TestExecutionOrderComparison:
    """Verify that verification detects execution order mismatches."""

    def test_matching_order(self):
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="Root",
            edges=[
                CallGraphEdge(caller="Root", callee="First", execution_order=1),
                CallGraphEdge(caller="Root", callee="Second", execution_order=2),
            ],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="Root",
            edges=[
                CallGraphEdge(caller="Root", callee="First", execution_order=1),
                CallGraphEdge(caller="Root", callee="Second", execution_order=2),
            ],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))
        assert result.metadata["execution_order_matches"] >= 1
        assert len(result.metadata["execution_order_mismatches"]) == 0

    def test_mismatched_order(self):
        neo = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.NEO4J,
            root_function="Root",
            edges=[
                CallGraphEdge(caller="Root", callee="First", execution_order=1),
                CallGraphEdge(caller="Root", callee="Second", execution_order=2),
            ],
        )
        fs = CallGraphOutput(
            execution_flow_id="EF-001",
            source=GraphSource.FILESYSTEM,
            root_function="Root",
            edges=[
                CallGraphEdge(caller="Root", callee="Second", execution_order=1),
                CallGraphEdge(caller="Root", callee="First", execution_order=2),
            ],
        )
        normalizer = NormalizationAgent()
        verifier = VerificationAgent()
        result = verifier.verify(normalizer.normalize(neo), normalizer.normalize(fs))
        mismatches = result.metadata["execution_order_mismatches"]
        assert len(mismatches) == 1
        assert mismatches[0]["caller"] == "ROOT"
