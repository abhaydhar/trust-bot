"""
Tests for the multi-agent validation pipeline.
"""

from __future__ import annotations

import pytest

from trustbot.models.agentic import (
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
    SpecFlowDocument,
)
from trustbot.agents.verification import VerificationAgent


def test_call_graph_output_to_comparable_edges() -> None:
    """CallGraphOutput.to_comparable_edges returns normalized set."""
    output = CallGraphOutput(
        execution_flow_id="EF-001",
        source=GraphSource.NEO4J,
        root_function="main",
        edges=[
            CallGraphEdge(caller="A", callee="B", extraction_method=ExtractionMethod.NEO4J),
            CallGraphEdge(caller="B", callee="C", extraction_method=ExtractionMethod.REGEX),
        ],
    )
    edges = output.to_comparable_edges()
    assert edges == {("A", "B"), ("B", "C")}


def test_verification_agent_confirmed() -> None:
    """Verification agent marks matching edges as confirmed."""
    neo = CallGraphOutput(
        execution_flow_id="EF-001",
        source=GraphSource.NEO4J,
        root_function="A",
        edges=[
            CallGraphEdge(caller="A", callee="B"),
            CallGraphEdge(caller="B", callee="C"),
        ],
    )
    fs = CallGraphOutput(
        execution_flow_id="EF-001",
        source=GraphSource.FILESYSTEM,
        root_function="A",
        edges=[
            CallGraphEdge(caller="A", callee="B"),
            CallGraphEdge(caller="B", callee="C"),
        ],
    )
    agent = VerificationAgent()
    result = agent.verify(neo, fs)
    assert len(result.confirmed_edges) == 2
    assert len(result.phantom_edges) == 0
    assert len(result.missing_edges) == 0
    assert result.flow_trust_score > 0.8


def test_verification_agent_phantom() -> None:
    """Verification agent marks Neo4j-only edges as phantom."""
    neo = CallGraphOutput(
        execution_flow_id="EF-001",
        source=GraphSource.NEO4J,
        root_function="A",
        edges=[
            CallGraphEdge(caller="A", callee="B"),
            CallGraphEdge(caller="A", callee="X"),  # phantom
        ],
    )
    fs = CallGraphOutput(
        execution_flow_id="EF-001",
        source=GraphSource.FILESYSTEM,
        root_function="A",
        edges=[CallGraphEdge(caller="A", callee="B")],
    )
    agent = VerificationAgent()
    result = agent.verify(neo, fs)
    assert len(result.confirmed_edges) == 1
    assert len(result.phantom_edges) == 1
    assert result.phantom_edges[0].callee == "X"


def test_verification_agent_missing() -> None:
    """Verification agent marks filesystem-only edges as missing."""
    neo = CallGraphOutput(
        execution_flow_id="EF-001",
        source=GraphSource.NEO4J,
        root_function="A",
        edges=[CallGraphEdge(caller="A", callee="B")],
    )
    fs = CallGraphOutput(
        execution_flow_id="EF-001",
        source=GraphSource.FILESYSTEM,
        root_function="A",
        edges=[
            CallGraphEdge(caller="A", callee="B"),
            CallGraphEdge(caller="B", callee="Z"),  # missing
        ],
    )
    agent = VerificationAgent()
    result = agent.verify(neo, fs)
    assert len(result.confirmed_edges) == 1
    assert len(result.missing_edges) == 1
    assert result.missing_edges[0].callee == "Z"


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
