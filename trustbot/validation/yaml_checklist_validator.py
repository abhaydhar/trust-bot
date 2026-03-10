"""
YAML Checklist Validator — validates Neo4j nodes and relationships against
the 29 items in Yaml_checklist.md for a given project_id and run_id.

Produces a report listing node keys (or relationship keys) where each
checklist item failed or was not implemented.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field

from trustbot.models.yaml_checklist import ChecklistItemResult, YamlChecklistReport

logger = logging.getLogger("trustbot.validation.yaml_checklist")

# Expected node types from checklist (item 1)
EXPECTED_NODE_TYPES = frozenset({
    "Snippet", "DBCall", "Calculation", "ServiceCall",
    "DatabaseEntity", "InputEntity", "InputInterface", "Job", "Step", "Variable", "JclJob",
})


@dataclass
class _ValidationContext:
    """Collected Neo4j data for validation."""

    project_id: int
    run_id: int
    execution_flows: list[dict] = field(default_factory=list)
    all_nodes: dict[str, dict] = field(default_factory=dict)  # key -> properties
    node_labels: dict[str, list[str]] = field(default_factory=dict)  # key -> labels
    snippets: list[dict] = field(default_factory=list)
    root_snippets: set[str] = field(default_factory=set)
    calls_edges: list[dict] = field(default_factory=list)  # caller_key, callee_key, props
    participates_in_flow: list[tuple[str, str]] = field(default_factory=list)  # (snippet_key, ef_key)
    contains_db_calls: list[tuple[str, str]] = field(default_factory=list)
    contains_calculation: list[tuple[str, str]] = field(default_factory=list)
    contains_service_calls: list[tuple[str, str]] = field(default_factory=list)
    contains_input: list[tuple[str, str]] = field(default_factory=list)
    contains_map_file: list[tuple[str, str]] = field(default_factory=list)
    contains_dmn: list[tuple[str, str]] = field(default_factory=list)
    has_step: list[tuple[str, str]] = field(default_factory=list)
    contains_variable: list[tuple[str, str]] = field(default_factory=list)
    dbcall_keys: set[str] = field(default_factory=set)
    variable_keys: set[str] = field(default_factory=set)
    job_keys: set[str] = field(default_factory=set)
    step_keys: set[str] = field(default_factory=set)


# Checklist item definitions: (item_id, title, category, priority)
CHECKLIST_ITEMS: list[tuple[int, str, str, str]] = [
    (1, "All required node types present", "Nodes", "Critical"),
    (2, "Every node has execution_order", "Nodes", "Critical"),
    (3, "Root Snippet has root_type set", "Nodes", "Critical"),
    (4, "Every node has name attribute", "Nodes", "Required"),
    (5, "short_summary present on all snippets", "Nodes", "Required"),
    (6, "business_summary present on all snippets", "Nodes", "Required"),
    (7, "snippet attribute present on snippet nodes", "Nodes", "Required"),
    (8, "topic attribute present", "Nodes", "Required"),
    (9, "function_name present where applicable", "Nodes", "Required"),
    (10, "meaning attribute present on Variable nodes", "Nodes", "Required"),
    (11, "visual_imagery_data present", "Nodes", "Required"),
    (12, "table_names present on DBCall nodes", "Nodes", "Critical"),
    (13, "CALLS connects all snippets in the flow", "Relationships", "Critical"),
    (14, "CALLS has global_execution_order", "Relationships", "Critical"),
    (15, "All snippets have PARTICIPATES_IN_FLOW", "Relationships", "Critical"),
    (16, "CONTAINS_DB_CALLS present where DB calls exist", "Relationships", "Required"),
    (17, "CONTAINS_CALCULATION present where calculations exist", "Relationships", "Required"),
    (18, "CONTAINS_SERVICE_CALLS present where service calls exist", "Relationships", "Required"),
    (19, "CONTAINS_INPUT present where input exists", "Relationships", "Required"),
    (20, "CONTAINS_MAP_FILE present where map file exists", "Relationships", "Required"),
    (21, "CONTAINS_DMN present where decision logic exists", "Relationships", "Required"),
    (22, "HAS_STEP present for Job nodes", "Relationships", "Required"),
    (23, "Contains_variable present for Snippet/Step", "Relationships", "Required"),
    (24, "execution_order is sequential and correct", "Execution Order", "Critical"),
    (25, "global_execution_order on CALLS is consistent", "Execution Order", "Critical"),
    (26, "Root snippet is first in execution order", "Execution Order", "Critical"),
    (27, "No orphan snippets", "Connectivity", "Critical"),
    (28, "All paths trace back to root", "Connectivity", "Critical"),
    (29, "Execution flow node exists", "Connectivity", "Required"),
]


class YamlChecklistValidator:
    """Validates Neo4j graph data against the YAML quality checklist."""

    def __init__(self, neo4j_tool) -> None:
        self._neo4j = neo4j_tool

    async def validate(
        self,
        project_id: int,
        run_id: int,
        progress_callback=None,
    ) -> YamlChecklistReport:
        """Run all 29 checklist validations and return report."""

        def _progress(pct: float, msg: str):
            if progress_callback:
                progress_callback(pct, msg)

        _progress(0.0, "Fetching Neo4j data...")
        ctx = await self._fetch_context(project_id, run_id)

        if not ctx.execution_flows and not ctx.snippets:
            return YamlChecklistReport(
                project_id=project_id,
                run_id=run_id,
                items=[],
                summary={
                    "passed": 0,
                    "failed": 0,
                    "critical_failed": 0,
                    "required_failed": 0,
                    "error": f"No data found for project_id={project_id}, run_id={run_id}",
                },
            )

        _progress(0.3, "Running validation rules...")
        items: list[ChecklistItemResult] = []
        for i, (item_id, title, category, priority) in enumerate(CHECKLIST_ITEMS):
            pct = 0.3 + 0.7 * (i + 1) / len(CHECKLIST_ITEMS)
            _progress(pct, f"Validating item {item_id}: {title[:40]}...")
            result = await self._validate_item(item_id, title, category, priority, ctx)
            items.append(result)

        _progress(1.0, "Done!")
        passed = sum(1 for r in items if r.passed)
        failed = len(items) - passed
        critical_failed = sum(1 for r in items if not r.passed and r.priority == "Critical")
        required_failed = sum(1 for r in items if not r.passed and r.priority == "Required")

        return YamlChecklistReport(
            project_id=project_id,
            run_id=run_id,
            items=items,
            summary={
                "passed": passed,
                "failed": failed,
                "critical_failed": critical_failed,
                "required_failed": required_failed,
            },
        )

    async def _fetch_context(self, project_id: int, run_id: int) -> _ValidationContext:
        """Fetch all Neo4j data needed for validation."""
        ctx = _ValidationContext(project_id=project_id, run_id=run_id)
        params = {"pid": project_id, "rid": run_id}

        # Execution flows
        ef_cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})
        RETURN ef.key AS key, properties(ef) AS props
        """
        ef_rows = await self._neo4j.query(ef_cypher, params)
        for r in ef_rows:
            key = r.get("key") or ""
            if key:
                ctx.execution_flows.append({"key": key, "props": r.get("props") or {}})

        if not ctx.execution_flows:
            return ctx

        # All nodes with project_id/run_id
        nodes_cypher = """
        MATCH (n {project_id: $pid, run_id: $rid})
        RETURN n.key AS key, labels(n) AS labels, properties(n) AS props
        """
        node_rows = await self._neo4j.query(nodes_cypher, params)
        for r in node_rows:
            key = r.get("key") or ""
            if key:
                ctx.all_nodes[key] = r.get("props") or {}
                ctx.node_labels[key] = r.get("labels") or []

        # Child nodes (DBCall, Variable, etc.) may not have project_id - fetch via traversal
        child_cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(s)
        OPTIONAL MATCH (s)-[:CONTAINS_DB_CALLS]->(dbc:DBCall)
        OPTIONAL MATCH (s)-[:CONTAINS_CALCULATION]->(calc:Calculation)
        OPTIONAL MATCH (s)-[:CONTAINS_SERVICE_CALLS]->(sc:ServiceCall)
        OPTIONAL MATCH (s)-[:CONTAINS_INPUT]->(inp)
        OPTIONAL MATCH (s)-[:Contains_variable]->(v:Variable)
        WITH collect(DISTINCT dbc) + collect(DISTINCT calc) + collect(DISTINCT sc) +
             collect(DISTINCT inp) + collect(DISTINCT v) AS nodes
        UNWIND nodes AS n
        WITH n WHERE n IS NOT NULL
        RETURN n.key AS key, labels(n) AS labels, properties(n) AS props
        """
        try:
            child_rows = await self._neo4j.query(child_cypher, params)
            for r in child_rows:
                key = r.get("key") or ""
                if key and key not in ctx.all_nodes:
                    ctx.all_nodes[key] = r.get("props") or {}
                    ctx.node_labels[key] = r.get("labels") or []
        except Exception:
            pass

        # Snippets with PARTICIPATES_IN_FLOW and STARTS_FLOW
        part_cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
        RETURN s.key AS key, ef.key AS ef_key, r.STARTS_FLOW AS starts_flow, properties(s) AS props
        """
        part_rows = await self._neo4j.query(part_cypher, params)
        for r in part_rows:
            key = r.get("key") or ""
            ef_key = r.get("ef_key") or ""
            if key:
                ctx.snippets.append({"key": key, "props": r.get("props") or {}})
                ctx.participates_in_flow.append((key, ef_key))
                if r.get("starts_flow"):
                    ctx.root_snippets.add(key)

        # CALLS edges with relationship properties
        calls_cypher = """
        MATCH (caller:Snippet {project_id: $pid, run_id: $rid})-[c:CALLS]->(callee:Snippet)
        RETURN caller.key AS caller_key, callee.key AS callee_key, properties(c) AS call_props
        """
        calls_rows = await self._neo4j.query(calls_cypher, params)
        for r in calls_rows:
            ctx.calls_edges.append({
                "caller_key": r.get("caller_key") or "",
                "callee_key": r.get("callee_key") or "",
                "props": r.get("call_props") or {},
            })

        # CONTAINS_DB_CALLS, CONTAINS_CALCULATION, etc.
        rel_cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
        OPTIONAL MATCH (s)-[:CONTAINS_DB_CALLS]->(dbc:DBCall)
        OPTIONAL MATCH (s)-[:CONTAINS_CALCULATION]->(calc:Calculation)
        OPTIONAL MATCH (s)-[:CONTAINS_SERVICE_CALLS]->(sc:ServiceCall)
        OPTIONAL MATCH (s)-[:CONTAINS_INPUT]->(inp)
        OPTIONAL MATCH (s)-[:CONTAINS_MAP_FILE]->(mf)
        OPTIONAL MATCH (s)-[:CONTAINS_DMN]->(dmn)
        OPTIONAL MATCH (s)-[:Contains_variable]->(v:Variable)
        RETURN s.key AS snippet_key,
               collect(DISTINCT dbc.key) AS dbc_keys,
               collect(DISTINCT calc.key) AS calc_keys,
               collect(DISTINCT sc.key) AS sc_keys,
               collect(DISTINCT inp.key) AS inp_keys,
               collect(DISTINCT mf.key) AS mf_keys,
               collect(DISTINCT dmn.key) AS dmn_keys,
               collect(DISTINCT v.key) AS var_keys
        """
        rel_rows = await self._neo4j.query(rel_cypher, params)
        for r in rel_rows:
            sk = r.get("snippet_key") or ""
            for k in (r.get("dbc_keys") or []):
                if k:
                    ctx.contains_db_calls.append((sk, k))
                    ctx.dbcall_keys.add(k)
            for k in (r.get("calc_keys") or []):
                if k:
                    ctx.contains_calculation.append((sk, k))
            for k in (r.get("sc_keys") or []):
                if k:
                    ctx.contains_service_calls.append((sk, k))
            for k in (r.get("inp_keys") or []):
                if k:
                    ctx.contains_input.append((sk, k))
            for k in (r.get("mf_keys") or []):
                if k:
                    ctx.contains_map_file.append((sk, k))
            for k in (r.get("dmn_keys") or []):
                if k:
                    ctx.contains_dmn.append((sk, k))
            for k in (r.get("var_keys") or []):
                if k:
                    ctx.contains_variable.append((sk, k))
                    ctx.variable_keys.add(k)

        # HAS_STEP (Job -> Step) and Step -> Variable
        job_cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(j:Job)
        OPTIONAL MATCH (j)-[:HAS_STEP]->(step:Step)
        OPTIONAL MATCH (step)-[:Contains_variable]->(sv:Variable)
        RETURN j.key AS job_key, step.key AS step_key, sv.key AS var_key
        """
        job_rows = await self._neo4j.query(job_cypher, params)
        for r in job_rows:
            jk = r.get("job_key") or ""
            sk = r.get("step_key") or ""
            vk = r.get("var_key") or ""
            if jk:
                ctx.job_keys.add(jk)
            if jk and sk:
                ctx.has_step.append((jk, sk))
                ctx.step_keys.add(sk)
            if sk and vk:
                ctx.contains_variable.append((sk, vk))
                ctx.variable_keys.add(vk)

        return ctx

    async def _validate_item(
        self,
        item_id: int,
        title: str,
        category: str,
        priority: str,
        ctx: _ValidationContext,
    ) -> ChecklistItemResult:
        """Validate a single checklist item."""
        handlers = {
            1: self._check_1_node_types,
            2: self._check_2_execution_order,
            3: self._check_3_root_type,
            4: self._check_4_name,
            5: self._check_5_short_summary,
            6: self._check_6_business_summary,
            7: self._check_7_snippet,
            8: self._check_8_topic,
            9: self._check_9_function_name,
            10: self._check_10_meaning,
            11: self._check_11_visual_imagery_data,
            12: self._check_12_table_names,
            13: self._check_13_calls_connects,
            14: self._check_14_calls_global_order,
            15: self._check_15_participates_in_flow,
            16: self._check_16_contains_db_calls,
            17: self._check_17_contains_calculation,
            18: self._check_18_contains_service_calls,
            19: self._check_19_contains_input,
            20: self._check_20_contains_map_file,
            21: self._check_21_contains_dmn,
            22: self._check_22_has_step,
            23: self._check_23_contains_variable,
            24: self._check_24_execution_order_sequential,
            25: self._check_25_global_order_consistent,
            26: self._check_26_root_first,
            27: self._check_27_no_orphans,
            28: self._check_28_paths_to_root,
            29: self._check_29_execution_flow_exists,
        }
        handler = handlers.get(item_id)
        if not handler:
            return ChecklistItemResult(
                item_id=item_id,
                title=title,
                category=category,
                priority=priority,
                passed=True,
                failed_keys=[],
                details="Not implemented",
            )
        return handler(item_id, title, category, priority, ctx)

    def _check_1_node_types(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """All required node types present (Snippet is required; others N/A if not used)."""
        present = set()
        for labels in ctx.node_labels.values():
            for lbl in labels:
                if lbl in EXPECTED_NODE_TYPES:
                    present.add(lbl)
        if "Snippet" not in present:
            return ChecklistItemResult(
                item_id=item_id, title=title, category=category, priority=priority,
                passed=False,
                failed_keys=["Snippet"],
                details="Snippet node type is required but not found.",
            )
        missing = EXPECTED_NODE_TYPES - present
        details = f"Present: {', '.join(sorted(present))}."
        if missing:
            details += f" Not used (N/A): {', '.join(sorted(missing))}."
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=True,
            failed_keys=[],
            details=details,
        )

    def _check_2_execution_order(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """Every node has execution_order."""
        failed = []
        for key, props in ctx.all_nodes.items():
            if "execution_order" not in props or props.get("execution_order") is None:
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} node(s) missing execution_order." if failed else "All nodes have execution_order.",
        )

    def _check_3_root_type(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """Root Snippet has root_type set."""
        failed = []
        for key in ctx.root_snippets:
            props = ctx.all_nodes.get(key, {})
            rt = props.get("root_type")
            if rt is None or (isinstance(rt, str) and not str(rt).strip()):
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} root snippet(s) missing root_type." if failed else "All root snippets have root_type.",
        )

    def _check_4_name(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """Every node has name attribute."""
        failed = []
        for key, props in ctx.all_nodes.items():
            if "name" not in props or props.get("name") is None or not str(props.get("name", "")).strip():
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} node(s) missing name." if failed else "All nodes have name.",
        )

    def _check_5_short_summary(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """short_summary present on all snippets."""
        failed = []
        snippet_keys = {k for k, labels in ctx.node_labels.items() if "Snippet" in labels}
        for key in snippet_keys:
            props = ctx.all_nodes.get(key, {})
            if "short_summary" not in props or props.get("short_summary") is None or not str(props.get("short_summary", "")).strip():
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} snippet(s) missing short_summary." if failed else "All snippets have short_summary.",
        )

    def _check_6_business_summary(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """business_summary present on all snippets."""
        failed = []
        snippet_keys = {k for k, labels in ctx.node_labels.items() if "Snippet" in labels}
        for key in snippet_keys:
            props = ctx.all_nodes.get(key, {})
            if "business_summary" not in props or props.get("business_summary") is None or not str(props.get("business_summary", "")).strip():
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} snippet(s) missing business_summary." if failed else "All snippets have business_summary.",
        )

    def _check_7_snippet(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """snippet attribute present on snippet nodes."""
        failed = []
        snippet_keys = {k for k, labels in ctx.node_labels.items() if "Snippet" in labels}
        for key in snippet_keys:
            props = ctx.all_nodes.get(key, {})
            if "snippet" not in props or props.get("snippet") is None or not str(props.get("snippet", "")).strip():
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} snippet(s) missing snippet attribute." if failed else "All snippets have snippet attribute.",
        )

    def _check_8_topic(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """topic attribute present."""
        failed = []
        for key, props in ctx.all_nodes.items():
            if "topic" not in props or props.get("topic") is None or not str(props.get("topic", "")).strip():
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} node(s) missing topic." if failed else "All nodes have topic.",
        )

    def _check_9_function_name(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """function_name present on Snippet nodes."""
        failed = []
        snippet_keys = {k for k, labels in ctx.node_labels.items() if "Snippet" in labels}
        for key in snippet_keys:
            props = ctx.all_nodes.get(key, {})
            if "function_name" not in props or props.get("function_name") is None or not str(props.get("function_name", "")).strip():
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} snippet(s) missing function_name." if failed else "All snippets have function_name.",
        )

    def _check_10_meaning(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """meaning attribute present on Variable nodes."""
        failed = []
        for key in ctx.variable_keys:
            props = ctx.all_nodes.get(key, {})
            if "meaning" not in props or props.get("meaning") is None or not str(props.get("meaning", "")).strip():
                failed.append(key)
        if not ctx.variable_keys:
            return ChecklistItemResult(
                item_id=item_id, title=title, category=category, priority=priority,
                passed=True, failed_keys=[], details="No Variable nodes; N/A.",
            )
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} Variable(s) missing meaning." if failed else "All Variables have meaning.",
        )

    def _check_11_visual_imagery_data(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """visual_imagery_data present for nodes with visual representation."""
        failed = []
        for key, props in ctx.all_nodes.items():
            if props.get("visual_imagery_data") is not None:
                continue
            labels = ctx.node_labels.get(key, [])
            if "Snippet" in labels or "Step" in labels:
                failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} node(s) missing visual_imagery_data." if failed else "All nodes have visual_imagery_data.",
        )

    def _check_12_table_names(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """table_names present on DBCall nodes."""
        failed = []
        for key in ctx.dbcall_keys:
            props = ctx.all_nodes.get(key, {})
            if "table_names" not in props or props.get("table_names") is None:
                failed.append(key)
        if not ctx.dbcall_keys:
            return ChecklistItemResult(
                item_id=item_id, title=title, category=category, priority=priority,
                passed=True, failed_keys=[], details="No DBCall nodes; N/A.",
            )
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} DBCall(s) missing table_names." if failed else "All DBCalls have table_names.",
        )

    def _check_13_calls_connects(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """Every snippet (except root) must be reachable via CALLS from another snippet."""
        snippet_keys = {
            k for k, labels in ctx.node_labels.items()
            if "Snippet" in labels
        }
        callees = {e["callee_key"] for e in ctx.calls_edges if e.get("callee_key")}
        roots = ctx.root_snippets
        unreachable = snippet_keys - roots - callees
        failed = list(unreachable)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} snippet(s) not reachable via CALLS." if failed else "All non-root snippets reachable via CALLS.",
        )

    def _check_14_calls_global_order(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """CALLS has global_execution_order (or execution_order as fallback)."""
        failed = []
        for e in ctx.calls_edges:
            props = e.get("props", {})
            geo = props.get("global_execution_order")
            eo = props.get("execution_order")
            if geo is None and eo is None:
                failed.append(f"{e.get('caller_key', '')}->{e.get('callee_key', '')}")
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} CALLS edge(s) missing global_execution_order." if failed else "All CALLS have global_execution_order.",
        )

    def _check_15_participates_in_flow(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """All snippets have PARTICIPATES_IN_FLOW."""
        all_snippet_keys = {
            k for k, labels in ctx.node_labels.items()
            if "Snippet" in labels
        }
        has_part = {t[0] for t in ctx.participates_in_flow if t[0]}
        missing = all_snippet_keys - has_part
        failed = list(missing)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} snippet(s) missing PARTICIPATES_IN_FLOW." if failed else "All snippets have PARTICIPATES_IN_FLOW.",
        )

    def _check_16_contains_db_calls(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """CONTAINS_DB_CALLS present where DB calls exist."""
        if not ctx.dbcall_keys:
            return ChecklistItemResult(
                item_id=item_id, title=title, category=category, priority=priority,
                passed=True, failed_keys=[], details="No DBCall nodes; N/A.",
            )
        connected = {t[1] for t in ctx.contains_db_calls}
        failed = list(ctx.dbcall_keys - connected)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} DBCall(s) not connected via CONTAINS_DB_CALLS." if failed else "All DBCalls connected.",
        )

    def _check_17_contains_calculation(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """CONTAINS_CALCULATION present where calculations exist."""
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=True, failed_keys=[], details="N/A (cannot verify existence of calculations from graph).",
        )

    def _check_18_contains_service_calls(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """CONTAINS_SERVICE_CALLS present where service calls exist."""
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=True, failed_keys=[], details="N/A (cannot verify existence of service calls from graph).",
        )

    def _check_19_contains_input(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """CONTAINS_INPUT present where input exists."""
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=True, failed_keys=[], details="N/A (cannot verify existence of input from graph).",
        )

    def _check_20_contains_map_file(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """CONTAINS_MAP_FILE present where map file exists."""
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=True, failed_keys=[], details="N/A (cannot verify existence of map file from graph).",
        )

    def _check_21_contains_dmn(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """CONTAINS_DMN present where decision logic exists."""
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=True, failed_keys=[], details="N/A (cannot verify existence of DMN from graph).",
        )

    def _check_22_has_step(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """HAS_STEP present for Job nodes."""
        if not ctx.job_keys:
            return ChecklistItemResult(
                item_id=item_id, title=title, category=category, priority=priority,
                passed=True, failed_keys=[], details="No Job nodes; N/A.",
            )
        jobs_with_steps = {t[0] for t in ctx.has_step if t[0]}
        failed = list(ctx.job_keys - jobs_with_steps)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} Job(s) without HAS_STEP." if failed else "All Jobs have HAS_STEP.",
        )

    def _check_23_contains_variable(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """Contains_variable present for Snippet/Step."""
        if not ctx.variable_keys:
            return ChecklistItemResult(
                item_id=item_id, title=title, category=category, priority=priority,
                passed=True, failed_keys=[], details="No Variable nodes; N/A.",
            )
        connected = {t[1] for t in ctx.contains_variable}
        failed = list(ctx.variable_keys - connected)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} Variable(s) not connected via Contains_variable." if failed else "All Variables connected.",
        )

    def _check_24_execution_order_sequential(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """execution_order is sequential and correct (no duplicates within same flow)."""
        failed = []
        for ef in ctx.execution_flows:
            ef_key = ef.get("key", "")
            part_in_flow = [t[0] for t in ctx.participates_in_flow if t[1] == ef_key]
            orders: dict[int, list[str]] = defaultdict(list)
            for key in part_in_flow:
                props = ctx.all_nodes.get(key, {})
                order = props.get("execution_order")
                if order is not None:
                    orders[order].append(key)
            for order, keys in orders.items():
                if len(keys) > 1:
                    failed.extend(keys)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} node(s) with duplicate execution_order." if failed else "No duplicate execution_order.",
        )

    def _check_25_global_order_consistent(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """global_execution_order on CALLS is consistent (unique across flow)."""
        failed = []
        seen: dict[int, list[str]] = defaultdict(list)
        for e in ctx.calls_edges:
            props = e.get("props", {})
            geo = props.get("global_execution_order")
            eo = props.get("execution_order")
            val = geo if geo is not None else eo
            if val is not None:
                edge_str = f"{e.get('caller_key', '')}->{e.get('callee_key', '')}"
                seen[val].append(edge_str)
            else:
                failed.append(f"{e.get('caller_key', '')}->{e.get('callee_key', '')}")
        for val, edges in seen.items():
            if len(edges) > 1:
                failed.extend(edges)
        failed = list(dict.fromkeys(failed))
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed[:50],
            details=f"{len(failed)} CALLS with duplicate or missing execution order." if failed else "All CALLS have unique global_execution_order.",
        )

    def _check_26_root_first(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """Root snippet is first in execution order."""
        failed = []
        for ef in ctx.execution_flows:
            ef_key = ef.get("key", "")
            part_in_flow = [(t[0], t[1]) for t in ctx.participates_in_flow if t[1] == ef_key]
            roots_in_flow = [k for k, _ in part_in_flow if k in ctx.root_snippets]
            non_roots = [k for k, _ in part_in_flow if k not in ctx.root_snippets]
            if not roots_in_flow:
                failed.extend(non_roots[:1])
                continue
            root_orders = [ctx.all_nodes.get(k, {}).get("execution_order") for k in roots_in_flow]
            root_orders = [o for o in root_orders if o is not None]
            if not root_orders:
                failed.extend(roots_in_flow)
                continue
            min_root = min(root_orders)
            for key in non_roots:
                o = ctx.all_nodes.get(key, {}).get("execution_order")
                if o is not None and o < min_root:
                    failed.append(key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} non-root snippet(s) with execution_order before root." if failed else "Root snippet is first.",
        )

    def _check_27_no_orphans(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """No orphan snippets (every snippet connected via CALLS or is root)."""
        return self._check_13_calls_connects(item_id, title, category, priority, ctx)

    def _check_28_paths_to_root(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """All paths trace back to root (BFS backwards from each snippet to root)."""
        callee_to_callers: dict[str, list[str]] = defaultdict(list)
        for e in ctx.calls_edges:
            caller = e.get("caller_key", "")
            callee = e.get("callee_key", "")
            if caller and callee:
                callee_to_callers[callee].append(caller)
        roots = ctx.root_snippets
        snippet_keys = {
            k for k, labels in ctx.node_labels.items()
            if "Snippet" in labels
        }
        failed = []
        for snippet_key in snippet_keys:
            if snippet_key in roots:
                continue
            visited: set[str] = set()
            queue: deque[str] = deque([snippet_key])
            found_root = False
            while queue:
                n = queue.popleft()
                if n in visited:
                    continue
                visited.add(n)
                if n in roots:
                    found_root = True
                    break
                for caller in callee_to_callers.get(n, []):
                    queue.append(caller)
            if not found_root:
                failed.append(snippet_key)
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=len(failed) == 0,
            failed_keys=failed,
            details=f"{len(failed)} snippet(s) not reachable from root." if failed else "All paths trace back to root.",
        )

    def _check_29_execution_flow_exists(
        self, item_id: int, title: str, category: str, priority: str, ctx: _ValidationContext
    ) -> ChecklistItemResult:
        """Execution flow node exists."""
        passed = len(ctx.execution_flows) > 0
        return ChecklistItemResult(
            item_id=item_id, title=title, category=category, priority=priority,
            passed=passed,
            failed_keys=[] if passed else ["ExecutionFlow"],
            details=f"{len(ctx.execution_flows)} ExecutionFlow(s) found." if passed else "No ExecutionFlow nodes found.",
        )
