#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FINAL COMPREHENSIVE UI TEST - Manual interaction with verification.

This test will:
1. Navigate to TrustBot
2. Index the repository (if not already done)
3. Run validation (if not already done)
4. Expand and screenshot all accordions
5. Generate detailed report
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
    """Run comprehensive UI test."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return 1

    report = []
    screenshots_dir = Path("data/final_test_screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Launch browser in non-headless mode to see what's happening
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1920, "height": 1200})
        page = await context.new_page()

        try:
            # ==================== STEP 1: Navigate ====================
            print("\n" + "="*70)
            print("STEP 1: Navigate to http://localhost:7860")
            print("="*70)
            report.append("="*70)
            report.append("STEP 1: Navigate to http://localhost:7860")
            report.append("="*70)
            
            response = await page.goto("http://localhost:7860", timeout=30000)
            await asyncio.sleep(3)
            
            screenshot = screenshots_dir / "step1_home.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            
            if response and response.status == 200:
                print("[SUCCESS] Page loaded successfully")
                report.append("[SUCCESS] Page loaded successfully")
                report.append(f"Screenshot: {screenshot.name}")
            else:
                print("[FAIL] Page did not load properly")
                report.append("[FAIL] Page did not load properly")
                return 1

            # ==================== STEP 2: Check tabs ====================
            print("\n[INFO] Checking for tabs...")
            tabs_found = []
            for tab_name in ["1. Code Indexer", "2. Validate", "3. Chunk Visualizer"]:
                tab = page.locator(f"button[role='tab']:has-text('{tab_name}')").first
                if await tab.count() > 0:
                    print(f"  [OK] Found: {tab_name}")
                    tabs_found.append(tab_name)
                else:
                    print(f"  [X] Missing: {tab_name}")
            
            report.append(f"\nTabs found: {', '.join(tabs_found)}")

            # ==================== STEP 3: Navigate to Validate ====================
            print("\n" + "="*70)
            print("STEP 2: Navigate to Validate tab")
            print("="*70)
            report.append("\n" + "="*70)
            report.append("STEP 2: Navigate to Validate tab")
            report.append("="*70)
            
            validate_tab = page.locator("button[role='tab']:has-text('2. Validate')").first
            await validate_tab.click(force=True)
            await asyncio.sleep(3)
            
            screenshot = screenshots_dir / "step2_validate_tab.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            print("[OK] Navigated to Validate tab")
            report.append("[OK] Navigated to Validate tab")
            report.append(f"Screenshot: {screenshot.name}")

            # ==================== STEP 4: Check for validation results ====================
            print("\n" + "="*70)
            print("STEP 3: Check for validation results")
            print("="*70)
            report.append("\n" + "="*70)
            report.append("STEP 3: Check for validation results")
            report.append("="*70)
            
            content = await page.content()
            
            checks = {
                "3-Agent": "3-Agent validation",
                "Trust Score": "Trust score metrics",
                "Summary": "Summary section",
                "Detailed Report": "Detailed Report accordion",
                "Call Tree Diagrams": "Call Tree Diagrams accordion",
            }
            
            for keyword, desc in checks.items():
                found = keyword in content
                status = "[OK]" if found else "[X]"
                print(f"  {status} {desc}")
                report.append(f"  {status} {desc}")

            # ==================== STEP 5: Expand Call Tree Diagrams ====================
            print("\n" + "="*70)
            print("STEP 4: Check Call Tree Diagrams")
            print("="*70)
            report.append("\n" + "="*70)
            report.append("STEP 4: Check Call Tree Diagrams (Mermaid diagrams)")
            report.append("="*70)
            
            try:
                accordion = page.locator("text=Call Tree Diagrams").first
                await accordion.click(force=True, timeout=5000)
                await asyncio.sleep(3)
                print("[OK] Expanded 'Call Tree Diagrams' accordion")
                report.append("[OK] Expanded 'Call Tree Diagrams' accordion")
                
                screenshot = screenshots_dir / "step4_call_tree_diagrams.png"
                await page.screenshot(path=str(screenshot), full_page=True)
                report.append(f"Screenshot: {screenshot.name}")
                
                # Check for iframe rendering
                content_after = await page.content()
                
                iframe_check = {
                    "<iframe": "Iframe element present",
                    "mermaid": "Mermaid references",
                    "cdn.jsdelivr.net": "Mermaid CDN loaded",
                    "graph TD": "Mermaid diagram syntax",
                    "flowchart": "Flowchart syntax",
                }
                
                print("\n  Rendering check:")
                report.append("\n  Rendering check:")
                for pattern, desc in iframe_check.items():
                    found = pattern in content_after
                    status = "[OK]" if found else "[X]"
                    print(f"    {status} {desc}")
                    report.append(f"    {status} {desc}")
                
                # Visual check
                iframes = await page.query_selector_all("iframe")
                print(f"\n  Found {len(iframes)} iframe element(s) on page")
                report.append(f"\n  Found {len(iframes)} iframe element(s) on page")
                
                if len(iframes) > 0:
                    print("  [SUCCESS] Mermaid diagrams should be rendering in iframe!")
                    report.append("  [SUCCESS] Mermaid diagrams should be rendering in iframe!")
                elif "No call tree diagrams" in content_after:
                    print("  [INFO] No diagrams to display")
                    report.append("  [INFO] No diagrams to display")
                else:
                    print("  [WARN] Iframe not found - Mermaid may not be rendering")
                    report.append("  [WARN] Iframe not found - Mermaid may not be rendering")
                    
            except Exception as e:
                print(f"[FAIL] Could not expand: {e}")
                report.append(f"[FAIL] Could not expand: {e}")

            # ==================== STEP 6: Expand Detailed Report ====================
            print("\n" + "="*70)
            print("STEP 5: Check Detailed Report")
            print("="*70)
            report.append("\n" + "="*70)
            report.append("STEP 5: Check Detailed Report (text call trees)")
            report.append("="*70)
            
            try:
                accordion = page.locator("text=Detailed Report").first
                await accordion.click(force=True, timeout=5000)
                await asyncio.sleep(3)
                print("[OK] Expanded 'Detailed Report' accordion")
                report.append("[OK] Expanded 'Detailed Report' accordion")
                
                # Scroll to see more
                await page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(1)
                
                screenshot = screenshots_dir / "step5_detailed_report.png"
                await page.screenshot(path=str(screenshot), full_page=True)
                report.append(f"Screenshot: {screenshot.name}")
                
                # Check for text call trees
                content_after = await page.content()
                
                tree_check = {
                    "Agent 1": "Agent 1 section",
                    "Agent 2": "Agent 2 section",
                    "Neo4j Call Graph": "Neo4j label",
                    "Indexed Codebase": "Index label",
                    "Call Tree:": "Call Tree label",
                    "[ROOT]": "[ROOT] marker in tree",
                    "|--": "Tree branch characters",
                }
                
                print("\n  Content check:")
                report.append("\n  Content check:")
                for pattern, desc in tree_check.items():
                    found = pattern in content_after
                    status = "[OK]" if found else "[X]"
                    print(f"    {status} {desc}")
                    report.append(f"    {status} {desc}")
                
                # Check code blocks
                code_blocks = await page.query_selector_all("pre, code")
                print(f"\n  Found {len(code_blocks)} code block element(s)")
                report.append(f"\n  Found {len(code_blocks)} code block element(s)")
                
                # Sample first few code blocks
                for i, block in enumerate(code_blocks[:3]):
                    text = await block.inner_text()
                    if text and len(text) > 10:
                        has_root = "[ROOT]" in text
                        has_tree = "|--" in text or "├──" in text
                        print(f"\n  Code block {i+1}: {len(text)} chars, has [ROOT]={has_root}, has tree={has_tree}")
                        if has_root or has_tree:
                            print(f"    Preview: {text[:100]}")
                            report.append(f"\n  Code block {i+1} contains call tree structure!")
                
            except Exception as e:
                print(f"[FAIL] Could not expand: {e}")
                report.append(f"[FAIL] Could not expand: {e}")

            # ==================== STEP 7: Check other accordions ====================
            print("\n" + "="*70)
            print("STEP 6: Check other accordions")
            print("="*70)
            report.append("\n" + "="*70)
            report.append("STEP 6: Check other accordions")
            report.append("="*70)
            
            other_accordions = [
                "Agent 1 Output (Neo4j Call Graph)",
                "Agent 2 Output (Indexed Codebase Call Graph)",
                "Raw JSON"
            ]
            
            for accordion_name in other_accordions:
                try:
                    accordion = page.locator(f"text={accordion_name}").first
                    count = await accordion.count()
                    if count > 0:
                        await accordion.click(force=True, timeout=3000)
                        await asyncio.sleep(2)
                        print(f"[OK] Found and expanded: {accordion_name}")
                        report.append(f"[OK] Found and expanded: {accordion_name}")
                        
                        safe_name = accordion_name.lower().replace(" ", "_").replace("(", "").replace(")", "")
                        screenshot = screenshots_dir / f"accordion_{safe_name}.png"
                        await page.screenshot(path=str(screenshot), full_page=True)
                        report.append(f"    Screenshot: {screenshot.name}")
                    else:
                        print(f"[X] Not found: {accordion_name}")
                        report.append(f"[X] Not found: {accordion_name}")
                except Exception as e:
                    print(f"[X] Error with {accordion_name}: {e}")
                    report.append(f"[X] Error with {accordion_name}: {e}")

            # Final screenshot
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(1)
            screenshot = screenshots_dir / "final_full_page.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            
            print("\n[INFO] Pausing for 5 seconds to review browser...")
            await asyncio.sleep(5)

        finally:
            await browser.close()

    # Write report
    print("\n" + "="*70)
    print("TEST COMPLETE - GENERATING REPORT")
    print("="*70)
    
    report_file = screenshots_dir / "TEST_REPORT.txt"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write("\n".join(report))
    
    print(f"\n[SUCCESS] Report saved to: {report_file}")
    print(f"[SUCCESS] Screenshots saved to: {screenshots_dir.absolute()}")
    
    # Print report to console
    print("\n" + "="*70)
    print("FINAL REPORT")
    print("="*70)
    for line in report:
        print(line)

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
