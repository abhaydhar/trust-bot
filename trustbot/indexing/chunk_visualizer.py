"""
Chunk visualizer - generates data for visualizing code chunks and relationships.
"""

from __future__ import annotations

import logging
from pathlib import Path

from trustbot.index.code_index import CodeIndex

logger = logging.getLogger("trustbot.indexing.visualizer")


class ChunkVisualizer:
    """Generate visualization data for code chunks."""

    def __init__(self, code_index: CodeIndex | None = None):
        self._index = code_index

    async def get_graph_data(self) -> dict:
        """
        Get chunk graph data in format suitable for visualization.
        
        Returns:
            {
                "nodes": [{"id": "...", "name": "...", "file": "...", "type": "..."}],
                "edges": [{"from": "...", "to": "...", "confidence": 0.75}]
            }
        """
        if not self._index:
            return {"nodes": [], "edges": []}
        
        try:
            conn = self._index._get_conn()
            rows = conn.execute("SELECT function_name, file_path, language, class_name FROM code_index").fetchall()
            
            nodes = []
            for row in rows:
                func_name = row[0]
                file_path = row[1]
                language = row[2]
                class_name = row[3]
                
                node_id = f"{func_name}@{file_path}"
                
                nodes.append({
                    "id": node_id,
                    "name": func_name,
                    "file": file_path,
                    "language": language,
                    "class": class_name or "",
                    "type": "class" if class_name else "function"
                })
            
            # Get stored call graph edges
            edges = self._index.get_edges()
            
            logger.info(f"Chunk visualization: {len(nodes)} nodes, {len(edges)} edges")
            
            return {
                "nodes": nodes,
                "edges": edges
            }
            
        except Exception as e:
            logger.error(f"Error getting chunk data: {e}")
            return {"nodes": [], "edges": []}
