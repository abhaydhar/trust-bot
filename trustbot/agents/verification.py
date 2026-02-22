"""
Verification Agent — diffs Neo4j and Filesystem graphs, produces trust scores.

Classifies edges as: Confirmed, Phantom, Missing, Conflicted.
Uses multi-field matching: (function_name, class_name, file_name) for both
caller and callee. Falls back to name-only matching when file/class info
is missing from one side.

Computes edge-level, node-level, and flow-level trust scores.
"""

from __future__ import annotations

import logging

from trustbot.models.agentic import (
    CallGraphOutput,
    EdgeClassification,
    ExtractionMethod,
    GraphSource,
    VerifiedEdge,
    VerificationResult,
    normalize_file_path,
)

logger = logging.getLogger("trustbot.agents.verification")


def _to_bare_name(name: str) -> str:
    """
    Normalize to a comparable bare function name for name-only matching.
    Strips a leading 'ClassName.' so Neo4j's 'TForm1.Button2Click' matches
    the index's 'Button2Click'.
    """
    s = (name or "").strip().upper()
    if not s:
        return s
    if "." in s:
        return s.rsplit(".", 1)[-1].strip()
    return s


class VerificationAgent:
    """Diffs two call graphs and produces trust scores."""

    def verify(
        self,
        neo4j_graph: CallGraphOutput,
        fs_graph: CallGraphOutput,
    ) -> VerificationResult:
        """
        Compare Neo4j and Filesystem graphs, classify edges, compute scores.

        Matching strategy (in order of preference):
        1. Full match: (caller, caller_class, caller_file, callee, callee_class, callee_file)
        2. Name+file match: (caller, caller_file, callee, callee_file) ignoring class
        3. Name-only match: (caller, callee) — used as fallback when file info is sparse
        """
        neo_full = neo4j_graph.to_comparable_edges()
        fs_full = fs_graph.to_comparable_edges()

        neo_name_only = neo4j_graph.to_comparable_edges_by_name()
        fs_name_only = fs_graph.to_comparable_edges_by_name()
        # Bare-name sets for name-only matching: strip "ClassName." so Neo4j
        # qualified names match index bare names (e.g. TForm1.Button2Click ↔ Button2Click).
        fs_name_only_bare = {
            (_to_bare_name(c), _to_bare_name(ce)) for c, ce in fs_name_only
        }

        # Build name+file sets (ignore class) for tier-2 matching
        def _name_file_key(t):
            return (t[0], t[2], t[3], t[5])

        def _bare_name_file_key(t):
            return (_to_bare_name(t[0]), t[2], _to_bare_name(t[3]), t[5])

        neo_name_file = {_name_file_key(e) for e in neo_full}
        fs_name_file = {_name_file_key(e) for e in fs_full}
        # Bare-name+file sets: strip ClassName. so TForm1.Button2Click ↔ Button2Click
        fs_name_file_bare = {_bare_name_file_key(e) for e in fs_full}

        confirmed: list[VerifiedEdge] = []
        phantom: list[VerifiedEdge] = []
        missing: list[VerifiedEdge] = []
        conflicted: list[VerifiedEdge] = []

        neo_edge_method: dict[tuple[str, str], ExtractionMethod] = {}
        for e in neo4j_graph.edges:
            key = (e.caller.upper().strip(), e.callee.upper().strip())
            neo_edge_method[key] = e.extraction_method

        # Track which Neo4j edges got matched (to detect phantom)
        matched_neo_full: set = set()
        matched_neo_name_file: set = set()
        matched_neo_name: set = set()

        # Pass 1: full match (all 6 fields)
        for edge_key in neo_full:
            if edge_key in fs_full:
                name_key = (edge_key[0], edge_key[3])
                method = neo_edge_method.get(name_key, ExtractionMethod.NEO4J)
                score = self._edge_trust(method, EdgeClassification.CONFIRMED)
                confirmed.append(
                    VerifiedEdge(
                        caller=edge_key[0],
                        callee=edge_key[3],
                        caller_file=edge_key[2],
                        callee_file=edge_key[5],
                        classification=EdgeClassification.CONFIRMED,
                        trust_score=score,
                        details="Full match (name + class + file)",
                    )
                )
                matched_neo_full.add(edge_key)

        # Pass 2: name+file match for remaining unmatched edges.
        # Also tries bare-name+file so "TForm1.Button2Click" matches "Button2Click"
        # when both reference the same file.
        unmatched_neo_nf = {_name_file_key(e) for e in neo_full if e not in matched_neo_full}
        for nf_key in unmatched_neo_nf:
            bare_nf = (_to_bare_name(nf_key[0]), nf_key[1], _to_bare_name(nf_key[2]), nf_key[3])
            exact = nf_key in fs_name_file
            bare = bare_nf in fs_name_file_bare
            if exact or bare:
                name_key = (nf_key[0], nf_key[2])
                method = neo_edge_method.get(name_key, ExtractionMethod.NEO4J)
                score = self._edge_trust(method, EdgeClassification.CONFIRMED) * 0.95
                details = "Matched on name + file (class mismatch or missing)"
                if bare and not exact:
                    details = "Matched on bare name + file (qualified → bare name)"
                    score *= 0.98
                confirmed.append(
                    VerifiedEdge(
                        caller=nf_key[0],
                        callee=nf_key[2],
                        caller_file=nf_key[1],
                        callee_file=nf_key[3],
                        classification=EdgeClassification.CONFIRMED,
                        trust_score=score,
                        details=details,
                    )
                )
                matched_neo_name_file.add(nf_key)

        # Pass 3: name-only fallback for still-unmatched edges.
        # Use bare-name comparison so Neo4j "Class.Method" matches index "Method".
        already_confirmed_names = {(e.caller, e.callee) for e in confirmed}
        for name_key in neo_name_only:
            if name_key in already_confirmed_names:
                continue
            bare_key = (_to_bare_name(name_key[0]), _to_bare_name(name_key[1]))
            if name_key in fs_name_only or bare_key in fs_name_only_bare:
                method = neo_edge_method.get(name_key, ExtractionMethod.NEO4J)
                score = self._edge_trust(method, EdgeClassification.CONFIRMED) * 0.80
                details = "Matched on function name only (file/class not compared)"
                if bare_key in fs_name_only_bare and name_key not in fs_name_only:
                    details = "Matched on bare name (Neo4j qualified name ↔ index bare name)"
                confirmed.append(
                    VerifiedEdge(
                        caller=name_key[0],
                        callee=name_key[1],
                        classification=EdgeClassification.CONFIRMED,
                        trust_score=score,
                        details=details,
                    )
                )
                matched_neo_name.add(name_key)

        # Phantom: Neo4j edges not matched at any tier
        confirmed_names = {(e.caller, e.callee) for e in confirmed}
        # Bare keys of confirmed edges (so we don't count index edges as missing when
        # they matched on bare name, e.g. Button2Click ↔ TForm1.Button2Click)
        confirmed_bare = {
            (_to_bare_name(e.caller), _to_bare_name(e.callee)) for e in confirmed
        }
        for name_key in neo_name_only:
            if name_key not in confirmed_names:
                phantom.append(
                    VerifiedEdge(
                        caller=name_key[0],
                        callee=name_key[1],
                        classification=EdgeClassification.PHANTOM,
                        trust_score=0.20,
                        details="In Neo4j only — not found in indexed codebase",
                    )
                )

        # Missing: filesystem edges not matched at any tier (exclude if bare-key matched)
        for name_key in fs_name_only:
            if name_key in confirmed_names:
                continue
            bare_key = (_to_bare_name(name_key[0]), _to_bare_name(name_key[1]))
            if bare_key in confirmed_bare:
                continue
            missing.append(
                VerifiedEdge(
                    caller=name_key[0],
                    callee=name_key[1],
                    classification=EdgeClassification.MISSING,
                    trust_score=0.0,
                    details="In indexed codebase only — not in Neo4j graph",
                )
            )

        # Execution order comparison:
        # For each caller with multiple callees in both graphs, check if
        # the relative call sequence matches.
        order_matches, order_mismatches = self._compare_execution_order(
            neo4j_graph, fs_graph,
        )

        # Flow-level score: weighted average of all edge scores.
        # Confirmed edges get full weight; phantom edges get partial weight
        # because some (e.g. .dfm form bindings) are structurally unverifiable.
        weighted_sum = 0.0
        weighted_count = 0.0
        for e in confirmed:
            weighted_sum += e.trust_score * 1.0
            weighted_count += 1.0
        for e in phantom:
            weighted_sum += e.trust_score * 0.5
            weighted_count += 0.5
        graph_score = weighted_sum / weighted_count if weighted_count > 0 else 0.0
        # Flow score: ratio of confirmed edges to total Neo4j edges
        total_neo = len(neo_name_only) if neo_name_only else 1
        flow_score = len(confirmed) / total_neo if total_neo > 0 else 0.0

        result = VerificationResult(
            execution_flow_id=neo4j_graph.execution_flow_id,
            graph_trust_score=graph_score,
            flow_trust_score=flow_score,
            confirmed_edges=confirmed,
            phantom_edges=phantom,
            missing_edges=missing,
            conflicted_edges=conflicted,
            unresolved_callees=fs_graph.unresolved_callees,
            metadata={
                "neo4j_edges": len(neo_name_only),
                "filesystem_edges": len(fs_name_only),
                "confirmed": len(confirmed),
                "phantom": len(phantom),
                "missing": len(missing),
                "match_full": len(matched_neo_full),
                "match_name_file": len(matched_neo_name_file),
                "match_name_only": len(matched_neo_name),
                "execution_order_matches": order_matches,
                "execution_order_mismatches": order_mismatches,
            },
        )

        logger.info(
            "Verification: %d confirmed (%d full, %d name+file, %d name-only), "
            "%d phantom, %d missing for flow %s",
            len(confirmed), len(matched_neo_full), len(matched_neo_name_file),
            len(matched_neo_name), len(phantom), len(missing),
            neo4j_graph.execution_flow_id,
        )
        return result

    def _compare_execution_order(
        self,
        neo4j_graph: CallGraphOutput,
        fs_graph: CallGraphOutput,
    ) -> tuple[int, list[dict]]:
        """
        Compare execution order of callees per caller between the two graphs.
        Returns (match_count, mismatches) where mismatches is a list of dicts
        describing each caller whose callee order differs.
        """
        def _build_caller_order(graph: CallGraphOutput) -> dict[str, list[str]]:
            """Build caller -> [callees in order] from edges."""
            caller_callees: dict[str, list[str]] = {}
            for e in graph.edges:
                key = e.caller.upper().strip()
                callee = e.callee.upper().strip()
                caller_callees.setdefault(key, [])
                if callee not in caller_callees[key]:
                    caller_callees[key].append(callee)
            return caller_callees

        neo_order = _build_caller_order(neo4j_graph)
        fs_order = _build_caller_order(fs_graph)

        matches = 0
        mismatches: list[dict] = []
        for caller, neo_callees in neo_order.items():
            # Try exact caller, then bare caller (Class.Method → Method)
            fs_callees = fs_order.get(caller)
            if fs_callees is None:
                bare_caller = _to_bare_name(caller)
                fs_callees = fs_order.get(bare_caller)
            if fs_callees is None:
                continue
            # Only compare callees that exist in both lists
            common = [c for c in neo_callees if c in fs_callees]
            if len(common) < 2:
                # Can't compare order with < 2 common callees
                matches += 1
                continue
            # Check if the relative order of common callees is the same
            fs_positions = {c: i for i, c in enumerate(fs_callees)}
            neo_common_order = [c for c in neo_callees if c in fs_positions]
            fs_common_order = sorted(neo_common_order, key=lambda c: fs_positions[c])
            if neo_common_order == fs_common_order:
                matches += 1
            else:
                mismatches.append({
                    "caller": caller,
                    "neo4j_order": neo_common_order,
                    "index_order": fs_common_order,
                })
        return matches, mismatches

    def _edge_trust(self, method: ExtractionMethod, classification: EdgeClassification) -> float:
        """Compute edge trust score from extraction method and classification."""
        if classification != EdgeClassification.CONFIRMED:
            return 0.20 if classification == EdgeClassification.PHANTOM else 0.0
        if method == ExtractionMethod.NEO4J:
            return 0.95
        if method == ExtractionMethod.REGEX:
            return 0.90
        if method == ExtractionMethod.LLM_TIER2:
            return 0.80
        if method == ExtractionMethod.LLM_TIER3:
            return 0.70
        return 0.75
