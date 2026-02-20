"""
Agent 2 — Filesystem Graph Builder.

Independently constructs a call graph by recursively traversing function calls
from the root function in the Spec Flow Document. Has NO access to Neo4j.
Uses tiered extraction: Regex → LLM Tier 2 → LLM Tier 3.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import litellm

from trustbot.config import settings
from trustbot.models.agentic import (
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
    SpecFlowDocument,
)
from trustbot.tools.filesystem_tool import FilesystemTool
from trustbot.index.code_index import CodeIndex

logger = logging.getLogger("trustbot.agents.agent2")

# Regex patterns for call extraction (Tier 1)
# Each entry: (pattern, group_index for capture)
CALL_PATTERNS: dict[str, list[tuple[re.Pattern, int]]] = {
    "python": [
        (re.compile(r"\b([A-Za-z_]\w*)\s*\("), 1),
        (re.compile(r"from\s+([A-Za-z_]\w*)\s+import"), 1),
        (re.compile(r"import\s+([A-Za-z_]\w*)"), 1),
    ],
    "java": [
        (re.compile(r"\b([A-Za-z_]\w*)\s*\("), 1),
        (re.compile(r"new\s+([A-Za-z_]\w*)\s*\("), 1),
    ],
    "javascript": [
        (re.compile(r"\b([A-Za-z_]\w*)\s*\("), 1),
        (re.compile(r"require\s*\(\s*['\"]([A-Za-z_]\w*)['\"]"), 1),
    ],
}

# Built-in/stdlib names to skip
SKIP_NAMES = frozenset({
    "def", "class", "if", "for", "while", "return", "print", "len", "str",
    "int", "float", "list", "dict", "set", "range", "open", "type", "isinstance",
    "super", "self", "True", "False", "None", "Exception", "raise",
})


class Agent2FilesystemBuilder:
    """
    Agent that builds the call graph from the filesystem only.
    Uses Code Index for callee resolution and tiered extraction.
    """

    def __init__(
        self,
        filesystem_tool: FilesystemTool,
        code_index: CodeIndex,
    ) -> None:
        self._fs = filesystem_tool
        self._index = code_index
        self._max_depth = 50
        self._visited: set[tuple[str, str]] = set()

    async def build(self, spec: SpecFlowDocument) -> CallGraphOutput:
        """
        Recursively traverse from root function and build the call graph.
        """
        self._visited.clear()
        edges: list[CallGraphEdge] = []
        unresolved: list[str] = []

        await self._traverse(
            spec.root_function,
            spec.root_file_path,
            spec.language,
            depth=1,
            edges=edges,
            unresolved=unresolved,
        )

        output = CallGraphOutput(
            execution_flow_id=spec.execution_flow_id or "unknown",
            source=GraphSource.FILESYSTEM,
            root_function=spec.root_function,
            edges=edges,
            unresolved_callees=unresolved,
            metadata={
                "total_depth": max((e.depth for e in edges), default=0),
                "total_nodes": len(set(e.caller for e in edges) | set(e.callee for e in edges)),
                "validation_timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            },
        )

        logger.info(
            "Agent 2 built %d edges from filesystem for flow %s (%d unresolved)",
            len(edges), spec.execution_flow_id, len(unresolved),
        )
        return output

    async def _traverse(
        self,
        function_name: str,
        file_path: str,
        language: str,
        depth: int,
        edges: list[CallGraphEdge],
        unresolved: list[str],
    ) -> None:
        if depth > self._max_depth:
            return

        visit_key = (function_name.upper(), file_path)
        if visit_key in self._visited:
            return
        self._visited.add(visit_key)

        try:
            code = await self._fs.read_file(file_path)
        except Exception as e:
            logger.debug("Could not read %s: %s", file_path, e)
            return

        # Extract function body if we have a specific function
        if function_name and function_name != "<module>":
            body = await self._fs.extract_function_body(file_path, function_name)
            code = body or code

        callees = await self._extract_callees(code, language)

        for callee_name, method, confidence in callees:
            callee_file = self._index.lookup(callee_name)

            if callee_file is None:
                if callee_name not in unresolved:
                    unresolved.append(callee_name)
                continue

            edges.append(
                CallGraphEdge(
                    caller=function_name,
                    callee=callee_name,
                    caller_file=file_path,
                    callee_file=callee_file,
                    depth=depth,
                    extraction_method=method,
                    confidence=confidence,
                )
            )

            await self._traverse(
                callee_name,
                callee_file,
                language,
                depth + 1,
                edges,
                unresolved,
            )

    async def _extract_callees(
        self, code: str, language: str
    ) -> list[tuple[str, ExtractionMethod, float]]:
        """
        Tiered extraction: Tier 1 regex, Tier 2 LLM if needed.
        Returns list of (callee_name, extraction_method, confidence).
        """
        # Tier 1: Regex
        regex_results = self._regex_extract(code, language)
        if regex_results:
            return [(name, ExtractionMethod.REGEX, 0.75) for name in regex_results]

        # Tier 2: LLM
        llm_results = await self._llm_extract(code, language)
        if llm_results:
            return [(name, ExtractionMethod.LLM_TIER2, 0.75) for name in llm_results]

        return []

    def _regex_extract(self, code: str, language: str) -> list[str]:
        """Tier 1: Extract callee names via regex."""
        patterns = CALL_PATTERNS.get(language, CALL_PATTERNS["python"])
        found: set[str] = set()

        for pattern, group in patterns:
            for m in pattern.finditer(code):
                try:
                    name = m.group(group)
                except (IndexError, AttributeError):
                    continue
                if name and name not in SKIP_NAMES and len(name) > 1:
                    found.add(name)

        return list(found)

    async def _llm_extract(self, code: str, language: str) -> list[str]:
        """Tier 2: LLM extraction for ambiguous cases."""
        prompt = f"""You are analyzing {language} source code.
List all functions, programs, or subroutines called in the following code.
Return a JSON array of function names only. No explanation.

Code:
```{language}
{code[:2000]}
```
"""
        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                **settings.get_litellm_kwargs(),
            )
            content = response.choices[0].message.content or "[]"
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
            return json.loads(content)
        except Exception as e:
            logger.debug("LLM extraction failed: %s", e)
            return []
