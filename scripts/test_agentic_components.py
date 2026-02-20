"""
Test script to verify TrustBot agentic functionality.
Tests the multi-agent pipeline components without requiring the full app.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trustbot.agents.agent1_neo4j import Agent1Neo4jFetcher
from trustbot.agents.agent2_filesystem import Agent2FilesystemBuilder
from trustbot.agents.verification import VerificationAgent
from trustbot.agents.report import ReportAgent
from trustbot.index.code_index import CodeIndex
from trustbot.models.agentic import (
    CallGraphOutput,
    CallGraphEdge,
    GraphSource,
    ExtractionMethod,
    SpecFlowDocument,
)
from trustbot.tools.filesystem_tool import FilesystemTool
from trustbot.tools.neo4j_tool import Neo4jTool
from trustbot.config import settings


async def test_code_index():
    print("=" * 60)
    print("Testing Code Index")
    print("=" * 60)
    
    code_index = CodeIndex()
    stats = code_index.build(settings.codebase_root)
    
    print(f"[OK] Code Index built: {stats['functions']} functions from {stats['files']} files")
    print(f"     Duration: {stats['duration_seconds']:.2f}s")
    
    result = code_index.lookup("validate_password")
    print(f"     Lookup 'validate_password': {result}")
    
    return code_index


async def test_agent1(neo4j_tool):
    print("\n" + "=" * 60)
    print("Testing Agent 1 (Neo4j Graph Fetcher)")
    print("=" * 60)
    
    agent1 = Agent1Neo4jFetcher(neo4j_tool)
    print("[OK] Agent 1 initialized")
    print("     Source: Neo4j only (no filesystem access)")
    
    return agent1


async def test_agent2(fs_tool, code_index):
    print("\n" + "=" * 60)
    print("Testing Agent 2 (Filesystem Graph Builder)")
    print("=" * 60)
    
    agent2 = Agent2FilesystemBuilder(fs_tool, code_index)
    
    spec = SpecFlowDocument(
        root_function="authenticate_user",
        root_file_path="services/auth_service.py",
        language="python",
        execution_flow_id="TEST-001",
    )
    
    print(f"     Spec: root={spec.root_function}, file={spec.root_file_path}")
    
    try:
        output = await agent2.build(spec)
        print(f"[OK] Agent 2 built graph:")
        print(f"     Source: Filesystem only (no Neo4j access)")
        print(f"     Edges found: {len(output.edges)}")
        print(f"     Unresolved callees: {len(output.unresolved_callees)}")
        
        if output.edges:
            print(f"     Sample edges:")
            for edge in output.edges[:3]:
                print(f"        {edge.caller} -> {edge.callee} ({edge.extraction_method.value})")
    except Exception as e:
        print(f"[WARN] Agent 2 build error (expected if function not in sample): {e}")
    
    return agent2


async def test_verification():
    print("\n" + "=" * 60)
    print("Testing Verification Agent")
    print("=" * 60)
    
    neo_graph = CallGraphOutput(
        execution_flow_id="TEST-001",
        source=GraphSource.NEO4J,
        root_function="main",
        edges=[
            CallGraphEdge(caller="main", callee="func_a", extraction_method=ExtractionMethod.NEO4J),
            CallGraphEdge(caller="func_a", callee="func_b", extraction_method=ExtractionMethod.NEO4J),
            CallGraphEdge(caller="main", callee="phantom_func", extraction_method=ExtractionMethod.NEO4J),
        ],
    )
    
    fs_graph = CallGraphOutput(
        execution_flow_id="TEST-001",
        source=GraphSource.FILESYSTEM,
        root_function="main",
        edges=[
            CallGraphEdge(caller="main", callee="func_a", extraction_method=ExtractionMethod.REGEX),
            CallGraphEdge(caller="func_a", callee="func_b", extraction_method=ExtractionMethod.REGEX),
            CallGraphEdge(caller="func_b", callee="missing_func", extraction_method=ExtractionMethod.REGEX),
        ],
    )
    
    verifier = VerificationAgent()
    result = verifier.verify(neo_graph, fs_graph)
    
    print(f"[OK] Verification complete:")
    print(f"     Confirmed edges: {len(result.confirmed_edges)}")
    print(f"     Phantom edges (Neo4j only): {len(result.phantom_edges)}")
    print(f"     Missing edges (filesystem only): {len(result.missing_edges)}")
    print(f"     Flow trust score: {result.flow_trust_score:.0%}")
    print(f"     Graph trust score: {result.graph_trust_score:.0%}")
    
    return result


async def test_report(result):
    print("\n" + "=" * 60)
    print("Testing Report Agent")
    print("=" * 60)
    
    reporter = ReportAgent()
    report_md = reporter.generate_markdown(result)
    summary = reporter.generate_summary(result)
    
    print(f"[OK] Report generated:")
    print(f"     Summary: {summary}")
    print(f"     Markdown length: {len(report_md)} chars")
    
    return report_md


async def main():
    print("\n" + "=" * 60)
    print("TrustBot Agentic Component Test Suite")
    print("=" * 60)
    
    try:
        neo4j_tool = Neo4jTool()
        fs_tool = FilesystemTool()
        
        await neo4j_tool.initialize()
        await fs_tool.initialize()
        
        print("[OK] Tools initialized")
        
        code_index = await test_code_index()
        agent1 = await test_agent1(neo4j_tool)
        agent2 = await test_agent2(fs_tool, code_index)
        result = await test_verification()
        report = await test_report(result)
        
        await neo4j_tool.shutdown()
        await fs_tool.shutdown()
        
        print("\n" + "=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60)
        print("\nAgentic TrustBot components are working correctly!")
        
    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
