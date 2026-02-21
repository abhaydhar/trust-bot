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

        # Build name+file sets (ignore class) for tier-2 matching
        def _name_file_key(t):
            return (t[0], t[2], t[3], t[5])

        neo_name_file = {_name_file_key(e) for e in neo_full}
        fs_name_file = {_name_file_key(e) for e in fs_full}

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

        # Pass 2: name+file match for remaining unmatched edges
        unmatched_neo_nf = {_name_file_key(e) for e in neo_full if e not in matched_neo_full}
        for nf_key in unmatched_neo_nf:
            if nf_key in fs_name_file:
                name_key = (nf_key[0], nf_key[2])
                method = neo_edge_method.get(name_key, ExtractionMethod.NEO4J)
                score = self._edge_trust(method, EdgeClassification.CONFIRMED) * 0.95
                confirmed.append(
                    VerifiedEdge(
                        caller=nf_key[0],
                        callee=nf_key[2],
                        caller_file=nf_key[1],
                        callee_file=nf_key[3],
                        classification=EdgeClassification.CONFIRMED,
                        trust_score=score,
                        details="Matched on name + file (class mismatch or missing)",
                    )
                )
                matched_neo_name_file.add(nf_key)

        # Pass 3: name-only fallback for still-unmatched edges
        already_confirmed_names = {(e.caller, e.callee) for e in confirmed}
        for name_key in neo_name_only:
            if name_key in already_confirmed_names:
                continue
            if name_key in fs_name_only:
                method = neo_edge_method.get(name_key, ExtractionMethod.NEO4J)
                score = self._edge_trust(method, EdgeClassification.CONFIRMED) * 0.80
                confirmed.append(
                    VerifiedEdge(
                        caller=name_key[0],
                        callee=name_key[1],
                        classification=EdgeClassification.CONFIRMED,
                        trust_score=score,
                        details="Matched on function name only (file/class not compared)",
                    )
                )
                matched_neo_name.add(name_key)

        # Phantom: Neo4j edges not matched at any tier
        confirmed_names = {(e.caller, e.callee) for e in confirmed}
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

        # Missing: filesystem edges not matched at any tier
        for name_key in fs_name_only - confirmed_names:
            missing.append(
                VerifiedEdge(
                    caller=name_key[0],
                    callee=name_key[1],
                    classification=EdgeClassification.MISSING,
                    trust_score=0.0,
                    details="In indexed codebase only — not in Neo4j graph",
                )
            )

        # Flow-level score
        all_scores = [e.trust_score for e in confirmed]
        if phantom:
            all_scores.extend([e.trust_score for e in phantom])
        flow_score = min(all_scores) if all_scores else 0.0
        graph_score = sum(all_scores) / len(all_scores) if all_scores else 0.0

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
