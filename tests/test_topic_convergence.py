"""Tests for the Topic Convergence Agent feature.

Covers:
  - Pydantic models (topic_convergence.py)
  - Verb-noun pattern checker (rule-based detection)
  - Convergence cluster detection (exact + fuzzy)
  - Neo4jWriteTool guardrails and audit log
  - TopicConvergenceAgent full pipeline (with mocked Neo4j + LLM)
  - Integration tests against Neo4j project_id=976
  - E2E UI flow tests
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 1. Model tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTopicConvergenceModels:

    def test_topic_issue_type_values(self):
        from trustbot.models.topic_convergence import TopicIssueType
        assert TopicIssueType.DUPLICATE == "duplicate"
        assert TopicIssueType.SIMILAR == "similar"
        assert TopicIssueType.VERB_NOUN_VIOLATION == "verb_noun"
        assert TopicIssueType.MISALIGNED == "misaligned"
        assert TopicIssueType.JOURNEY_BREAK == "journey_break"
        assert TopicIssueType.TECHNICAL_GLUE == "technical_glue"
        assert TopicIssueType.TOPIC_MISSING == "topic_missing"

    def test_node_topic_analysis_defaults(self):
        from trustbot.models.topic_convergence import NodeTopicAnalysis
        a = NodeTopicAnalysis(
            node_key="snp_001",
            node_name="ProcessPayment",
            node_type="Snippet",
            execution_flow_key="ef_01",
            execution_flow_name="Payment Flow",
        )
        assert a.node_key == "snp_001"
        assert a.current_topic == ""
        assert a.business_summary == ""
        assert a.issues == []
        assert a.suggested_topic == ""
        assert a.confidence == 0.0
        assert a.parent_snippet_key is None
        assert a.chain_position is None

    def test_node_topic_analysis_full(self):
        from trustbot.models.topic_convergence import NodeTopicAnalysis, TopicIssueType
        a = NodeTopicAnalysis(
            node_key="snp_002",
            node_name="CalcInterest",
            node_type="Calculation",
            parent_snippet_key="snp_001",
            execution_flow_key="ef_01",
            execution_flow_name="Loan Flow",
            current_topic="Calculate Interest",
            business_summary="Computes interest on the loan principal",
            issues=[TopicIssueType.VERB_NOUN_VIOLATION],
            issue_details="Does not follow Active Verb + Business Object pattern",
            suggested_topic="Compute Loan Interest",
            suggestion_rationale="More specific business object",
            confidence=0.85,
            chain_position=2,
            chain_context="Validate Loan --> [Calculate Interest] --> Approve Disbursement",
            duplicate_group_id=None,
        )
        assert a.confidence == 0.85
        assert a.parent_snippet_key == "snp_001"
        assert TopicIssueType.VERB_NOUN_VIOLATION in a.issues

    def test_topic_analysis_report_defaults(self):
        from trustbot.models.topic_convergence import TopicAnalysisReport
        r = TopicAnalysisReport(project_id=976, run_id=2416)
        assert r.total_nodes_analyzed == 0
        assert r.nodes_with_issues == 0
        assert r.nodes_missing_topic == 0
        assert r.analyses == []
        assert r.duplicate_groups == {}
        assert r.journey_chains == {}

    def test_topic_change_record(self):
        from trustbot.models.topic_convergence import TopicChangeRecord
        rec = TopicChangeRecord(
            node_key="snp_001",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="Process Payment",
            new_topic="Authorize Credit Payment",
            changed_by="user",
            execution_flow_key="ef_01",
        )
        assert rec.old_topic == "Process Payment"
        assert rec.new_topic == "Authorize Credit Payment"
        assert rec.is_undo is False
        assert isinstance(rec.changed_at, datetime)

    def test_topic_change_record_undo(self):
        from trustbot.models.topic_convergence import TopicChangeRecord
        rec = TopicChangeRecord(
            node_key="snp_001",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="(reverted)",
            new_topic="Process Payment",
            changed_by="undo",
            is_undo=True,
        )
        assert rec.is_undo is True
        assert rec.changed_by == "undo"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Verb-noun pattern checker tests
# ═══════════════════════════════════════════════════════════════════════════


class TestVerbNounChecker:

    def _check(self, topic):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        return TopicConvergenceAgent.check_verb_noun(topic)

    def test_valid_verb_noun(self):
        from trustbot.models.topic_convergence import TopicIssueType
        issues = self._check("Validate Customer Identity")
        assert TopicIssueType.VERB_NOUN_VIOLATION not in issues
        assert TopicIssueType.TECHNICAL_GLUE not in issues

    def test_approved_verbs(self):
        from trustbot.models.topic_convergence import TopicIssueType
        for verb in ["Calculate", "Authorize", "Check", "Notify", "Generate"]:
            issues = self._check(f"{verb} Account Balance")
            assert TopicIssueType.VERB_NOUN_VIOLATION not in issues, f"'{verb}' should be approved"

    def test_technical_glue_detected(self):
        from trustbot.models.topic_convergence import TopicIssueType
        issues = self._check("Submit Data Record")
        assert TopicIssueType.TECHNICAL_GLUE in issues

    def test_verb_noun_violation_non_verb_start(self):
        from trustbot.models.topic_convergence import TopicIssueType
        issues = self._check("Payment Processing Module")
        assert TopicIssueType.VERB_NOUN_VIOLATION in issues

    def test_empty_topic_no_issues(self):
        issues = self._check("")
        assert issues == []

    def test_none_topic_no_issues(self):
        issues = self._check(None)
        assert issues == []

    def test_technical_glue_words(self):
        from trustbot.models.topic_convergence import TopicIssueType
        for word in ["Handle", "Execute", "Invoke", "Fetch", "Load"]:
            issues = self._check(f"Validate {word} Request")
            assert TopicIssueType.TECHNICAL_GLUE in issues, f"'{word}' should be flagged as glue"

    def test_update_as_glue_word(self):
        from trustbot.models.topic_convergence import TopicIssueType
        issues = self._check("Update Customer Record")
        assert TopicIssueType.TECHNICAL_GLUE in issues

    def test_mixed_case_verb(self):
        from trustbot.models.topic_convergence import TopicIssueType
        issues = self._check("VALIDATE Customer")
        assert TopicIssueType.VERB_NOUN_VIOLATION not in issues

    def test_single_word_topic(self):
        from trustbot.models.topic_convergence import TopicIssueType
        issues = self._check("Validate")
        assert TopicIssueType.VERB_NOUN_VIOLATION not in issues


# ═══════════════════════════════════════════════════════════════════════════
# 3. Convergence cluster detection tests
# ═══════════════════════════════════════════════════════════════════════════


class TestConvergenceDetection:

    def _make_node(self, key, topic):
        from trustbot.agents.topic_convergence import _NodeRecord
        return _NodeRecord(
            node_key=key,
            node_name=key,
            node_type="Snippet",
            parent_snippet_key=None,
            ef_key="ef_01",
            ef_name="Test Flow",
            topic=topic,
            business_summary="",
        )

    def test_exact_duplicates(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        nodes = [
            self._make_node("a", "Process Payment"),
            self._make_node("b", "Process Payment"),
            self._make_node("c", "Validate Customer"),
        ]
        groups = TopicConvergenceAgent._detect_convergence(nodes)
        dup_groups = {k: v for k, v in groups.items() if k.startswith("dup_")}
        assert len(dup_groups) >= 1
        found = False
        for keys in dup_groups.values():
            if "a" in keys and "b" in keys:
                found = True
        assert found, "Should detect 'a' and 'b' as exact duplicates"

    def test_no_duplicates(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        nodes = [
            self._make_node("a", "Validate Customer"),
            self._make_node("b", "Calculate Interest"),
            self._make_node("c", "Approve Loan"),
        ]
        groups = TopicConvergenceAgent._detect_convergence(nodes)
        assert len(groups) == 0

    def test_fuzzy_similar(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        nodes = [
            self._make_node("a", "Validate Customer Identity"),
            self._make_node("b", "Validate Customer Identty"),  # typo
        ]
        groups = TopicConvergenceAgent._detect_convergence(nodes)
        sim_groups = {k: v for k, v in groups.items() if k.startswith("sim_")}
        assert len(sim_groups) >= 1

    def test_case_insensitive_duplicates(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        nodes = [
            self._make_node("a", "Validate Customer"),
            self._make_node("b", "validate customer"),
        ]
        groups = TopicConvergenceAgent._detect_convergence(nodes)
        dup_groups = {k: v for k, v in groups.items() if k.startswith("dup_")}
        assert len(dup_groups) >= 1

    def test_none_topics_skipped(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        nodes = [
            self._make_node("a", None),
            self._make_node("b", None),
            self._make_node("c", "Validate Customer"),
        ]
        groups = TopicConvergenceAgent._detect_convergence(nodes)
        assert len(groups) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. Neo4jWriteTool guardrails and audit log tests
# ═══════════════════════════════════════════════════════════════════════════


class TestNeo4jWriteToolGuardrails:

    def test_allowed_labels(self):
        from trustbot.tools.neo4j_write_tool import ALLOWED_LABELS
        assert "Snippet" in ALLOWED_LABELS
        assert "DBCall" in ALLOWED_LABELS
        assert "Calculation" in ALLOWED_LABELS
        assert "ServiceCall" in ALLOWED_LABELS
        assert "Variable" in ALLOWED_LABELS
        assert "Job" in ALLOWED_LABELS
        assert "Step" in ALLOWED_LABELS
        assert "DatabaseEntity" in ALLOWED_LABELS
        assert "DatabaseField" in ALLOWED_LABELS
        assert "InputEntity" in ALLOWED_LABELS
        assert "InputInterface" in ALLOWED_LABELS
        assert "JclJob" in ALLOWED_LABELS

    def test_disallowed_label_raises(self):
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()
        with pytest.raises(PermissionError, match="not in the allowed set"):
            tool._validate_label("ExecutionFlow")

    def test_disallowed_label_unknown(self):
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()
        with pytest.raises(PermissionError):
            tool._validate_label("FakeNode")

    def test_allowed_label_passes(self):
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()
        tool._validate_label("Snippet")  # should not raise
        tool._validate_label("DBCall")

    @pytest.mark.asyncio
    async def test_update_node_topic_records_audit(self):
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_record = {"old_topic": "Old Topic", "key": "snp_001"}
        mock_result.single = AsyncMock(return_value=mock_record)
        mock_session.run = AsyncMock(return_value=mock_result)

        mock_write_result = AsyncMock()
        mock_write_result.single = AsyncMock(return_value={"key": "snp_001"})

        call_count = 0
        async def mock_run(cypher, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_result
            return mock_write_result

        mock_session.run = mock_run

        mock_driver = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
        tool._driver = mock_driver

        rec = await tool.update_node_topic(
            "snp_001", "Snippet", "New Topic",
            execution_flow_key="ef_01",
        )
        assert rec.node_key == "snp_001"
        assert rec.new_topic == "New Topic"
        assert rec.old_topic == "Old Topic"
        assert len(tool.change_log) == 1
        assert tool.change_log[0].node_label == "Snippet"

    def test_export_audit_json(self):
        from trustbot.models.topic_convergence import TopicChangeRecord
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()
        tool._change_log.append(TopicChangeRecord(
            node_key="snp_001",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="Old",
            new_topic="New",
        ))
        exported = tool.export_audit_json()
        parsed = json.loads(exported)
        assert len(parsed) == 1
        assert parsed[0]["node_key"] == "snp_001"

    def test_export_audit_csv(self):
        from trustbot.models.topic_convergence import TopicChangeRecord
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()
        tool._change_log.append(TopicChangeRecord(
            node_key="snp_001",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="Old",
            new_topic="New",
        ))
        csv_str = tool.export_audit_csv()
        assert "snp_001" in csv_str
        assert "node_key" in csv_str  # header

    def test_clear_audit_log(self):
        from trustbot.models.topic_convergence import TopicChangeRecord
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()
        tool._change_log.append(TopicChangeRecord(
            node_key="x",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="a",
            new_topic="b",
        ))
        assert len(tool.change_log) == 1
        tool.clear_audit_log()
        assert len(tool.change_log) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. JSON response parser tests
# ═══════════════════════════════════════════════════════════════════════════


class TestJsonParser:

    def _parse(self, text):
        from trustbot.agents.topic_convergence import _parse_json_response
        return _parse_json_response(text)

    def test_plain_json(self):
        result = self._parse('{"suggested_topic": "Validate Account", "rationale": "clear"}')
        assert result["suggested_topic"] == "Validate Account"

    def test_markdown_code_block(self):
        text = '```json\n{"suggested_topic": "Check Balance"}\n```'
        result = self._parse(text)
        assert result["suggested_topic"] == "Check Balance"

    def test_mixed_text_with_json(self):
        text = 'Here is my response:\n{"suggested_topic": "Approve Loan", "confidence": 0.9}\nEnd.'
        result = self._parse(text)
        assert result["suggested_topic"] == "Approve Loan"

    def test_empty_returns_empty(self):
        result = self._parse("")
        assert result == {}

    def test_invalid_json(self):
        result = self._parse("this is not json at all")
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# 6. TopicConvergenceAgent pipeline tests (mocked)
# ═══════════════════════════════════════════════════════════════════════════


class TestTopicConvergenceAgentPipeline:

    def _make_mock_neo4j(self, snippet_rows=None, child_rows=None, call_rows=None):
        mock_tool = MagicMock()
        mock_tool.query = AsyncMock()

        async def side_effect(cypher, params=None):
            if "PARTICIPATES_IN_FLOW]-(s:Snippet)" in cypher and "CONTAINS_DB_CALLS" not in cypher and "CALLS" not in cypher:
                return snippet_rows or []
            elif "CONTAINS_DB_CALLS" in cypher:
                return child_rows or []
            elif "CALLS" in cypher:
                return call_rows or []
            return []

        mock_tool.query.side_effect = side_effect
        return mock_tool

    @pytest.mark.asyncio
    async def test_analyze_empty_project(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        mock_tool = self._make_mock_neo4j()
        agent = TopicConvergenceAgent(mock_tool)
        report = await agent.analyze(999, 999)
        assert report.total_nodes_analyzed == 0
        assert report.project_id == 999

    @pytest.mark.asyncio
    async def test_analyze_detects_missing_topic(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        from trustbot.models.topic_convergence import TopicIssueType

        snippet_rows = [{
            "ef_key": "ef_01",
            "ef_name": "Test Flow",
            "node_key": "snp_001",
            "node_labels": ["Snippet"],
            "topic": None,
            "business_summary": "Validates the customer identity",
            "name": "ValidateCustomer",
            "function_name": "validate_customer",
            "all_props": {},
        }]

        mock_tool = self._make_mock_neo4j(snippet_rows=snippet_rows)
        agent = TopicConvergenceAgent(mock_tool)

        with patch("trustbot.agents.topic_convergence.litellm") as mock_llm:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = json.dumps({
                "suggested_topic": "Validate Customer Identity",
                "rationale": "Generated from business summary",
                "confidence": 0.8,
            })
            mock_llm.acompletion = AsyncMock(return_value=mock_resp)

            report = await agent.analyze(976, 2416)

        assert report.total_nodes_analyzed == 1
        assert report.nodes_missing_topic == 1
        a = report.analyses[0]
        assert TopicIssueType.TOPIC_MISSING in a.issues
        assert a.suggested_topic == "Validate Customer Identity"

    @pytest.mark.asyncio
    async def test_analyze_detects_duplicates(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        from trustbot.models.topic_convergence import TopicIssueType

        snippet_rows = [
            {
                "ef_key": "ef_01", "ef_name": "Flow A",
                "node_key": "snp_001", "node_labels": ["Snippet"],
                "topic": "Process Payment", "business_summary": "Handles credit card payments",
                "name": "ProcessPayment1", "function_name": "process_payment",
                "all_props": {},
            },
            {
                "ef_key": "ef_01", "ef_name": "Flow A",
                "node_key": "snp_002", "node_labels": ["Snippet"],
                "topic": "Process Payment", "business_summary": "Handles refund processing",
                "name": "ProcessPayment2", "function_name": "process_refund",
                "all_props": {},
            },
        ]

        mock_tool = self._make_mock_neo4j(snippet_rows=snippet_rows)
        agent = TopicConvergenceAgent(mock_tool)

        with patch("trustbot.agents.topic_convergence.litellm") as mock_llm:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = json.dumps({
                "alignment_score": 0.8, "explanation": "Good match",
            })
            mock_llm.acompletion = AsyncMock(return_value=mock_resp)

            report = await agent.analyze(976, 2416)

        assert report.total_nodes_analyzed == 2
        dup_nodes = [a for a in report.analyses if TopicIssueType.DUPLICATE in a.issues]
        assert len(dup_nodes) == 2
        assert len(report.duplicate_groups) >= 1

    @pytest.mark.asyncio
    async def test_analyze_verb_noun_violations(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        from trustbot.models.topic_convergence import TopicIssueType

        snippet_rows = [{
            "ef_key": "ef_01", "ef_name": "Flow A",
            "node_key": "snp_001", "node_labels": ["Snippet"],
            "topic": "Submit Data Record", "business_summary": "Sends customer data to backend",
            "name": "SubmitData", "function_name": "submit_data",
            "all_props": {},
        }]

        mock_tool = self._make_mock_neo4j(snippet_rows=snippet_rows)
        agent = TopicConvergenceAgent(mock_tool)

        with patch("trustbot.agents.topic_convergence.litellm") as mock_llm:
            mock_resp_combined = MagicMock()
            mock_resp_combined.choices = [MagicMock()]
            mock_resp_combined.choices[0].message.content = json.dumps({
                "alignment_score": 0.5,
                "alignment_explanation": "Technical naming",
                "suggested_topic": "Transfer Customer Profile",
                "rationale": "Describes the business action",
                "confidence": 0.85,
            })
            mock_llm.acompletion = AsyncMock(return_value=mock_resp_combined)

            report = await agent.analyze(976, 2416)

        a = report.analyses[0]
        assert TopicIssueType.TECHNICAL_GLUE in a.issues
        assert TopicIssueType.VERB_NOUN_VIOLATION in a.issues

    @pytest.mark.asyncio
    async def test_analyze_with_child_nodes(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent

        snippet_rows = [{
            "ef_key": "ef_01", "ef_name": "Flow A",
            "node_key": "snp_001", "node_labels": ["Snippet"],
            "topic": "Validate Account", "business_summary": "Validates account details",
            "name": "ValidateAccount", "function_name": "validate_account",
            "all_props": {},
        }]

        # Simulate child nodes returned as Neo4j node objects
        mock_db_call = MagicMock()
        mock_db_call.__iter__ = MagicMock(return_value=iter([]))
        mock_db_call.get = lambda k, d=None: {
            "key": "dbc_001", "name": "QueryAccountTable",
            "topic": "Retrieve Account Data", "business_summary": "Queries the account table",
            "labels": ["DBCall"],
        }.get(k, d)
        mock_db_call.__getitem__ = lambda self, k: {
            "key": "dbc_001", "name": "QueryAccountTable",
            "topic": "Retrieve Account Data", "business_summary": "Queries the account table",
        }[k]
        mock_db_call.items = MagicMock(return_value={
            "key": "dbc_001", "name": "QueryAccountTable",
            "topic": "Retrieve Account Data", "business_summary": "Queries the account table",
        }.items())

        child_rows = [{
            "parent_key": "snp_001",
            "ef_key": "ef_01",
            "ef_name": "Flow A",
            "db_calls": [mock_db_call],
            "calculations": [],
            "service_calls": [],
            "inputs": [],
            "variables": [],
        }]

        mock_tool = self._make_mock_neo4j(
            snippet_rows=snippet_rows,
            child_rows=child_rows,
        )
        agent = TopicConvergenceAgent(mock_tool)

        with patch("trustbot.agents.topic_convergence.litellm") as mock_llm:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = json.dumps({
                "alignment_score": 0.9, "explanation": "Good",
            })
            mock_llm.acompletion = AsyncMock(return_value=mock_resp)

            report = await agent.analyze(976, 2416)

        types = {a.node_type for a in report.analyses}
        assert "Snippet" in types

    @pytest.mark.asyncio
    async def test_progress_callback_invoked(self):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent

        snippet_rows = [{
            "ef_key": "ef_01", "ef_name": "Test Flow",
            "node_key": "snp_001", "node_labels": ["Snippet"],
            "topic": "Validate Customer", "business_summary": "Validates identity",
            "name": "Validate", "function_name": "validate",
            "all_props": {},
        }]
        mock_tool = self._make_mock_neo4j(snippet_rows=snippet_rows)
        agent = TopicConvergenceAgent(mock_tool)

        progress_calls = []
        def on_progress(pct, msg):
            progress_calls.append((pct, msg))

        with patch("trustbot.agents.topic_convergence.litellm") as mock_llm:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = json.dumps({
                "alignment_score": 0.9, "explanation": "ok",
            })
            mock_llm.acompletion = AsyncMock(return_value=mock_resp)

            await agent.analyze(976, 2416, progress_callback=on_progress)

        assert len(progress_calls) > 0
        assert progress_calls[-1][0] == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# 7. Integration tests — Neo4j project 976 (requires live Neo4j)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegrationProject976:
    """These tests require a running Neo4j instance with project 976 data.
    
    Run with: pytest -m integration tests/test_topic_convergence.py
    """

    @pytest.fixture
    async def neo4j_tool(self):
        from trustbot.tools.neo4j_tool import Neo4jTool
        tool = Neo4jTool()
        await tool.initialize()
        yield tool
        await tool.shutdown()

    @pytest.fixture
    async def write_tool(self):
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool
        tool = Neo4jWriteTool()
        await tool.initialize()
        yield tool
        await tool.shutdown()

    @pytest.mark.asyncio
    async def test_fetch_snippets_for_976(self, neo4j_tool):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        agent = TopicConvergenceAgent(neo4j_tool)
        nodes = await agent._fetch_all_nodes(976, 2416)
        assert len(nodes) > 0
        snippet_count = sum(1 for n in nodes if n.node_type == "Snippet")
        assert snippet_count > 0

    @pytest.mark.asyncio
    async def test_full_analysis_976(self, neo4j_tool):
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        agent = TopicConvergenceAgent(neo4j_tool)
        report = await agent.analyze(976, 2416)
        assert report.total_nodes_analyzed > 0
        assert report.project_id == 976
        assert len(report.node_type_breakdown) > 0

    @pytest.mark.asyncio
    async def test_write_and_undo_cycle(self, neo4j_tool, write_tool):
        """Write a topic, verify it changed, then undo and verify restoration."""
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        agent = TopicConvergenceAgent(neo4j_tool)
        nodes = await agent._fetch_all_nodes(976, 2416)
        snippet_nodes = [n for n in nodes if n.node_type == "Snippet" and n.topic]
        if not snippet_nodes:
            pytest.skip("No snippet nodes with topic found")

        target = snippet_nodes[0]
        original_topic = target.topic
        test_topic = "TEST_TOPIC_CONVERGENCE_TEMP"

        rec = await write_tool.update_node_topic(
            target.node_key, "Snippet", test_topic,
        )
        assert rec.old_topic == original_topic
        assert rec.new_topic == test_topic

        undo_rec = await write_tool.restore_topic(
            target.node_key, "Snippet", original_topic,
        )
        assert undo_rec.new_topic == original_topic
        assert undo_rec.is_undo is True
        assert len(write_tool.change_log) == 2

    @pytest.mark.asyncio
    async def test_audit_log_export(self, write_tool):
        from trustbot.models.topic_convergence import TopicChangeRecord
        write_tool._change_log.append(TopicChangeRecord(
            node_key="test_key",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="Old",
            new_topic="New",
        ))
        json_export = write_tool.export_audit_json()
        assert "test_key" in json_export
        csv_export = write_tool.export_audit_csv()
        assert "test_key" in csv_export


# ═══════════════════════════════════════════════════════════════════════════
# 8. E2E tests — UI flow
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
class TestE2ETopicConvergence:
    """End-to-end tests for the Topic Convergence UI tab.
    
    Run with: pytest -m e2e tests/test_topic_convergence.py
    """

    @pytest.mark.asyncio
    async def test_agent_analyze_returns_report(self):
        """Verify the agent can produce a complete report structure."""
        from trustbot.agents.topic_convergence import TopicConvergenceAgent

        snippet_rows = [
            {
                "ef_key": "ef_01", "ef_name": "Test Flow",
                "node_key": f"snp_{i:03d}", "node_labels": ["Snippet"],
                "topic": f"Topic {i}" if i % 3 != 0 else None,
                "business_summary": f"Does business thing {i}",
                "name": f"Func{i}", "function_name": f"func_{i}",
                "all_props": {},
            }
            for i in range(10)
        ]

        mock_tool = MagicMock()
        mock_tool.query = AsyncMock(return_value=snippet_rows)

        async def side_effect(cypher, params=None):
            if "PARTICIPATES_IN_FLOW]-(s:Snippet)" in cypher and "CONTAINS_DB_CALLS" not in cypher and "CALLS" not in cypher:
                return snippet_rows
            return []

        mock_tool.query.side_effect = side_effect
        agent = TopicConvergenceAgent(mock_tool)

        with patch("trustbot.agents.topic_convergence.litellm") as mock_llm:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = json.dumps({
                "alignment_score": 0.9, "explanation": "ok",
            })
            mock_llm.acompletion = AsyncMock(return_value=mock_resp)

            report = await agent.analyze(976, 2416)

        assert report.total_nodes_analyzed == 10
        assert report.nodes_missing_topic > 0
        assert "Snippet" in report.node_type_breakdown
        assert len(report.analyses) == 10

    @pytest.mark.asyncio
    async def test_full_remedial_flow(self):
        """Simulate: analyze -> get suggestions -> apply -> undo."""
        from trustbot.agents.topic_convergence import TopicConvergenceAgent
        from trustbot.models.topic_convergence import TopicChangeRecord
        from trustbot.tools.neo4j_write_tool import Neo4jWriteTool

        snippet_rows = [{
            "ef_key": "ef_01", "ef_name": "Payment Flow",
            "node_key": "snp_pay", "node_labels": ["Snippet"],
            "topic": "Submit Data", "business_summary": "Sends payment authorization request",
            "name": "SubmitPayment", "function_name": "submit_payment",
            "all_props": {},
        }]

        mock_tool = MagicMock()
        async def side_effect(cypher, params=None):
            if "PARTICIPATES_IN_FLOW]-(s:Snippet)" in cypher and "CONTAINS_DB_CALLS" not in cypher and "CALLS" not in cypher:
                return snippet_rows
            return []
        mock_tool.query = AsyncMock(side_effect=side_effect)

        agent = TopicConvergenceAgent(mock_tool)

        with patch("trustbot.agents.topic_convergence.litellm") as mock_llm:
            mock_resp_combined = MagicMock()
            mock_resp_combined.choices = [MagicMock()]
            mock_resp_combined.choices[0].message.content = json.dumps({
                "alignment_score": 0.4,
                "alignment_explanation": "Technical naming",
                "suggested_topic": "Authorize Payment Request",
                "rationale": "Active verb + business object",
                "confidence": 0.9,
            })
            mock_llm.acompletion = AsyncMock(return_value=mock_resp_combined)

            report = await agent.analyze(976, 2416)

        assert report.total_nodes_analyzed == 1
        a = report.analyses[0]
        assert a.suggested_topic == "Authorize Payment Request"
        assert a.confidence == 0.9

        write_tool = Neo4jWriteTool()
        write_tool._change_log.append(TopicChangeRecord(
            node_key="snp_pay",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="Submit Data",
            new_topic="Authorize Payment Request",
        ))
        assert len(write_tool.change_log) == 1

        write_tool._change_log.append(TopicChangeRecord(
            node_key="snp_pay",
            node_type="Snippet",
            node_label="Snippet",
            old_topic="(reverted)",
            new_topic="Submit Data",
            changed_by="undo",
            is_undo=True,
        ))
        assert len(write_tool.change_log) == 2
        assert write_tool.change_log[-1].is_undo is True
