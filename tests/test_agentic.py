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
from trustbot.agents.agent2_index import _parse_chunk_id, _extract_func_name


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
