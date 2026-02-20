"""
Verification Agent — diffs Neo4j and Filesystem graphs, produces trust scores.

Classifies edges as: Confirmed, Phantom, Missing, Conflicted.
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
        """
        neo_edges = neo4j_graph.to_comparable_edges()
        fs_edges = fs_graph.to_comparable_edges()

        confirmed: list[VerifiedEdge] = []
        phantom: list[VerifiedEdge] = []
        missing: list[VerifiedEdge] = []
        conflicted: list[VerifiedEdge] = []

        # Build edge lookup for score calculation
        neo_edge_scores: dict[tuple[str, str], float] = {}
        for e in neo4j_graph.edges:
            key = (e.caller.upper().strip(), e.callee.upper().strip())
            neo_edge_scores[key] = self._edge_trust(e.extraction_method, EdgeClassification.CONFIRMED)

        for edge_key in neo_edges:
            if edge_key in fs_edges:
                score = neo_edge_scores.get(edge_key, 0.85)
                confirmed.append(
                    VerifiedEdge(
                        caller=edge_key[0],
                        callee=edge_key[1],
                        classification=EdgeClassification.CONFIRMED,
                        trust_score=score,
                        details="Found in both Neo4j and filesystem",
                    )
                )
            else:
                phantom.append(
                    VerifiedEdge(
                        caller=edge_key[0],
                        callee=edge_key[1],
                        classification=EdgeClassification.PHANTOM,
                        trust_score=0.20,
                        details="In Neo4j only — not found in source code",
                    )
                )

        for edge_key in fs_edges - neo_edges:
            missing.append(
                VerifiedEdge(
                    caller=edge_key[0],
                    callee=edge_key[1],
                    classification=EdgeClassification.MISSING,
                    trust_score=0.0,
                    details="In filesystem only — not in Neo4j graph",
                )
            )

        # Flow-level score: min of all edge scores (weakest link)
        all_scores = [e.trust_score for e in confirmed]
        if phantom:
            all_scores.extend([e.trust_score for e in phantom])
        flow_score = min(all_scores) if all_scores else 0.0

        # Graph-level: weighted average
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
                "neo4j_edges": len(neo_edges),
                "filesystem_edges": len(fs_edges),
                "confirmed": len(confirmed),
                "phantom": len(phantom),
                "missing": len(missing),
            },
        )

        logger.info(
            "Verification: %d confirmed, %d phantom, %d missing for flow %s",
            len(confirmed), len(phantom), len(missing), neo4j_graph.execution_flow_id,
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
