#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Extract visible text content from each section for analysis.
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
    """Extract text content."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return 1

    output_dir = Path("data/text_extracts")
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1200})
        page = await context.new_page()

        try:
            print("Connecting...")
            await page.goto("http://localhost:7860", timeout=30000)
            await asyncio.sleep(3)

            # Go to Validate tab
            validate_tab = page.locator("button[role='tab']:has-text('2. Validate')").first
            await validate_tab.click(force=True)
            await asyncio.sleep(3)

            # Extract summary
            print("\nExtracting Summary...")
            try:
                # Try to find the summary markdown/prose container
                summary_elements = page.locator("div.prose, div.markdown, .gradio-markdown").all()
                all_summaries = await summary_elements
                
                if len(await summary_elements.count()) > 0:
                    summary_text = await all_summaries[0].inner_text()
                    file_path = output_dir / "summary.txt"
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(summary_text)
                    print(f"  Saved: {file_path}")
                    print(f"  Length: {len(summary_text)} chars")
                    print(f"  Preview:\n{summary_text[:300]}\n")
            except Exception as e:
                print(f"  Error: {e}")

            # Extract each accordion
            accordions = [
                ("Call Tree Diagrams", "call_tree_diagrams"),
                ("Detailed Report", "detailed_report"),
                ("Agent 1 Output", "agent1_output"),
                ("Agent 2 Output", "agent2_output"),
                ("Raw JSON", "raw_json"),
            ]

            for accordion_name, safe_name in accordions:
                print(f"\nExtracting {accordion_name}...")
                try:
                    accordion = page.locator(f"text={accordion_name}").first
                    await accordion.click(force=True, timeout=5000)
                    await asyncio.sleep(2)
                    
                    # Get the content after the accordion header
                    # Find the parent accordion container and get all text
                    content_text = await page.evaluate("""
                        () => {
                            const accordions = document.querySelectorAll('[class*="accordion"]');
                            for (let acc of accordions) {
                                if (acc.textContent.includes('""" + accordion_name + """')) {
                                    return acc.textContent;
                                }
                            }
                            return "";
                        }
                    """)
                    
                    file_path = output_dir / f"{safe_name}.txt"
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content_text)
                    
                    print(f"  Saved: {file_path}")
                    print(f"  Length: {len(content_text)} chars")
                    
                    # Check for key patterns
                    has_iframe = "<iframe" in content_text.lower()
                    has_mermaid = "mermaid" in content_text.lower()
                    has_root = "[ROOT]" in content_text
                    has_tree = "|--" in content_text or "├──" in content_text
                    
                    print(f"  Has iframe ref: {has_iframe}")
                    print(f"  Has mermaid ref: {has_mermaid}")
                    print(f"  Has [ROOT]: {has_root}")
                    print(f"  Has tree chars: {has_tree}")
                    
                    if len(content_text) > 100:
                        print(f"  Preview:\n{content_text[:200]}\n")
                    
                except Exception as e:
                    print(f"  Error: {e}")

        finally:
            await browser.close()

    print("\n" + "="*70)
    print(f"Text extracts saved to: {output_dir.absolute()}")
    print("="*70)

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
