"""Tests for data models."""

from trustbot.models.graph import CallEdge, CallGraph, ExecutionFlow, Snippet
from trustbot.models.validation import (
    EdgeStatus,
    EdgeVerdict,
    NodeStatus,
    NodeVerdict,
    ValidationReport,
)


def test_execution_flow_creation():
    ef = ExecutionFlow(key="login-flow", name="User Login", description="Handles login")
    assert ef.key == "login-flow"
    assert ef.name == "User Login"


def test_snippet_creation():
    s = Snippet(
        id="s1",
        function_name="login",
        class_name="AuthService",
        file_path="services/auth_service.py",
        language="python",
        line_start=14,
        line_end=23,
        starts_flow=True,
    )
    assert s.function_name == "login"
    assert s.starts_flow is True


def test_call_graph_operations():
    ef = ExecutionFlow(key="test")
    s1 = Snippet(id="s1", function_name="caller")
    s2 = Snippet(id="s2", function_name="callee")
    edge = CallEdge(caller_id="s1", callee_id="s2")

    graph = CallGraph(
        execution_flow=ef,
        snippets={"s1": s1, "s2": s2},
        edges=[edge],
        entry_points=["s1"],
    )

    assert graph.get_snippet("s1") == s1
    assert graph.get_snippet("nonexistent") is None
    assert len(graph.get_callees_of("s1")) == 1
    assert len(graph.get_callers_of("s2")) == 1
    assert len(graph.get_callers_of("s1")) == 0


def test_validation_report_summary():
    report = ValidationReport(execution_flow_key="test")
    report.node_results = [
        NodeStatus(snippet_id="s1", function_name="a", file_path="a.py", verdict=NodeVerdict.VALID, confidence=0.95),
        NodeStatus(snippet_id="s2", function_name="b", file_path="b.py", verdict=NodeVerdict.DRIFTED, confidence=0.7),
        NodeStatus(snippet_id="s3", function_name="c", file_path="c.py", verdict=NodeVerdict.MISSING, confidence=0.9),
    ]
    report.edge_results = [
        EdgeStatus(caller_id="s1", callee_id="s2", verdict=EdgeVerdict.CONFIRMED, confidence=0.9),
        EdgeStatus(caller_id="s2", callee_id="s3", verdict=EdgeVerdict.CONTRADICTED, confidence=0.85),
    ]

    report.compute_summary()

    assert report.summary.total_nodes == 3
    assert report.summary.valid_nodes == 1
    assert report.summary.drifted_nodes == 1
    assert report.summary.missing_nodes == 1
    assert report.summary.total_edges == 2
    assert report.summary.confirmed_edges == 1
    assert report.summary.contradicted_edges == 1
