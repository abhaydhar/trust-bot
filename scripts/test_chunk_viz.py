"""Test chunk visualization components."""

import asyncio
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from trustbot.index.code_index import CodeIndex
from trustbot.indexing.chunk_visualizer import ChunkVisualizer

async def test_viz():
    """Test chunk visualization."""
    print("Testing Chunk Visualizer...")
    
    # Initialize code index
    print("\n1. Building code index...")
    code_index = CodeIndex("c:\\Abhay\\trust-bot\\sample_codebase")
    code_index.build()
    
    # Test visualizer
    print("\n2. Getting chunk data...")
    viz = ChunkVisualizer(code_index)
    try:
        data = await viz.get_graph_data()
        print(f"   Nodes: {len(data.get('nodes', []))}")
        print(f"   Edges: {len(data.get('edges', []))}")
        
        if data.get('nodes'):
            print("\n3. Sample nodes:")
            for node in data['nodes'][:3]:
                print(f"   - {node['name']} ({node['type']}) in {node['file']}")
        
        # Test HTML generation
        print("\n4. Generating HTML...")
        from trustbot.ui.app import _generate_chunk_html
        html = _generate_chunk_html(data)
        print(f"   HTML length: {len(html)} characters")
        print(f"   HTML preview: {html[:200]}...")
        
        print("\n✓ All tests passed!")
        return True
        
    except Exception as e:
        print(f"\n✗ Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        code_index.close()

if __name__ == "__main__":
    success = asyncio.run(test_viz())
    sys.exit(0 if success else 1)
