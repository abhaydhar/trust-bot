"""Topic Convergence Agent.

Analyzes Neo4j node topics across all node types for:
  1. Convergence / duplication (exact + fuzzy)
  2. Verb-noun naming convention violations
  3. Topic vs business_summary misalignment
  4. Customer journey chain coherence
  5. LLM-driven remedial suggestions

Performance optimisations:
  - Neo4j fetches run in parallel (asyncio.gather)
  - Alignment + remedial merged into one LLM call per flagged node
  - Journey chain validations parallelised across flows
  - Fuzzy similarity uses early-exit length pruning
  - Concurrency governed by a single shared semaphore
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

import litellm

from trustbot.config import settings
from trustbot.prompts import get_prompt
from trustbot.models.topic_convergence import (
    NodeTopicAnalysis,
    TopicAnalysisReport,
    TopicIssueType,
)
from trustbot.tools.neo4j_tool import Neo4jTool

logger = logging.getLogger("trustbot.agents.topic_convergence")

# ------------------------------------------------------------------
# Constants for rule-based checks
# ------------------------------------------------------------------

TECHNICAL_GLUE_WORDS = frozenset({
    "submit", "data", "record", "update", "process",
    "handle", "execute", "run", "call", "invoke",
    "fetch", "load", "save", "store", "get", "set",
    "push", "pull", "send", "receive", "trigger",
})

APPROVED_VERBS = frozenset({
    "validate", "verify", "calculate", "authorize", "check",
    "adjust", "notify", "generate", "approve", "reject",
    "route", "classify", "determine", "evaluate", "assess",
    "retrieve", "identify", "confirm", "allocate", "assign",
    "resolve", "initiate", "complete", "cancel", "suspend",
    "transfer", "reconcile", "aggregate", "transform", "enrich",
    "publish", "archive", "audit", "monitor", "schedule",
    "dispatch", "escalate", "review", "certify", "register",
    "deactivate", "activate", "merge", "split", "encode",
    "decode", "authenticate", "renew", "close", "open",
    "create", "delete", "apply", "compute", "derive",
    "map", "filter", "sort", "format", "parse",
    "convert", "extract", "inspect", "detect", "flag",
})

FUZZY_SIMILARITY_THRESHOLD = 0.85
ALIGNMENT_THRESHOLD = 0.7
MAX_CONCURRENT_LLM = 3
LLM_BATCH_SIZE = 3
LLM_BATCH_DELAY = 0.5  # seconds between batches to avoid rate-limit bursts


# ------------------------------------------------------------------
# Lightweight internal record used during fetching
# ------------------------------------------------------------------

@dataclass
class _NodeRecord:
    node_key: str
    node_name: str
    node_type: str
    parent_snippet_key: str | None
    ef_key: str
    ef_name: str
    topic: str | None
    business_summary: str
    function_name: str = ""
    all_properties: dict = field(default_factory=dict)


@dataclass
class _FlowTree:
    """Call tree for one execution flow — preserves parent-child structure."""
    ordered: list[str]
    adjacency: dict[str, list[str]]
    parent_of: dict[str, str | None] = field(default_factory=dict)

    def children(self, key: str) -> list[str]:
        return self.adjacency.get(key, [])

    def parent(self, key: str) -> str | None:
        return self.parent_of.get(key)

    def siblings(self, key: str) -> list[str]:
        p = self.parent(key)
        if p is None:
            return []
        return [c for c in self.children(p) if c != key]


# ------------------------------------------------------------------
# Agent
# ------------------------------------------------------------------


class TopicConvergenceAgent:
    """Stateless agent that analyses topic quality for a project run."""

    def __init__(self, neo4j_tool: Neo4jTool) -> None:
        self._neo4j = neo4j_tool

    # ==================================================================
    # STEP 1 — Fetch all nodes via traversal (PARALLEL)
    # ==================================================================

    async def _fetch_all_nodes(self, project_id: int, run_id: int) -> list[_NodeRecord]:
        nodes: dict[str, _NodeRecord] = {}

        snippet_nodes: dict[str, _NodeRecord] = {}
        child_nodes: dict[str, _NodeRecord] = {}
        job_nodes: dict[str, _NodeRecord] = {}
        db_nodes: dict[str, _NodeRecord] = {}

        logger.info("[fetch] Starting 4 parallel Neo4j queries for project=%d run=%d", project_id, run_id)
        await asyncio.gather(
            self._fetch_snippets(project_id, run_id, snippet_nodes),
            self._fetch_snippet_children(project_id, run_id, child_nodes),
            self._fetch_jobs_steps(project_id, run_id, job_nodes),
            self._fetch_database_entities(project_id, run_id, db_nodes),
        )
        logger.info(
            "[fetch] Query results: snippets=%d, children=%d, jobs=%d, db_entities=%d",
            len(snippet_nodes), len(child_nodes), len(job_nodes), len(db_nodes),
        )

        for d in (snippet_nodes, child_nodes, job_nodes, db_nodes):
            for k, v in d.items():
                if k not in nodes:
                    nodes[k] = v

        logger.info("[fetch] Fetched %d unique nodes for project=%d run=%d", len(nodes), project_id, run_id)
        return list(nodes.values())

    async def _fetch_snippets(self, pid: int, rid: int, out: dict[str, _NodeRecord]) -> None:
        cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
        RETURN ef.key AS ef_key, ef.name AS ef_name,
               s.key AS node_key, labels(s) AS node_labels,
               s.topic AS topic, s.business_summary AS business_summary,
               s.name AS name, s.function_name AS function_name,
               properties(s) AS all_props
        """
        rows = await self._neo4j.query(cypher, {"pid": pid, "rid": rid})
        for r in rows:
            key = r.get("node_key") or ""
            if not key or key in out:
                continue
            labels = r.get("node_labels") or []
            node_type = _pick_label(labels, "Snippet")
            out[key] = _NodeRecord(
                node_key=key,
                node_name=r.get("name") or "",
                node_type=node_type,
                parent_snippet_key=None,
                ef_key=r.get("ef_key") or "",
                ef_name=r.get("ef_name") or "",
                topic=r.get("topic"),
                business_summary=r.get("business_summary") or "",
                function_name=r.get("function_name") or "",
                all_properties=r.get("all_props") or {},
            )

    async def _fetch_snippet_children(self, pid: int, rid: int, out: dict[str, _NodeRecord]) -> None:
        cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
        OPTIONAL MATCH (s)-[:CONTAINS_DB_CALLS]->(dbc:DBCall)
        OPTIONAL MATCH (s)-[:CONTAINS_CALCULATION]->(calc:Calculation)
        OPTIONAL MATCH (s)-[:CONTAINS_SERVICE_CALLS]->(sc:ServiceCall)
        OPTIONAL MATCH (s)-[:CONTAINS_INPUT]->(inp)
          WHERE inp:InputEntity OR inp:InputInterface
        OPTIONAL MATCH (s)-[:Contains_variable]->(v:Variable)
        WITH s, ef,
             collect(DISTINCT dbc) AS db_calls,
             collect(DISTINCT calc) AS calculations,
             collect(DISTINCT sc) AS service_calls,
             collect(DISTINCT inp) AS inputs,
             collect(DISTINCT v) AS variables
        RETURN s.key AS parent_key, ef.key AS ef_key, ef.name AS ef_name,
               db_calls, calculations, service_calls, inputs, variables
        """
        rows = await self._neo4j.query(cypher, {"pid": pid, "rid": rid})
        for r in rows:
            parent_key = r.get("parent_key") or ""
            ef_key = r.get("ef_key") or ""
            ef_name = r.get("ef_name") or ""
            for child_node in (r.get("db_calls") or []):
                self._add_child_node(child_node, "DBCall", parent_key, ef_key, ef_name, out)
            for child_node in (r.get("calculations") or []):
                self._add_child_node(child_node, "Calculation", parent_key, ef_key, ef_name, out)
            for child_node in (r.get("service_calls") or []):
                self._add_child_node(child_node, "ServiceCall", parent_key, ef_key, ef_name, out)
            for child_node in (r.get("inputs") or []):
                self._add_child_node(child_node, "InputEntity", parent_key, ef_key, ef_name, out)
            for child_node in (r.get("variables") or []):
                self._add_child_node(child_node, "Variable", parent_key, ef_key, ef_name, out)

    def _add_child_node(
        self,
        node: Any,
        default_type: str,
        parent_key: str,
        ef_key: str,
        ef_name: str,
        out: dict[str, _NodeRecord],
    ) -> None:
        if node is None:
            return
        props = dict(node) if not isinstance(node, dict) else node
        key = props.get("key") or ""
        if not key or key in out:
            return
        labels = props.get("labels") or []
        node_type = _pick_label(labels, default_type) if labels else default_type
        out[key] = _NodeRecord(
            node_key=key,
            node_name=props.get("name") or "",
            node_type=node_type,
            parent_snippet_key=parent_key,
            ef_key=ef_key,
            ef_name=ef_name,
            topic=props.get("topic"),
            business_summary=props.get("business_summary") or "",
            function_name=props.get("function_name") or "",
            all_properties=props,
        )

    async def _fetch_jobs_steps(self, pid: int, rid: int, out: dict[str, _NodeRecord]) -> None:
        cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(j:Job)
        OPTIONAL MATCH (j)-[:HAS_STEP]->(step:Step)
        OPTIONAL MATCH (step)-[:Contains_variable]->(sv:Variable)
        WITH j, ef,
             collect(DISTINCT step) AS steps,
             collect(DISTINCT sv) AS step_vars
        RETURN ef.key AS ef_key, ef.name AS ef_name,
               j AS job_node,
               steps, step_vars
        """
        try:
            rows = await self._neo4j.query(cypher, {"pid": pid, "rid": rid})
        except Exception:
            logger.debug("Job/Step query returned no results or failed; skipping.")
            return

        for r in rows:
            ef_key = r.get("ef_key") or ""
            ef_name = r.get("ef_name") or ""
            job = r.get("job_node")
            if job:
                jprops = dict(job) if not isinstance(job, dict) else job
                jkey = jprops.get("key") or ""
                if jkey and jkey not in out:
                    out[jkey] = _NodeRecord(
                        node_key=jkey,
                        node_name=jprops.get("name") or "",
                        node_type="Job",
                        parent_snippet_key=None,
                        ef_key=ef_key,
                        ef_name=ef_name,
                        topic=jprops.get("topic"),
                        business_summary=jprops.get("business_summary") or "",
                        all_properties=jprops,
                    )
                for step_node in (r.get("steps") or []):
                    self._add_child_node(step_node, "Step", jkey, ef_key, ef_name, out)
                for sv_node in (r.get("step_vars") or []):
                    self._add_child_node(sv_node, "Variable", jkey, ef_key, ef_name, out)

    async def _fetch_database_entities(self, pid: int, rid: int, out: dict[str, _NodeRecord]) -> None:
        cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
              -[:CONTAINS_DB_CALLS]->(dbc:DBCall)
        OPTIONAL MATCH (dbc)-[:ACCESSES|REFERENCES]->(de:DatabaseEntity)
        OPTIONAL MATCH (de)-[:HAS_FIELD]->(df:DatabaseField)
        WITH ef, s, de, collect(DISTINCT df) AS fields
        RETURN ef.key AS ef_key, s.key AS snippet_key,
               de AS entity_node,
               fields
        """
        try:
            rows = await self._neo4j.query(cypher, {"pid": pid, "rid": rid})
        except Exception:
            logger.debug("DatabaseEntity traversal returned no results; skipping.")
            return

        for r in rows:
            ef_key = r.get("ef_key") or ""
            snippet_key = r.get("snippet_key") or ""
            entity = r.get("entity_node")
            if entity:
                eprops = dict(entity) if not isinstance(entity, dict) else entity
                ekey = eprops.get("key") or ""
                if ekey and ekey not in out:
                    out[ekey] = _NodeRecord(
                        node_key=ekey,
                        node_name=eprops.get("name") or "",
                        node_type="DatabaseEntity",
                        parent_snippet_key=snippet_key,
                        ef_key=ef_key,
                        ef_name="",
                        topic=eprops.get("topic"),
                        business_summary=eprops.get("business_summary") or eprops.get("description") or "",
                        all_properties=eprops,
                    )
                for fnode in (r.get("fields") or []):
                    self._add_child_node(fnode, "DatabaseField", ekey, ef_key, "", out)

    # ==================================================================
    # STEP 2 — Detect convergence clusters (exact + fuzzy, optimised)
    # ==================================================================

    @staticmethod
    def _detect_convergence(nodes: list[_NodeRecord]) -> dict[str, list[str]]:
        topic_map: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            if n.topic:
                normalised = n.topic.strip().lower()
                topic_map[normalised].append(n.node_key)

        groups: dict[str, list[str]] = {}
        group_idx = 0

        for normalised, keys in topic_map.items():
            if len(keys) > 1:
                gid = f"dup_{group_idx}"
                groups[gid] = keys
                group_idx += 1

        seen_topics = list(topic_map.keys())
        topic_lengths = [len(t) for t in seen_topics]
        n_topics = len(seen_topics)

        for i in range(n_topics):
            len_i = topic_lengths[i]
            for j in range(i + 1, n_topics):
                # Length-based pruning: SequenceMatcher ratio can't exceed
                # 2*min(a,b)/(a+b), so skip pairs that can't reach threshold
                len_j = topic_lengths[j]
                max_possible = 2.0 * min(len_i, len_j) / max(len_i + len_j, 1)
                if max_possible < FUZZY_SIMILARITY_THRESHOLD:
                    continue
                if seen_topics[i] == seen_topics[j]:
                    continue
                ratio = SequenceMatcher(None, seen_topics[i], seen_topics[j]).ratio()
                if ratio >= FUZZY_SIMILARITY_THRESHOLD:
                    combined_keys = list(set(topic_map[seen_topics[i]] + topic_map[seen_topics[j]]))
                    if len(combined_keys) > 1:
                        gid = f"sim_{group_idx}"
                        groups[gid] = combined_keys
                        group_idx += 1

        return groups

    # ==================================================================
    # STEP 3 — Verb-noun pattern validation (rule-based)
    # ==================================================================

    @staticmethod
    def check_verb_noun(topic: str) -> list[TopicIssueType]:
        issues: list[TopicIssueType] = []
        if not topic or not topic.strip():
            return issues
        words = topic.strip().split()
        first_word = words[0].lower()

        for w in words:
            if w.lower() in TECHNICAL_GLUE_WORDS:
                issues.append(TopicIssueType.TECHNICAL_GLUE)
                break

        if first_word not in APPROVED_VERBS:
            issues.append(TopicIssueType.VERB_NOUN_VIOLATION)

        return issues

    # ==================================================================
    # STEP 4+7 COMBINED — Single LLM call: alignment + remedial
    # ==================================================================

    async def _check_and_remediate(
        self,
        node: _NodeRecord,
        rule_issues: list[TopicIssueType],
        duplicate_keys: list[str],
        nodes_by_key: dict[str, _NodeRecord],
        flow_tree: _FlowTree | None,
        sem: asyncio.Semaphore,
    ) -> tuple[float, str, str, str, float]:
        """Returns (alignment_score, alignment_explanation,
                    suggested_topic, rationale, confidence)."""
        if not node.topic and not node.business_summary:
            return (0.0, "Both topic and business_summary missing", "", "", 0.0)

        caller_topic = ""
        children_topics: list[str] = []
        sibling_topics: list[str] = []
        if flow_tree and node.node_key in flow_tree.parent_of:
            parent_key = flow_tree.parent(node.node_key)
            if parent_key:
                pn = nodes_by_key.get(parent_key)
                caller_topic = pn.topic if pn and pn.topic else ""
            for ck in flow_tree.children(node.node_key):
                cn = nodes_by_key.get(ck)
                if cn and cn.topic:
                    children_topics.append(cn.topic)
            for sk in flow_tree.siblings(node.node_key):
                sn = nodes_by_key.get(sk)
                if sn and sn.topic:
                    sibling_topics.append(sn.topic)

        parent_topic = ""
        parent_code = ""
        if node.parent_snippet_key:
            parent = nodes_by_key.get(node.parent_snippet_key)
            if parent:
                if parent.topic:
                    parent_topic = parent.topic
                parent_code = _truncate_code(
                    parent.all_properties.get("snippet") or ""
                )

        dup_details = []
        for dk in duplicate_keys:
            dn = nodes_by_key.get(dk)
            if dn and dn.node_key != node.node_key:
                dup_details.append(f"key={dk} bs=\"{dn.business_summary[:80]}\"")

        has_issues = bool(rule_issues)
        needs_alignment = bool(node.topic and node.business_summary)

        snippet_code = _truncate_code(
            node.all_properties.get("snippet") or ""
        )

        # For InputEntity/InputInterface nodes, extract useful UI properties
        ui_context = ""
        if node.node_type in ("InputEntity", "InputInterface"):
            ui_props = {}
            for prop_key in ("Caption", "OnClick", "OnChange", "type",
                             "Name", "ControlType", "Action"):
                val = node.all_properties.get(prop_key)
                if val:
                    ui_props[prop_key] = val
            if ui_props:
                ui_context = "; ".join(f"{k}={v}" for k, v in ui_props.items())

        context_parts = [
            f"- Current topic: \"{node.topic or '(missing)'}\"",
            f"- Business summary: \"{node.business_summary}\"",
            f"- Node type: \"{node.node_type}\"",
            f"- Function name: \"{node.function_name}\"",
            f"- Source code:\n```\n{snippet_code}\n```" if snippet_code else "",
            f"- Parent snippet topic: \"{parent_topic}\"",
            f"- Parent snippet code:\n```\n{parent_code}\n```" if parent_code else "",
            f"- UI element properties: {ui_context}" if ui_context else "",
            f"- Issues found: {[i.value for i in rule_issues]}",
            f"- Called by (parent in call tree): \"{caller_topic}\"" if caller_topic else "",
            f"- Calls (children in call tree): {children_topics}" if children_topics else "",
            f"- Siblings (also called by same parent): {sibling_topics}" if sibling_topics else "",
            f"- Other nodes with same topic: {dup_details}" if dup_details else "",
        ]
        context_lines = "\n".join(p for p in context_parts if p)
        prompt = get_prompt("topic_convergence.topic_alignment", context_lines=context_lines)

        async with sem:
            try:
                resp = await litellm.acompletion(
                    model=settings.litellm_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=settings.llm_temperature,
                    max_tokens=600,
                    **settings.get_litellm_kwargs(),
                )
                text = resp.choices[0].message.content.strip()
                parsed = _parse_json_response(text)
                return (
                    float(parsed.get("alignment_score", 0.5)),
                    parsed.get("alignment_explanation", ""),
                    parsed.get("suggested_topic", ""),
                    parsed.get("rationale", ""),
                    float(parsed.get("confidence", 0.7)),
                )
            except Exception as exc:
                logger.warning("Combined LLM check failed for %s: %s", node.node_key, exc)
                return (0.5, f"LLM error: {exc}", "", "", 0.0)

    # ==================================================================
    # STEP 5 — Build CALLS chain per ExecutionFlow
    # ==================================================================

    async def _build_journey_chains(
        self,
        project_id: int,
        run_id: int,
        nodes_by_key: dict[str, _NodeRecord],
    ) -> dict[str, _FlowTree]:
        """Returns {ef_key: _FlowTree} with ordered nodes and adjacency.

        Each tree has the ExecutionFlow node as its root, with the snippet
        call tree hanging beneath it.
        """
        cypher = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})<-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
        OPTIONAL MATCH (s)-[c:CALLS]->(t:Snippet)
        RETURN ef.key AS ef_key,
               ef.name AS ef_name,
               ef.topic AS ef_topic,
               ef.description AS ef_description,
               s.key AS caller_key,
               t.key AS callee_key,
               c.global_execution_order AS exec_order
        ORDER BY c.global_execution_order
        """
        rows = await self._neo4j.query(cypher, {"pid": project_id, "rid": run_id})

        ef_edges: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        ef_meta: dict[str, dict[str, str]] = {}
        for r in rows:
            ef_key = r.get("ef_key") or ""
            caller = r.get("caller_key") or ""
            callee = r.get("callee_key")
            order = r.get("exec_order") or 0
            if caller and callee:
                ef_edges[ef_key].append((caller, callee, order))
            if ef_key and ef_key not in ef_meta:
                ef_meta[ef_key] = {
                    "name": r.get("ef_name") or "",
                    "topic": r.get("ef_topic") or "",
                    "description": r.get("ef_description") or "",
                }

        trees: dict[str, _FlowTree] = {}
        for ef_key, edges in ef_edges.items():
            edges.sort(key=lambda x: x[2])

            all_callers = {c for c, _, _ in edges}
            all_callees = {t for _, t, _ in edges}
            snippet_roots = all_callers - all_callees
            if not snippet_roots:
                snippet_roots = {edges[0][0]} if edges else set()

            adjacency: dict[str, list[str]] = defaultdict(list)
            parent_of: dict[str, str | None] = {}

            for caller, callee, _ in edges:
                if callee not in adjacency[caller]:
                    adjacency[caller].append(callee)
                if callee not in parent_of:
                    parent_of[callee] = caller

            adjacency[ef_key] = list(snippet_roots)
            for sr in snippet_roots:
                parent_of[sr] = ef_key
            parent_of[ef_key] = None

            meta = ef_meta.get(ef_key, {})
            if ef_key not in nodes_by_key:
                nodes_by_key[ef_key] = _NodeRecord(
                    node_key=ef_key,
                    node_name=meta.get("name", ""),
                    node_type="ExecutionFlow",
                    parent_snippet_key=None,
                    ef_key=ef_key,
                    ef_name=meta.get("name", ""),
                    topic=meta.get("topic") or None,
                    business_summary=meta.get("description", ""),
                    function_name="",
                    all_properties={
                        "name": meta.get("name", ""),
                        "description": meta.get("description", ""),
                    },
                )

            ordered: list[str] = [ef_key]
            seen: set[str] = {ef_key}

            queue: deque[str] = deque()
            for r_key in sorted(snippet_roots):
                if r_key not in seen:
                    queue.append(r_key)
                    seen.add(r_key)

            while queue:
                node = queue.popleft()
                ordered.append(node)
                for child in adjacency.get(node, []):
                    if child not in seen:
                        seen.add(child)
                        queue.append(child)

            for caller, callee, _ in edges:
                for k in (caller, callee):
                    if k not in seen:
                        ordered.append(k)
                        seen.add(k)

            trees[ef_key] = _FlowTree(
                ordered=ordered,
                adjacency=dict(adjacency),
                parent_of=parent_of,
            )
        return trees

    # ==================================================================
    # STEP 6 — LLM: journey chain validation (PARALLEL across flows)
    # ==================================================================

    @staticmethod
    def _build_tree_text(
        tree: _FlowTree,
        nodes_by_key: dict[str, _NodeRecord],
    ) -> str:
        """Render call tree as indented text so the LLM sees parent-child structure."""
        lines: list[str] = []
        key_info: dict[str, dict] = {}
        for key in tree.ordered:
            n = nodes_by_key.get(key)
            if n:
                code_brief = _truncate_code(
                    n.all_properties.get("snippet") or "", 200,
                )
                key_info[key] = {
                    "key": key,
                    "topic": n.topic or "(missing)",
                    "business_summary": n.business_summary,
                    "node_type": n.node_type,
                    "code": code_brief,
                }

        visited: set[str] = set()

        def _walk(key: str, depth: int) -> None:
            if key in visited or key not in key_info:
                return
            visited.add(key)
            info = key_info[key]
            indent = "    " * depth
            prefix = "├── " if depth > 0 else "[ROOT] "
            lines.append(
                f"{indent}{prefix}[{info['node_type']}] "
                f"topic=\"{info['topic']}\" key={info['key']}"
            )
            if info["code"]:
                lines.append(f"{indent}      code: {info['code']}")
            lines.append(
                f"{indent}      business_summary: \"{info['business_summary']}\""
            )
            for child in tree.children(key):
                _walk(child, depth + 1)

        roots = [k for k in tree.ordered if tree.parent(k) is None]
        for r in roots:
            _walk(r, 0)
        for k in tree.ordered:
            if k not in visited:
                _walk(k, 0)
        return "\n".join(lines)

    async def _validate_journey_chains_parallel(
        self,
        chains: dict[str, _FlowTree],
        nodes_by_key: dict[str, _NodeRecord],
        sem: asyncio.Semaphore,
    ) -> dict[str, str]:
        """Validate all chains in parallel. Returns {node_key: suggested_topic}."""
        all_suggestions: dict[str, str] = {}

        async def _validate_one(ef_key: str, flow: _FlowTree) -> None:
            if len(flow.ordered) < 2:
                return
            tree_text = self._build_tree_text(flow, nodes_by_key)
            if not tree_text:
                return

            prompt = get_prompt("topic_convergence.journey_chain_validation", tree_text=tree_text)
            async with sem:
                try:
                    resp = await litellm.acompletion(
                        model=settings.litellm_model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=settings.llm_temperature,
                        max_tokens=1500,
                        **settings.get_litellm_kwargs(),
                    )
                    text = resp.choices[0].message.content.strip()
                    parsed = _parse_json_response(text)
                    suggestions = parsed.get("suggestions") or []
                    for s in suggestions:
                        if "key" in s and "suggested_topic" in s:
                            all_suggestions[s["key"]] = s["suggested_topic"]
                except Exception as exc:
                    logger.warning("Journey chain validation failed for %s: %s", ef_key, exc)

        items = [(ek, ft) for ek, ft in chains.items() if len(ft.ordered) >= 2]
        for i in range(0, len(items), LLM_BATCH_SIZE):
            batch = items[i:i + LLM_BATCH_SIZE]
            await asyncio.gather(*[_validate_one(ek, ft) for ek, ft in batch])
            if i + LLM_BATCH_SIZE < len(items):
                await asyncio.sleep(LLM_BATCH_DELAY)
        return all_suggestions

    # ==================================================================
    # MAIN ENTRY POINT
    # ==================================================================

    async def analyze(
        self,
        project_id: int,
        run_id: int,
        *,
        progress_callback=None,
    ) -> TopicAnalysisReport:
        """Run full topic convergence analysis and return the report."""

        def _progress(pct: float, msg: str) -> None:
            if progress_callback:
                try:
                    progress_callback(pct, msg)
                except Exception:
                    pass

        sem = asyncio.Semaphore(MAX_CONCURRENT_LLM)
        logger.info("[analyze] START project=%d run=%d", project_id, run_id)

        _progress(0.05, "Fetching all nodes from Neo4j (parallel)...")
        all_nodes = await self._fetch_all_nodes(project_id, run_id)
        if not all_nodes:
            logger.info("[analyze] No nodes found — returning empty report")
            _progress(1.0, "No nodes found.")
            return TopicAnalysisReport(project_id=project_id, run_id=run_id)
        logger.info("[analyze] Fetched %d nodes", len(all_nodes))

        nodes_by_key: dict[str, _NodeRecord] = {n.node_key: n for n in all_nodes}

        type_breakdown: dict[str, int] = defaultdict(int)
        for n in all_nodes:
            type_breakdown[n.node_type] += 1

        _progress(0.15, "Detecting convergence clusters...")
        logger.info("[analyze] Detecting convergence clusters...")
        dup_groups = self._detect_convergence(all_nodes)
        logger.info("[analyze] Found %d convergence groups", len(dup_groups))

        key_to_group: dict[str, str] = {}
        for gid, keys in dup_groups.items():
            for k in keys:
                key_to_group[k] = gid

        _progress(0.20, "Checking verb-noun patterns...")
        key_issues: dict[str, list[TopicIssueType]] = defaultdict(list)
        for n in all_nodes:
            if n.topic is None or n.topic.strip() == "":
                key_issues[n.node_key].append(TopicIssueType.TOPIC_MISSING)
            else:
                vn_issues = self.check_verb_noun(n.topic)
                key_issues[n.node_key].extend(vn_issues)
            if n.node_key in key_to_group:
                grp_id = key_to_group[n.node_key]
                if grp_id.startswith("dup_"):
                    key_issues[n.node_key].append(TopicIssueType.DUPLICATE)
                else:
                    key_issues[n.node_key].append(TopicIssueType.SIMILAR)

        _progress(0.25, "Building journey chains & validating (parallel)...")
        logger.info("[analyze] Building journey chains...")
        chains = await self._build_journey_chains(project_id, run_id, nodes_by_key)
        logger.info("[analyze] Built %d journey chains, validating...", len(chains))

        ef_node_keys_added: set[str] = set()
        for ef_key in chains:
            ef_node = nodes_by_key.get(ef_key)
            if ef_node and ef_node.node_key not in {n.node_key for n in all_nodes}:
                all_nodes.append(ef_node)
                ef_node_keys_added.add(ef_key)
                type_breakdown[ef_node.node_type] += 1
                if not ef_node.topic:
                    key_issues[ef_key].append(TopicIssueType.TOPIC_MISSING)

        journey_suggestions = await self._validate_journey_chains_parallel(chains, nodes_by_key, sem)
        logger.info("[analyze] Journey validation done, %d suggestions", len(journey_suggestions))

        for nk, suggested in journey_suggestions.items():
            n = nodes_by_key.get(nk)
            if n and suggested and n.topic and suggested.lower() != n.topic.lower():
                key_issues[nk].append(TopicIssueType.JOURNEY_BREAK)

        # Determine which nodes need LLM analysis:
        # - nodes with any rule-based issue
        # - nodes that have both topic and business_summary (for alignment check)
        nodes_needing_llm: set[str] = set()
        for k, issues in key_issues.items():
            if issues:
                nodes_needing_llm.add(k)
        for n in all_nodes:
            if n.topic and n.business_summary:
                nodes_needing_llm.add(n.node_key)

        total_llm = len(nodes_needing_llm)
        logger.info("[analyze] %d nodes need LLM analysis (batch_size=%d, delay=%.1fs)", total_llm, LLM_BATCH_SIZE, LLM_BATCH_DELAY)
        _progress(0.35, f"Running combined alignment + remedial LLM for {total_llm} nodes...")

        llm_results: dict[str, tuple[float, str, str, str, float]] = {}

        async def _process_node(nkey: str) -> None:
            n = nodes_by_key[nkey]
            issues = key_issues.get(nkey, [])
            dup_keys = dup_groups.get(key_to_group.get(nkey, ""), [])
            flow_tree = chains.get(n.ef_key)
            result = await self._check_and_remediate(
                n, issues, dup_keys, nodes_by_key, flow_tree, sem,
            )
            llm_results[nkey] = result

        llm_keys = list(nodes_needing_llm)
        completed = 0
        num_batches = (len(llm_keys) + LLM_BATCH_SIZE - 1) // max(LLM_BATCH_SIZE, 1)
        logger.info("[analyze] Processing %d LLM nodes in %d batches", total_llm, num_batches)
        for i in range(0, len(llm_keys), LLM_BATCH_SIZE):
            batch = llm_keys[i:i + LLM_BATCH_SIZE]
            batch_num = i // LLM_BATCH_SIZE + 1
            logger.info("[analyze] LLM batch %d/%d (%d nodes)...", batch_num, num_batches, len(batch))
            await asyncio.gather(*[_process_node(nk) for nk in batch])
            completed += len(batch)
            pct = 0.35 + 0.40 * (completed / max(total_llm, 1))
            _progress(pct, f"LLM analysis: {completed}/{total_llm} nodes...")
            logger.info("[analyze] LLM batch %d/%d done — %d/%d total", batch_num, num_batches, completed, total_llm)
            if i + LLM_BATCH_SIZE < len(llm_keys):
                await asyncio.sleep(LLM_BATCH_DELAY)

        # Apply alignment results
        for nkey, (align_score, _, _, _, _) in llm_results.items():
            if align_score < ALIGNMENT_THRESHOLD:
                if TopicIssueType.MISALIGNED not in key_issues[nkey]:
                    key_issues[nkey].append(TopicIssueType.MISALIGNED)

        logger.info("[analyze] All LLM calls complete. Compiling analysis for %d nodes...", len(all_nodes))
        _progress(0.78, f"Compiling analysis for {len(all_nodes)} nodes...")

        analyses: list[NodeTopicAnalysis] = []
        for n in all_nodes:
            issues = list(dict.fromkeys(key_issues.get(n.node_key, [])))

            suggested_topic = ""
            rationale = ""
            confidence = 0.0
            alignment_detail = ""

            if n.node_key in llm_results:
                align_score, align_expl, sug_topic, rat, conf = llm_results[n.node_key]
                alignment_detail = f"Alignment={align_score:.2f}: {align_expl}"
                if sug_topic:
                    suggested_topic = sug_topic
                    rationale = rat
                    confidence = conf

            if not suggested_topic and n.node_key in journey_suggestions:
                suggested_topic = journey_suggestions[n.node_key]
                rationale = "Suggested by journey chain analysis"
                confidence = 0.6

            flow_tree = chains.get(n.ef_key)
            chain_pos = None
            chain_ctx = ""
            if flow_tree and n.node_key in flow_tree.parent_of:
                chain_pos = flow_tree.ordered.index(n.node_key) if n.node_key in flow_tree.ordered else None
                parent_key = flow_tree.parent(n.node_key)
                child_keys = flow_tree.children(n.node_key)
                parent_t = ""
                if parent_key:
                    pn = nodes_by_key.get(parent_key)
                    parent_t = pn.topic if pn and pn.topic else "?"
                child_ts = []
                for ck in child_keys:
                    cn = nodes_by_key.get(ck)
                    child_ts.append(cn.topic if cn and cn.topic else "?")
                current_t = n.topic or "(missing)"
                if parent_t and child_ts:
                    chain_ctx = f"{parent_t} -> [{current_t}] -> {{{', '.join(child_ts)}}}"
                elif parent_t:
                    chain_ctx = f"{parent_t} -> [{current_t}]"
                elif child_ts:
                    chain_ctx = f"[{current_t}] -> {{{', '.join(child_ts)}}}"
                else:
                    chain_ctx = f"[{current_t}]"

            issue_details_parts = []
            if TopicIssueType.TOPIC_MISSING in issues:
                issue_details_parts.append("Topic field is missing on this node")
            if TopicIssueType.DUPLICATE in issues:
                issue_details_parts.append(f"Exact duplicate in group {key_to_group.get(n.node_key, '?')}")
            if TopicIssueType.SIMILAR in issues:
                issue_details_parts.append(f"Fuzzy match in group {key_to_group.get(n.node_key, '?')}")
            if TopicIssueType.VERB_NOUN_VIOLATION in issues:
                issue_details_parts.append("Does not follow Active Verb + Business Object pattern")
            if TopicIssueType.TECHNICAL_GLUE in issues:
                issue_details_parts.append("Contains technical glue words")
            if TopicIssueType.MISALIGNED in issues:
                issue_details_parts.append(alignment_detail or "Topic does not match business_summary")
            if TopicIssueType.JOURNEY_BREAK in issues:
                issue_details_parts.append("Breaks customer journey coherence in chain")

            analyses.append(NodeTopicAnalysis(
                node_key=n.node_key,
                node_name=n.node_name,
                node_type=n.node_type,
                parent_snippet_key=n.parent_snippet_key,
                execution_flow_key=n.ef_key,
                execution_flow_name=n.ef_name,
                current_topic=n.topic or "",
                business_summary=n.business_summary,
                issues=issues,
                issue_details="; ".join(issue_details_parts),
                suggested_topic=suggested_topic,
                suggestion_rationale=rationale,
                confidence=confidence,
                chain_position=chain_pos,
                chain_context=chain_ctx,
                duplicate_group_id=key_to_group.get(n.node_key),
            ))

        _progress(0.95, "Compiling report...")
        issue_breakdown: dict[str, int] = defaultdict(int)
        nodes_with_issues = 0
        nodes_missing = 0
        for a in analyses:
            if a.issues:
                nodes_with_issues += 1
            for issue in a.issues:
                issue_breakdown[issue.value] += 1
            if TopicIssueType.TOPIC_MISSING in a.issues:
                nodes_missing += 1

        journey_chain_topics: dict[str, list[str]] = {}
        for ef_key, flow_tree in chains.items():
            topics = []
            for k in flow_tree.ordered:
                n = nodes_by_key.get(k)
                topics.append(n.topic if n and n.topic else "(missing)")
            journey_chain_topics[ef_key] = topics

        journey_chain_trees: dict[str, dict[str, list[str]]] = {}
        for ef_key, flow_tree in chains.items():
            adj_topics: dict[str, list[str]] = {}
            for parent_key, child_keys in flow_tree.adjacency.items():
                pn = nodes_by_key.get(parent_key)
                parent_topic = pn.topic if pn and pn.topic else "(missing)"
                child_topic_list = []
                for ck in child_keys:
                    cn = nodes_by_key.get(ck)
                    child_topic_list.append(cn.topic if cn and cn.topic else "(missing)")
                adj_topics[parent_topic] = child_topic_list
            journey_chain_trees[ef_key] = adj_topics

        report = TopicAnalysisReport(
            project_id=project_id,
            run_id=run_id,
            total_nodes_analyzed=len(all_nodes),
            nodes_with_issues=nodes_with_issues,
            nodes_missing_topic=nodes_missing,
            issue_breakdown=dict(issue_breakdown),
            node_type_breakdown=dict(type_breakdown),
            analyses=analyses,
            duplicate_groups=dup_groups,
            journey_chains=journey_chain_topics,
            journey_chain_trees=journey_chain_trees,
        )
        logger.info(
            "[analyze] DONE: %d nodes, %d with issues, %d missing topic",
            report.total_nodes_analyzed, report.nodes_with_issues, report.nodes_missing_topic,
        )
        _progress(1.0, "Analysis complete.")
        return report


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_CODE_TRUNCATE_LEN = 500


def _truncate_code(code: str, max_len: int = _CODE_TRUNCATE_LEN) -> str:
    """Truncate source code to a reasonable length for LLM context."""
    if not code:
        return ""
    code = code.strip()
    if len(code) <= max_len:
        return code
    return code[:max_len] + "\n... (truncated)"


def _pick_label(labels: list[str], default: str) -> str:
    """Pick the most specific label from a Neo4j labels list."""
    skip = {"Node", "Resource"}
    for lbl in labels:
        if lbl not in skip:
            return lbl
    return default


def _parse_json_response(text: str) -> dict:
    """Best-effort JSON extraction from LLM output."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {}
