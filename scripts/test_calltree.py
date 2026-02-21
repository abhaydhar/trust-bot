"""Test call tree builder output."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from trustbot.models.agentic import CallGraphEdge, CallGraphOutput, GraphSource
from trustbot.ui.call_tree import build_text_tree, build_mermaid

graph = CallGraphOutput(
    execution_flow_id='test',
    source=GraphSource.NEO4J,
    root_function='InitialiseEcran',
    edges=[
        CallGraphEdge(caller='InitialiseEcran', callee='ChargeArborescence',
                      caller_file='fMain.pas', callee_file='fMain.pas'),
        CallGraphEdge(caller='ChargeArborescence', callee='ChargeArborescence',
                      caller_file='fMain.pas', callee_file='fMain.pas'),
    ],
)

print('=== TEXT TREE ===')
print(build_text_tree(graph))
print()

# More complex example
graph2 = CallGraphOutput(
    execution_flow_id='test2',
    source=GraphSource.NEO4J,
    root_function='Form1',
    edges=[
        CallGraphEdge(caller='Form1', callee='Button2Click',
                      caller_file='Unit1.dfm', callee_file='Unit1.pas'),
        CallGraphEdge(caller='Button2Click', callee='TraitementDeLaBase',
                      caller_file='Unit1.pas', callee_file='Unit3.pas'),
        CallGraphEdge(caller='Form1', callee='Button1Click',
                      caller_file='Unit1.dfm', callee_file='Unit1.pas'),
    ],
)

print('=== COMPLEX TEXT TREE ===')
print(build_text_tree(graph2))
print()

print('=== MERMAID (simple) ===')
print(build_mermaid(graph))
print()
print('=== MERMAID (complex) ===')
print(build_mermaid(graph2))
