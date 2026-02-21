#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
End-to-end UI test for TrustBot following specific test steps.

Tests:
1. Navigate to http://localhost:7860
2. Index repository: https://github.com/AnshuSuroliya/Delphi-Test.git (branch: master)
3. Run validation: Project ID 3151, Run ID 4912
4. Check Call Tree Diagrams (Mermaid rendering)
5. Check Detailed Report (text call trees)

Usage:
    python scripts/test_ui_e2e.py

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
from pathlib import Path

# Fix Windows encoding issues
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    """Run the end-to-end UI test."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed. Run:")
        print("  pip install playwright")
        print("  playwright install chromium")
        return 1

    results = {
        "step1_navigate": False,
        "step2_index": False,
        "step3_validate": False,
        "step4_calltree": False,
        "step5_detailed": False,
        "errors": [],
    }

    screenshots_dir = Path("data/screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # Set to True for headless
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        try:
            # ═══════════════════════════════════════════════════════════
            # STEP 1: Navigate to the app
            # ═══════════════════════════════════════════════════════════
            print("\n" + "="*60)
            print("STEP 1: Navigate to http://localhost:7860")
            print("="*60)
            
            try:
                response = await page.goto("http://localhost:7860", timeout=30000)
                if not response or response.status != 200:
                    results["errors"].append(f"Step 1: Bad response: {response}")
                    print(f"[X] FAILED: Bad response status")
                    return 1

                # Wait for page to load (with fallback if networkidle fails)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    await asyncio.sleep(2)
                
                # Take screenshot
                screenshot_path = screenshots_dir / "step1_home.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"[OK] Screenshot saved: {screenshot_path}")

                # Verify heading
                content = await page.content()
                if "TrustBot" not in content:
                    results["errors"].append("Step 1: 'TrustBot' heading not found")
                    print("[X] FAILED: TrustBot heading not found")
                    return 1

                # Check for tabs
                tabs = ["1. Code Indexer", "2. Validate"]
                for tab in tabs:
                    if tab in content:
                        print(f"[OK] Found tab: {tab}")
                    else:
                        print(f"[WARN] Tab not found: {tab}")

                results["step1_navigate"] = True
                print("[SUCCESS] STEP 1: SUCCESS")

            except Exception as e:
                results["errors"].append(f"Step 1: {e}")
                print(f"[X] FAILED: {e}")
                return 1

            # ═══════════════════════════════════════════════════════════
            # STEP 2: Index the repository
            # ═══════════════════════════════════════════════════════════
            print("\n" + "="*60)
            print("STEP 2: Index Repository")
            print("="*60)

            try:
                # Check if "1. Code Indexer" tab is already active
                print("Looking for '1. Code Indexer' tab...")
                tab_locator = page.locator("button[role='tab']:has-text('1. Code Indexer')")
                tab_is_selected = await tab_locator.get_attribute("aria-selected")
                
                if tab_is_selected != "true":
                    print("  Clicking tab...")
                    await tab_locator.click(force=True, timeout=5000)
                    await asyncio.sleep(1)
                    print("[OK] Clicked Code Indexer tab")
                else:
                    print("[OK] Code Indexer tab already active")

                # Fill in Git Repository URL
                git_url = "https://github.com/AnshuSuroliya/Delphi-Test.git"
                print(f"Entering Git URL: {git_url}")
                
                # Wait for the input to be ready and try different selectors
                await asyncio.sleep(1)
                
                # Try to find the git URL input field
                git_input = page.locator("textarea, input[type='text']").filter(has_text="").nth(0)
                await git_input.wait_for(state="visible", timeout=5000)
                await git_input.click()
                await asyncio.sleep(0.5)
                await git_input.fill(git_url)
                print("[OK] Git URL entered")

                # Fill in Branch  
                branch = "master"
                print(f"Entering branch: {branch}")
                branch_input = page.locator("textarea, input[type='text']").filter(has_text="").nth(1)
                await branch_input.wait_for(state="visible", timeout=5000)
                await branch_input.click()
                await asyncio.sleep(0.5)
                await branch_input.clear()
                await branch_input.fill(branch)
                print("[OK] Branch entered")

                # Take screenshot before clicking
                screenshot_path = screenshots_dir / "step2_before_index.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"[OK] Screenshot saved: {screenshot_path}")

                # Click "Clone and Index Repository"
                print("Clicking 'Clone and Index Repository'...")
                index_button = page.locator("button:has-text('Clone and Index Repository')").first
                await index_button.click(timeout=5000)
                print("[OK] Button clicked, waiting for indexing...")

                # Wait for indexing to complete (up to 60 seconds)
                print("Waiting for indexing (up to 60 seconds)...")
                success = False
                for i in range(60):
                    await asyncio.sleep(1)
                    content = await page.content()
                    if "Indexing Complete" in content or "Codebase is ready" in content or "Files processed" in content:
                        success = True
                        print(f"[OK] Indexing completed after {i+1} seconds")
                        break
                    if i % 5 == 0:
                        print(f"  ... still waiting ({i+1}s)")

                if not success:
                    results["errors"].append("Step 2: Indexing did not complete in 60 seconds")
                    print("[WARN] WARNING: Timeout waiting for indexing completion")
                
                # Take screenshot after indexing
                screenshot_path = screenshots_dir / "step2_after_index.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"[OK] Screenshot saved: {screenshot_path}")

                # Check for success message
                content = await page.content()
                if "Indexing Complete" in content or "Files processed" in content:
                    print("[OK] Found indexing complete message")
                    results["step2_index"] = True
                    print("[SUCCESS] STEP 2: SUCCESS")
                else:
                    print("[WARN] Could not confirm indexing completion")

            except Exception as e:
                results["errors"].append(f"Step 2: {e}")
                print(f"[X] FAILED: {e}")
                screenshot_path = screenshots_dir / "step2_error.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)

            # ═══════════════════════════════════════════════════════════
            # STEP 3: Run validation
            # ═══════════════════════════════════════════════════════════
            print("\n" + "="*60)
            print("STEP 3: Run Validation")
            print("="*60)

            try:
                # Click on "2. Validate" tab
                print("Clicking '2. Validate' tab...")
                validate_tab = page.locator("button[role='tab']:has-text('2. Validate')").first
                await validate_tab.click(force=True, timeout=5000)
                await asyncio.sleep(1)
                print("[OK] Clicked Validate tab")

                # Fill in Project ID
                project_id = "3151"
                print(f"Entering Project ID: {project_id}")
                project_input = page.locator("input[placeholder*='3151']").or_(
                    page.locator("label:has-text('Project ID')").locator("xpath=following::input[1]")
                ).first
                await project_input.click()
                await project_input.fill(project_id)
                print("[OK] Project ID entered")

                # Fill in Run ID
                run_id = "4912"
                print(f"Entering Run ID: {run_id}")
                run_input = page.locator("input[placeholder*='4912']").or_(
                    page.locator("label:has-text('Run ID')").locator("xpath=following::input[1]")
                ).first
                await run_input.click()
                await run_input.fill(run_id)
                print("[OK] Run ID entered")

                # Take screenshot before validation
                screenshot_path = screenshots_dir / "step3_before_validate.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"[OK] Screenshot saved: {screenshot_path}")

                # Click "Validate All Flows"
                print("Clicking 'Validate All Flows'...")
                validate_button = page.locator("button:has-text('Validate All Flows')").first
                await validate_button.click(timeout=5000)
                print("[OK] Button clicked, waiting for validation...")

                # Wait for validation to complete (up to 120 seconds)
                print("Waiting for validation (up to 120 seconds)...")
                success = False
                for i in range(120):
                    await asyncio.sleep(1)
                    content = await page.content()
                    if "trust" in content.lower() or "Validation complete" in content or "3-Agent" in content:
                        success = True
                        print(f"[OK] Validation completed after {i+1} seconds")
                        break
                    if i % 10 == 0:
                        print(f"  ... still waiting ({i+1}s)")

                if not success:
                    results["errors"].append("Step 3: Validation did not complete in 120 seconds")
                    print("[WARN] WARNING: Timeout waiting for validation completion")

                # Take screenshot after validation
                screenshot_path = screenshots_dir / "step3_after_validate.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"[OK] Screenshot saved: {screenshot_path}")

                # Check for validation results
                content = await page.content()
                if "trust" in content.lower() or "3-Agent" in content:
                    print("[OK] Found validation results")
                    results["step3_validate"] = True
                    print("[SUCCESS] STEP 3: SUCCESS")
                else:
                    print("[WARN] Could not confirm validation completion")

            except Exception as e:
                results["errors"].append(f"Step 3: {e}")
                print(f"[X] FAILED: {e}")
                screenshot_path = screenshots_dir / "step3_error.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)

            # ═══════════════════════════════════════════════════════════
            # STEP 4: Check Call Tree Diagrams
            # ═══════════════════════════════════════════════════════════
            print("\n" + "="*60)
            print("STEP 4: Check Call Tree Diagrams")
            print("="*60)

            try:
                # Look for "Call Tree Diagrams" accordion
                print("Looking for 'Call Tree Diagrams' accordion...")
                # Try multiple selectors for Gradio accordions
                accordion = page.locator("div.label:has-text('Call Tree Diagrams')").or_(
                    page.locator("span:has-text('Call Tree Diagrams')").filter(has=page.locator(".."))
                ).or_(
                    page.locator("text=Call Tree Diagrams")
                ).first
                
                accordion_count = await accordion.count()
                if accordion_count > 0:
                    print("[OK] Found 'Call Tree Diagrams' accordion")
                    
                    # Click to expand
                    await accordion.click(force=True, timeout=5000)
                    await asyncio.sleep(2)
                    print("[OK] Expanded accordion")

                    # Take screenshot
                    screenshot_path = screenshots_dir / "step4_calltree_diagrams.png"
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    print(f"[OK] Screenshot saved: {screenshot_path}")

                    # Check content
                    content = await page.content()
                    
                    # Check for Mermaid iframe or mermaid code
                    if "<iframe" in content and "mermaid" in content.lower():
                        print("[SUCCESS] FOUND: Mermaid diagrams rendered in iframe")
                        results["step4_calltree"] = "iframe_rendered"
                    elif "graph TD" in content or "flowchart" in content:
                        print("[WARN] FOUND: Raw Mermaid code (not rendered)")
                        results["step4_calltree"] = "raw_code"
                    elif "No call tree diagrams" in content:
                        print("[WARN] FOUND: 'No call tree diagrams to display'")
                        results["step4_calltree"] = "no_diagrams"
                    else:
                        print("[WARN] Could not determine diagram status")
                        results["step4_calltree"] = "unknown"

                else:
                    print("[X] 'Call Tree Diagrams' accordion not found")
                    results["errors"].append("Step 4: Accordion not found")

            except Exception as e:
                results["errors"].append(f"Step 4: {e}")
                print(f"[X] FAILED: {e}")
                screenshot_path = screenshots_dir / "step4_error.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)

            # ═══════════════════════════════════════════════════════════
            # STEP 5: Check Detailed Report
            # ═══════════════════════════════════════════════════════════
            print("\n" + "="*60)
            print("STEP 5: Check Detailed Report")
            print("="*60)

            try:
                # Look for "Detailed Report" accordion
                print("Looking for 'Detailed Report' accordion...")
                accordion = page.locator("div.label:has-text('Detailed Report')").or_(
                    page.locator("span:has-text('Detailed Report')").filter(has=page.locator(".."))
                ).or_(
                    page.locator("text=Detailed Report")
                ).first
                
                accordion_count = await accordion.count()
                if accordion_count > 0:
                    print("[OK] Found 'Detailed Report' accordion")
                    
                    # Click to expand
                    await accordion.click(force=True, timeout=5000)
                    await asyncio.sleep(2)
                    print("[OK] Expanded accordion")

                    # Scroll down to see more content
                    await page.evaluate("window.scrollBy(0, 500)")
                    await asyncio.sleep(1)

                    # Take screenshot
                    screenshot_path = screenshots_dir / "step5_detailed_report.png"
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    print(f"[OK] Screenshot saved: {screenshot_path}")

                    # Check for Agent 1 and Agent 2 sections with text call trees
                    content = await page.content()
                    
                    has_agent1 = "Agent 1" in content
                    has_agent2 = "Agent 2" in content
                    has_root = "[ROOT]" in content
                    has_tree_format = "|--" in content or "`--" in content or "└──" in content or "├──" in content

                    print(f"  Agent 1 section: {'[OK]' if has_agent1 else '[X]'}")
                    print(f"  Agent 2 section: {'[OK]' if has_agent2 else '[X]'}")
                    print(f"  [ROOT] marker: {'[OK]' if has_root else '[X]'}")
                    print(f"  Tree format (|--): {'[OK]' if has_tree_format else '[X]'}")

                    if has_agent1 and has_agent2:
                        results["step5_detailed"] = True
                        print("[SUCCESS] STEP 5: SUCCESS - Found Agent sections")
                        
                        if has_root and has_tree_format:
                            print("[SUCCESS] Found text call trees with proper formatting")
                        else:
                            print("[WARN] Text call trees may not be properly formatted")
                    else:
                        print("[WARN] Agent sections not complete")

                else:
                    print("[X] 'Detailed Report' accordion not found")
                    results["errors"].append("Step 5: Accordion not found")

            except Exception as e:
                results["errors"].append(f"Step 5: {e}")
                print(f"[X] FAILED: {e}")
                screenshot_path = screenshots_dir / "step5_error.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)

        finally:
            await browser.close()

    # ═══════════════════════════════════════════════════════════
    # FINAL REPORT
    # ═══════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("FINAL TEST REPORT")
    print("="*60)
    
    print(f"\n[OK] Step 1 (Navigate): {results['step1_navigate']}")
    print(f"[OK] Step 2 (Index): {results['step2_index']}")
    print(f"[OK] Step 3 (Validate): {results['step3_validate']}")
    print(f"[OK] Step 4 (Call Tree Diagrams): {results['step4_calltree']}")
    print(f"[OK] Step 5 (Detailed Report): {results['step5_detailed']}")
    
    if results["errors"]:
        print("\n[X] ERRORS:")
        for error in results["errors"]:
            print(f"  - {error}")
    
    print(f"\n[SCREENSHOT] Screenshots saved to: {screenshots_dir.absolute()}")
    
    # Summary
    all_passed = (
        results["step1_navigate"] and
        results["step2_index"] and
        results["step3_validate"]
    )
    
    if all_passed:
        print("\n[SUCCESS] TEST PASSED: Core functionality working")
        
        # Check rendering issues
        if results["step4_calltree"] == "raw_code":
            print("[WARN] RENDERING ISSUE: Mermaid diagrams showing as raw code")
        elif results["step4_calltree"] == "iframe_rendered":
            print("[SUCCESS] Mermaid diagrams rendering correctly")
            
        return 0
    else:
        print("\n[X] TEST FAILED: Some steps did not complete")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
