#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Manual test - Click accordions and capture exactly what renders.
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
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return 1

    screenshots_dir = Path("data/test_screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Headless is more reliable on Windows; use --headed for visible browser
        browser = await p.chromium.launch(
            headless=True,
            slow_mo=500,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1200})
        page = await context.new_page()

        try:
            print("\n" + "="*70)
            print("Connecting to http://127.0.0.1:7860")
            print("="*70)
            
            await page.goto("http://127.0.0.1:7860", timeout=30000)
            await asyncio.sleep(3)
            
            # Check for validation results
            content = await page.content()
            if "84%" in content or "trust" in content.lower():
                print("[OK] Page shows validation results with trust score")
            else:
                print("[INFO] Validation results may not be visible yet")

            # STEP 1: Scroll to bottom to see accordions
            print("\n" + "="*70)
            print("STEP 1: Scrolling to accordion panels")
            print("="*70)
            
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            
            screenshot = screenshots_dir / "page_bottom_accordions.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            print(f"[SCREENSHOT] {screenshot.name}")

            # STEP 2: Click "Call Tree Diagrams"
            print("\n" + "="*70)
            print("STEP 2: Expanding 'Call Tree Diagrams'")
            print("="*70)
            
            try:
                calltree_accordion = page.locator("text=Call Tree Diagrams").first
                await calltree_accordion.scroll_into_view_if_needed()
                await asyncio.sleep(1)
                await calltree_accordion.click(force=True)
                print("[OK] Clicked 'Call Tree Diagrams'")
                
                print("Waiting 3 seconds...")
                await asyncio.sleep(3)
                
                screenshot = screenshots_dir / "accordion_calltree.png"
                await page.screenshot(path=str(screenshot), full_page=True)
                print(f"[SCREENSHOT] {screenshot.name}")
                
                # Analyze what's visible
                print("\n[ANALYSIS] Checking Call Tree Diagrams content:")
                content = await page.content()
                
                # Check for iframes
                iframes = await page.query_selector_all("iframe")
                print(f"  - Iframe elements found: {len(iframes)}")
                
                if len(iframes) > 0:
                    print("  [SUCCESS] Visual flowchart diagrams should be rendered in iframe!")
                    for i, iframe in enumerate(iframes):
                        src = await iframe.get_attribute("src")
                        srcdoc_attr = await iframe.get_attribute("srcdoc")
                        print(f"    Iframe {i+1}: src={src}, has_srcdoc={srcdoc_attr is not None}")
                else:
                    print("  [INFO] No iframe found")
                
                # Check for Mermaid
                if "mermaid" in content.lower():
                    print("  - Mermaid references: YES")
                else:
                    print("  - Mermaid references: NO")
                
                # Check for raw code
                if "graph TD" in content or "flowchart" in content:
                    print("  [WARN] Raw Mermaid code visible (not rendered)")
                
                # Check for "No diagrams" message
                if "No call tree diagrams" in content:
                    print("  [INFO] Message: 'No call tree diagrams to display'")
                    
            except Exception as e:
                print(f"[ERROR] Could not expand Call Tree Diagrams: {e}")

            # STEP 3: Click "Detailed Report"
            print("\n" + "="*70)
            print("STEP 3: Expanding 'Detailed Report'")
            print("="*70)
            
            try:
                report_accordion = page.locator("text=Detailed Report").first
                await report_accordion.scroll_into_view_if_needed()
                await asyncio.sleep(1)
                await report_accordion.click(force=True)
                print("[OK] Clicked 'Detailed Report'")
                
                print("Waiting 2 seconds...")
                await asyncio.sleep(2)
                
                # First screenshot
                screenshot = screenshots_dir / "accordion_report_1.png"
                await page.screenshot(path=str(screenshot), full_page=True)
                print(f"[SCREENSHOT] {screenshot.name}")
                
                # Analyze content
                print("\n[ANALYSIS] Checking Detailed Report content:")
                content = await page.content()
                
                # Check for Agent sections
                if "Agent 1" in content:
                    print("  - Agent 1 section: YES")
                else:
                    print("  - Agent 1 section: NO")
                    
                if "Agent 2" in content:
                    print("  - Agent 2 section: YES")
                else:
                    print("  - Agent 2 section: NO")
                
                # Check for text call trees
                has_root = "[ROOT]" in content
                has_tree_chars = "|--" in content or "├──" in content or "└──" in content or "`--" in content
                
                print(f"  - [ROOT] markers: {'YES' if has_root else 'NO'}")
                print(f"  - Tree branch chars: {'YES' if has_tree_chars else 'NO'}")
                
                # Check for code blocks
                code_blocks = await page.query_selector_all("pre, code")
                print(f"  - Code blocks found: {len(code_blocks)}")
                
                # Check for tables
                tables = await page.query_selector_all("table")
                print(f"  - Tables found: {len(tables)}")
                
                # Sample code blocks
                if len(code_blocks) > 0:
                    print("\n  [INFO] Checking first few code blocks:")
                    for i, block in enumerate(code_blocks[:3]):
                        text = await block.inner_text()
                        if len(text) > 10:
                            has_root = "[ROOT]" in text
                            has_tree = "|--" in text or "├──" in text
                            print(f"    Block {i+1}: {len(text)} chars, ROOT={has_root}, tree={has_tree}")
                            if has_root or has_tree:
                                lines = text.split('\n')[:5]
                                print(f"      Preview: {lines}")
                
                # STEP 4: Scroll through Detailed Report
                print("\n" + "="*70)
                print("STEP 4: Scrolling through Detailed Report")
                print("="*70)
                
                # Scroll down incrementally
                for i in range(2, 6):
                    await page.evaluate(f"window.scrollBy(0, 400)")
                    await asyncio.sleep(1)
                    
                    screenshot = screenshots_dir / f"accordion_report_{i}.png"
                    await page.screenshot(path=str(screenshot), full_page=True)
                    print(f"[SCREENSHOT] {screenshot.name}")
                
                # Full page final screenshot
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(1)
                screenshot = screenshots_dir / "full_page_final.png"
                await page.screenshot(path=str(screenshot), full_page=True)
                print(f"[SCREENSHOT] {screenshot.name}")
                    
            except Exception as e:
                print(f"[ERROR] Could not expand Detailed Report: {e}")

            print("\n[INFO] Keeping browser open for 10 seconds for manual review...")
            await asyncio.sleep(10)

        finally:
            await browser.close()

    print("\n" + "="*70)
    print("TEST COMPLETE")
    print("="*70)
    print(f"\nScreenshots saved to: {screenshots_dir.absolute()}")
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
