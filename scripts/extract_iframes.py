#!/usr/bin/env python
"""Extract and analyze iframe content from the saved HTML."""

import re
from pathlib import Path

html_file = Path("data/ui_analysis/full_page.html")
output_dir = Path("data/ui_analysis")

with open(html_file, 'r', encoding='utf-8') as f:
    content = f.read()

# Find iframes with srcdoc
iframe_pattern = r'<iframe[^>]*srcdoc=&quot;([^&]*)&quot;'
matches = re.findall(iframe_pattern, content, re.DOTALL)

print(f"Found {len(matches)} iframe(s) with srcdoc")

for i, match in enumerate(matches, 1):
    # Decode HTML entities
    import html
    decoded = html.unescape(match)
    
    iframe_file = output_dir / f"iframe_{i}.html"
    with open(iframe_file, 'w', encoding='utf-8') as f:
        f.write(decoded)
    
    print(f"\nIframe {i}:")
    print(f"  Length: {len(decoded)} chars")
    print(f"  Has Mermaid: {'mermaid' in decoded.lower()}")
    print(f"  Has Mermaid CDN: {'cdn.jsdelivr.net' in decoded}")
    print(f"  Has graph TD: {'graph TD' in decoded}")
    print(f"  Has flowchart: {'flowchart' in decoded}")
    print(f"  Saved to: {iframe_file}")
    
    # Extract Mermaid code
    mermaid_pattern = r'<pre class="mermaid">([^<]*)</pre>'
    mermaid_matches = re.findall(mermaid_pattern, decoded, re.DOTALL)
    if mermaid_matches:
        print(f"  Found {len(mermaid_matches)} Mermaid diagram(s)")
        for j, mermaid_code in enumerate(mermaid_matches, 1):
            print(f"\n    Mermaid {j} (first 150 chars):")
            print(f"    {mermaid_code[:150]}")

# Search for text call trees in code blocks
code_block_pattern = r'```([^`]+)```'
code_blocks = re.findall(code_block_pattern, content)

print(f"\n\nFound {len(code_blocks)} code block(s) with triple backticks")

for i, block in enumerate(code_blocks[:5], 1):
    if '[ROOT]' in block or '|--' in block or '├──' in block:
        print(f"\nCode block {i} contains tree structure (first 200 chars):")
        print(block[:200])
        
        # Save it
        tree_file = output_dir / f"text_tree_{i}.txt"
        with open(tree_file, 'w', encoding='utf-8') as f:
            f.write(block)
        print(f"  Saved to: {tree_file}")

print("\n" + "="*60)
print("Analysis complete!")
