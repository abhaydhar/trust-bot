#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Detailed content analyzer - extracts specific sections from the UI.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    """Analyze specific content sections."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return 1

    output_dir = Path("data/ui_analysis")
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1200})
        page = await context.new_page()

        try:
            print("Connecting to http://localhost:7860...")
            await page.goto("http://localhost:7860", timeout=30000)
            await asyncio.sleep(3)

            # Navigate to Validate tab
            validate_tab = page.locator("button[role='tab']:has-text('2. Validate')").first
            await validate_tab.click(force=True)
            await asyncio.sleep(2)
            print("[OK] On Validate tab")

            # Get full HTML
            html_content = await page.content()
            html_file = output_dir / "full_page.html"
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"[OK] Saved HTML: {html_file}")

            # Analyze Call Tree Diagrams
            print("\n" + "="*60)
            print("ANALYZING CALL TREE DIAGRAMS")
            print("="*60)
            
            # Click to expand
            try:
                accordion = page.locator("text=Call Tree Diagrams").first
                await accordion.click(force=True, timeout=3000)
                await asyncio.sleep(2)
            except:
                pass

            # Check for iframe
            iframes = await page.query_selector_all("iframe")
            print(f"Found {len(iframes)} iframe(s) on page")
            
            for i, iframe in enumerate(iframes):
                src = await iframe.get_attribute("src")
                srcdoc = await iframe.get_attribute("srcdoc")
                print(f"\nIframe {i+1}:")
                print(f"  src: {src if src else 'None'}")
                print(f"  srcdoc: {'Present (length=' + str(len(srcdoc)) + ')' if srcdoc else 'None'}")
                
                if srcdoc:
                    # Save srcdoc content
                    srcdoc_file = output_dir / f"iframe_{i+1}_content.html"
                    with open(srcdoc_file, 'w', encoding='utf-8') as f:
                        f.write(srcdoc)
                    print(f"  Saved to: {srcdoc_file}")
                    
                    # Check for Mermaid
                    if "mermaid" in srcdoc.lower():
                        print("  [OK] Contains Mermaid code!")
                        if "mermaid.min.js" in srcdoc or "cdn.jsdelivr.net" in srcdoc:
                            print("  [OK] Mermaid CDN script included!")
                        if "graph TD" in srcdoc or "flowchart" in srcdoc:
                            print("  [OK] Contains Mermaid diagram syntax!")

            # Check page content for Mermaid
            if "mermaid.min.js" in html_content:
                print("\n[OK] Mermaid JS found in page")
            if "graph TD" in html_content or "flowchart" in html_content:
                print("[OK] Mermaid diagram syntax found in page")
            
            # Analyze Detailed Report
            print("\n" + "="*60)
            print("ANALYZING DETAILED REPORT")
            print("="*60)
            
            try:
                accordion = page.locator("text=Detailed Report").first
                await accordion.click(force=True, timeout=3000)
                await asyncio.sleep(2)
            except:
                pass

            # Look for code blocks
            code_blocks = await page.query_selector_all("pre, code")
            print(f"Found {len(code_blocks)} code block(s)")
            
            for i, block in enumerate(code_blocks[:5]):  # First 5 only
                text = await block.inner_text()
                if "[ROOT]" in text or "|--" in text or "├──" in text:
                    print(f"\nCode block {i+1} contains tree structure:")
                    lines = text.split('\n')[:10]
                    for line in lines:
                        print(f"  {line}")
                    if len(text.split('\n')) > 10:
                        print(f"  ... ({len(text.split('\n'))} total lines)")

            # Check for specific text patterns
            print("\n" + "="*60)
            print("SEARCHING FOR KEY PATTERNS")
            print("="*60)
            
            patterns = {
                "[ROOT]": "[ROOT] marker",
                "|--": "Tree branch (|--)",
                "├──": "Tree branch (├──)",
                "└──": "Tree branch (└──)",
                "`--": "Tree branch (`--)",
                "Call Tree:": "Call Tree label",
                "Agent 1": "Agent 1 section",
                "Agent 2": "Agent 2 section",
                "Neo4j": "Neo4j references",
                "Index": "Index references",
            }
            
            for pattern, desc in patterns.items():
                count = html_content.count(pattern)
                print(f"  {desc}: {count} occurrence(s)")

            # Extract summary section
            print("\n" + "="*60)
            print("EXTRACTING SUMMARY")
            print("="*60)
            
            try:
                summary_elem = page.locator("div.prose, div.markdown").first
                summary_text = await summary_elem.inner_text()
                summary_file = output_dir / "summary.txt"
                with open(summary_file, 'w', encoding='utf-8') as f:
                    f.write(summary_text)
                print(f"[OK] Saved summary: {summary_file}")
                print("\nFirst 500 chars:")
                print(summary_text[:500])
            except:
                print("[WARN] Could not extract summary")

        finally:
            await browser.close()

    print("\n" + "="*60)
    print(f"Analysis complete. Files saved to: {output_dir.absolute()}")
    print("="*60)

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
