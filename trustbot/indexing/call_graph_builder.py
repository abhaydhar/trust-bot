"""
Build call graphs from code chunks using static analysis and LLM.
"""

from __future__ import annotations

import logging
import re
from typing import List

from trustbot.indexing.chunker import CodeChunk

logger = logging.getLogger("trustbot.indexing.callgraph")


class CallGraphEdge:
    """Represents a call relationship between two chunks."""
    
    def __init__(self, from_chunk: str, to_chunk: str, confidence: float = 1.0):
        self.from_chunk = from_chunk
        self.to_chunk = to_chunk
        self.confidence = confidence


def build_call_graph_from_chunks(chunks: List[CodeChunk]) -> List[CallGraphEdge]:
    """
    Build a call graph from code chunks using regex pattern matching.
    
    For each chunk, find function calls in its content and match to other chunks.
    """
    edges = []
    
    # Build function name index
    function_index = {}
    for chunk in chunks:
        if chunk.function_name and chunk.function_name != "<module>":
            function_index[chunk.function_name] = chunk
    
    # For each chunk, find calls
    for chunk in chunks:
        if not chunk.content:
            continue
        
        # Find function calls using regex
        # Pattern: function_name(
        call_pattern = re.compile(r'\b([A-Za-z_]\w*)\s*\(')
        
        for match in call_pattern.finditer(chunk.content):
            callee_name = match.group(1)
            
            # Skip built-ins and keywords
            if callee_name in {'if', 'for', 'while', 'def', 'class', 'return', 'print', 'len', 'str', 'int', 'range'}:
                continue
            
            # Check if callee exists in our index
            if callee_name in function_index:
                callee_chunk = function_index[callee_name]
                
                # Don't create self-loops
                if callee_chunk.chunk_id != chunk.chunk_id:
                    edges.append(CallGraphEdge(
                        from_chunk=chunk.chunk_id,
                        to_chunk=callee_chunk.chunk_id,
                        confidence=0.75  # Regex-based, medium confidence
                    ))
    
    logger.info(f"Built call graph: {len(edges)} edges from {len(chunks)} chunks")
    return edges
