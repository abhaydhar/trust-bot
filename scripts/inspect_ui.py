#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Simple UI inspection script - checks what's currently displayed on TrustBot UI.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

# Fix Windows encoding issues
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    """Inspect the current state of TrustBot UI."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return 1

    screenshots_dir = Path("data/screenshots_manual")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1920, "height": 1200})
        page = await context.new_page()

        try:
            print("\n" + "="*60)
            print("Connecting to http://localhost:7860")
            print("="*60)
            
            response = await page.goto("http://localhost:7860", timeout=30000)
            if not response or response.status != 200:
                print(f"[X] Failed to load page: {response}")
                return 1

            # Wait for page
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except:
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)

            print("[OK] Page loaded")
            
            # Take initial screenshot
            screenshot_path = screenshots_dir / "00_initial.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"[OK] Screenshot: {screenshot_path}")

            # Get page content for analysis
            content = await page.content()
            
            # Check tabs
            print("\n" + "="*60)
            print("CHECKING TABS")
            print("="*60)
            tabs = ["1. Code Indexer", "2. Validate", "3. Chunk Visualizer", "4. Chat", "5. Index Management"]
            for tab in tabs:
                if tab in content:
                    print(f"[OK] Found: {tab}")

            # Navigate to Validate tab
            print("\n" + "="*60)
            print("NAVIGATING TO '2. Validate' TAB")
            print("="*60)
            
            validate_tab = page.locator("button[role='tab']:has-text('2. Validate')").first
            await validate_tab.click(force=True)
            await asyncio.sleep(2)
            print("[OK] Clicked Validate tab")
            
            screenshot_path = screenshots_dir / "01_validate_tab.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"[OK] Screenshot: {screenshot_path}")

            # Check for validation results
            content = await page.content()
            
            has_summary = "Summary" in content or "trust" in content.lower()
            has_3agent = "3-Agent" in content
            has_trust_score = "Trust Score" in content or "trust score" in content.lower()
            
            print("\n" + "="*60)
            print("CHECKING FOR VALIDATION RESULTS")
            print("="*60)
            print(f"  Summary section: {has_summary}")
            print(f"  3-Agent text: {has_3agent}")
            print(f"  Trust score: {has_trust_score}")

            # Check for accordions
            print("\n" + "="*60)
            print("CHECKING ACCORDIONS")
            print("="*60)
            
            accordions = [
                "Detailed Report",
                "Call Tree Diagrams",
                "Agent 1 Output",
                "Agent 2 Output",
                "Raw JSON"
            ]
            
            for accordion_name in accordions:
                if accordion_name in content:
                    print(f"[OK] Found: {accordion_name}")
                    
                    # Try to find and click it
                    try:
                        accordion = page.locator(f"text={accordion_name}").first
                        if await accordion.count() > 0:
                            await accordion.click(force=True, timeout=2000)
                            await asyncio.sleep(1)
                            print(f"      Expanded accordion")
                            
                            # Take screenshot
                            safe_name = accordion_name.lower().replace(" ", "_")
                            screenshot_path = screenshots_dir / f"accordion_{safe_name}.png"
                            await page.screenshot(path=str(screenshot_path), full_page=True)
                            print(f"      Screenshot: {screenshot_path}")
                    except:
                        pass
                else:
                    print(f"[X] Not found: {accordion_name}")

            # Check for Mermaid diagrams in Call Tree
            print("\n" + "="*60)
            print("CHECKING CALL TREE DIAGRAMS")
            print("="*60)
            
            content = await page.content()
            if "<iframe" in content and "mermaid" in content.lower():
                print("[SUCCESS] Mermaid diagrams rendered in iframe!")
            elif "graph TD" in content or "flowchart" in content:
                print("[WARN] Raw Mermaid code found (not rendered)")
            elif "No call tree diagrams" in content:
                print("[INFO] No diagrams to display")
            else:
                print("[INFO] Call tree status unclear")

            # Check for text call trees in Detailed Report
            print("\n" + "="*60)
            print("CHECKING TEXT CALL TREES IN DETAILED REPORT")
            print("="*60)
            
            has_root = "[ROOT]" in content
            has_tree_chars = "|--" in content or "`--" in content or "└──" in content or "├──" in content
            has_agent1 = "Agent 1" in content
            has_agent2 = "Agent 2" in content
            
            print(f"  Agent 1 section: {has_agent1}")
            print(f"  Agent 2 section: {has_agent2}")
            print(f"  [ROOT] marker: {has_root}")
            print(f"  Tree format chars: {has_tree_chars}")

            # Final comprehensive screenshot
            print("\n" + "="*60)
            print("TAKING FINAL SCREENSHOTS")
            print("="*60)
            
            # Scroll to see more content
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
            screenshot_path = screenshots_dir / "final_full_page.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"[OK] Full page: {screenshot_path}")

            print("\n" + "="*60)
            print("INSPECTION COMPLETE")
            print("="*60)
            print(f"\nAll screenshots saved to: {screenshots_dir.absolute()}")
            print("\nPress Enter to close browser...")
            await asyncio.sleep(5)  # Give time to review

        finally:
            await browser.close()

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
