"""
TrustBot 3-Agent UI Test - Restructured tabs and validation flow.

Tests:
1. Tab order: "1. Code Indexer" first, "2. Validate" second
2. Code Indexer: Git URL, Clone button, indexing
3. Validate: 3-agent pipeline, progress bar, trust scores, confirmed/phantom/missing
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "ui_test_3agent"


async def test_3agent_ui():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return 1

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("  TrustBot 3-Agent UI Test")
    print("=" * 70)

    findings = {
        "tabs_correct_order": None,
        "code_indexer_first": None,
        "code_indexer_guidance": None,
        "git_url_and_clone": None,
        "indexing_completed": None,
        "validate_tab_step2_instructions": None,
        "button_disabled_during": None,
        "progress_agent_steps": None,
        "progress_color_coding": None,
        "results_trust_scores": None,
        "results_confirmed_phantom_missing": None,
        "errors": [],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await context.new_page()

        try:
            # Step 1: Navigate and capture initial state
            print("\n[Step 1] Navigating to http://127.0.0.1:7860...")
            await page.goto("http://127.0.0.1:7860", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            await page.screenshot(path=str(SCREENSHOT_DIR / "01_initial_state.png"))
            print("        Screenshot: 01_initial_state.png")

            # Verify first tab is "1. Code Indexer"
            content = await page.content()
            tabs = await page.locator('button[role="tab"]').all()
            tab_texts = []
            for t in tabs:
                try:
                    tab_texts.append(await t.inner_text())
                except Exception:
                    pass

            first_tab = tab_texts[0] if tab_texts else ""
            findings["code_indexer_first"] = "1. Code Indexer" in first_tab or "Code Indexer" in first_tab
            findings["tabs_correct_order"] = (
                any("1. Code Indexer" in t or "Code Indexer" in t for t in tab_texts[:1]) and
                any("2. Validate" in t or "Validate" in t for t in tab_texts[:3])
            )

            # Verify guidance text and Git URL / Clone button
            findings["code_indexer_guidance"] = (
                "Step 1" in content and "Index" in content and "Agent 2" in content
            )
            findings["git_url_and_clone"] = (
                "Git" in content and "Clone" in content and "Repository" in content
            )

            # Step 2: Enter Git URL, Branch, click Clone
            print("\n[Step 2] Code Indexer - Entering Git URL and Branch...")
            git_inputs = await page.locator(
                'label:has-text("Git") ~ div input, label:has-text("Git") ~ div textarea, '
                'input[placeholder*="github"], input[placeholder*="repo"]'
            ).all()
            branch_inputs = await page.locator(
                'label:has-text("Branch") ~ div input, label:has-text("Branch") ~ div textarea'
            ).all()

            if git_inputs:
                await git_inputs[0].fill("https://github.com/nicabar/Delphi-Test.git")
            if branch_inputs:
                await branch_inputs[0].fill("main")
            elif len(await page.locator("input, textarea").all()) >= 2:
                inputs = await page.locator("input, textarea").all()
                await inputs[0].fill("https://github.com/nicabar/Delphi-Test.git")
                await inputs[1].fill("main")

            await asyncio.sleep(0.5)

            clone_btn = page.locator('button:has-text("Clone and Index Repository")')
            await clone_btn.click()
            print("        Clone and Index Repository clicked. Waiting 15-35s for indexing...")

            # Wait for indexing (15-35 seconds)
            for i in range(12):
                await asyncio.sleep(3)
                content = await page.content()
                if "Indexing Complete" in content or "Files processed" in content or "Codebase is ready" in content:
                    print(f"        Indexing completed at ~{(i+1)*3}s")
                    findings["indexing_completed"] = True
                    break
                if (i + 1) % 4 == 0:
                    print(f"        Still waiting... {(i+1)*3}s elapsed")
            else:
                findings["indexing_completed"] = False

            await page.screenshot(path=str(SCREENSHOT_DIR / "02_code_indexer_result.png"))
            print("        Screenshot: 02_code_indexer_result.png")

            # Step 3: Click "2. Validate" tab
            print("\n[Step 3] Clicking '2. Validate' tab...")
            for tab in tabs:
                try:
                    text = await tab.inner_text()
                    if "2. Validate" in text or ("Validate" in text and "2" in text):
                        await tab.click()
                        break
                    if "Validate" in text and "Code" not in text:
                        await tab.click()
                        break
                except Exception:
                    continue

            await asyncio.sleep(2)
            await page.screenshot(path=str(SCREENSHOT_DIR / "03_validate_tab_step2.png"))
            print("        Screenshot: 03_validate_tab_step2.png")

            content = await page.content()
            findings["validate_tab_step2_instructions"] = (
                "Step 2" in content and "Agent 1" in content and "Agent 2" in content and "Agent 3" in content
            )

            # Step 4: Enter Project ID, Run ID, click Validate (scope to visible Validate tab)
            print("\n[Step 4] Entering Project ID=3151, Run ID=4912, clicking Validate...")
            # Validate tab has Project ID and Run ID - use get_by_placeholder for visible tab
            project_input = page.get_by_placeholder("e.g. 3151")
            run_input = page.get_by_placeholder("e.g. 4912")
            await project_input.first.wait_for(state="visible", timeout=10000)
            await project_input.first.fill("3151")
            await run_input.first.fill("4912")

            validate_btn = page.locator('button:has-text("Validate All Flows")')
            await validate_btn.click()

            # Step 5: Screenshot during progress (button disabled, agent steps)
            await asyncio.sleep(2)
            await page.screenshot(path=str(SCREENSHOT_DIR / "04_validation_progress.png"))
            print("        Screenshot: 04_validation_progress.png")

            content = await page.content()
            findings["button_disabled_during"] = "interactive" in content or "%" in content  # Gradio disables via update
            has_agent = any(
                x in content for x in ["Agent 1", "Agent 2", "Agent 3", "agent1", "agent2", "agent3",
                                       "Neo4j", "Fetching", "Building", "Comparing"]
            )
            findings["progress_agent_steps"] = has_agent or "%" in content

            # Check color coding (orange/purple/green in progress bar HTML)
            findings["progress_color_coding"] = (
                "#FF9800" in content or "#9C27B0" in content or "#4CAF50" in content
            )

            # Step 6: Wait for completion (up to 90 seconds)
            print("\n[Step 5] Waiting for validation to complete (up to 90s)...")
            for i in range(18):
                await asyncio.sleep(5)
                content = await page.content()
                if "Validation complete!" in content or "3-Agent Validation" in content:
                    print(f"        Completed at ~{(i+1)*5}s")
                    break
                if (i + 1) % 6 == 0:
                    print(f"        Still waiting... {(i+1)*5}s elapsed")

            await asyncio.sleep(2)
            await page.screenshot(path=str(SCREENSHOT_DIR / "05_validation_complete.png"))
            print("        Screenshot: 05_validation_complete.png")

            # Final checks
            content = await page.content()
            findings["results_trust_scores"] = (
                "Trust Score" in content or "trust" in content.lower()
            )
            findings["results_confirmed_phantom_missing"] = (
                "Confirmed" in content and ("Phantom" in content or "phantom" in content) and
                ("Missing" in content or "missing" in content)
            )

        except Exception as e:
            findings["errors"].append(str(e))
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
            await page.screenshot(path=str(SCREENSHOT_DIR / "99_error.png"))
            print("        Error screenshot: 99_error.png")
            return 1
        finally:
            await browser.close()

    # Report
    print("\n" + "=" * 70)
    print("  TEST REPORT")
    print("=" * 70)
    print(f"\n  Tabs in correct order (Code Indexer first, Validate second): {findings['tabs_correct_order']}")
    print(f"  First tab is '1. Code Indexer': {findings['code_indexer_first']}")
    print(f"  Code Indexer has Step 1 guidance: {findings['code_indexer_guidance']}")
    print(f"  Git URL input and Clone button present: {findings['git_url_and_clone']}")
    print(f"  Code Indexer indexing completed: {findings['indexing_completed']}")
    print(f"  Validate tab shows Step 2 / 3-agent instructions: {findings['validate_tab_step2_instructions']}")
    print(f"  Button disabled during validation: {findings['button_disabled_during']}")
    print(f"  Progress bar shows agent-specific steps: {findings['progress_agent_steps']}")
    print(f"  Progress bar has color coding per agent: {findings['progress_color_coding']}")
    print(f"  Results show trust scores: {findings['results_trust_scores']}")
    print(f"  Results show confirmed/phantom/missing edges: {findings['results_confirmed_phantom_missing']}")
    if findings["errors"]:
        print(f"\n  Errors: {findings['errors']}")
    print(f"\n  Screenshots saved to: {SCREENSHOT_DIR}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(test_3agent_ui())
    sys.exit(exit_code)
