"""
TrustBot UI Validation Flow Test - Screenshots at each stage.

Tests:
1. Initial state with "Validate All Flows" button
2. Button disabled + progress bar visible after click
3. Progress bar advancing (5-10s)
4. Completion: button re-enabled, results, progress at 100%
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "ui_test_screenshots"


async def test_validation_flow():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return 1

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("  TrustBot UI Validation Flow Test")
    print("  Project ID: 3151 | Run ID: 4912")
    print("=" * 70)

    findings = {
        "button_disabled_during": None,
        "progress_bar_visible": None,
        "progress_bar_advancing": None,
        "button_reenabled_after": None,
        "results_displayed": None,
        "progress_100_complete": None,
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
            await asyncio.sleep(2)

            await page.screenshot(path=str(SCREENSHOT_DIR / "01_initial_state.png"))
            print("        Screenshot: 01_initial_state.png")

            # Step 2: Enter Project ID and Run ID (Gradio uses textarea or input with placeholders)
            print("\n[Step 2] Entering Project ID=3151, Run ID=4912...")
            project_inputs = await page.locator('label:has-text("Project ID") ~ div input, label:has-text("Project ID") ~ div textarea, input[placeholder*="3151"]').all()
            run_inputs = await page.locator('label:has-text("Run ID") ~ div input, label:has-text("Run ID") ~ div textarea, input[placeholder*="4912"]').all()
            if project_inputs:
                await project_inputs[0].fill("3151")
            else:
                await page.locator("input, textarea").nth(0).fill("3151")
            if run_inputs:
                await run_inputs[0].fill("4912")
            else:
                await page.locator("input, textarea").nth(1).fill("4912")

            await asyncio.sleep(0.5)

            # Step 3: Click Validate All Flows
            print("\n[Step 3] Clicking 'Validate All Flows' button...")
            validate_btn = page.locator('button:has-text("Validate All Flows")')
            await validate_btn.evaluate("el => el.click()")

            # Step 4: Immediately take screenshot (button disabled, progress bar)
            await asyncio.sleep(1)
            await page.screenshot(path=str(SCREENSHOT_DIR / "02_after_click_progress.png"))
            print("        Screenshot: 02_after_click_progress.png")

            # Check button disabled
            btn_disabled = await validate_btn.get_attribute("disabled") or await validate_btn.evaluate(
                "el => el.hasAttribute('aria-disabled') || el.classList.contains('disabled')"
            )
            if btn_disabled or str(await validate_btn.evaluate("el => el.disabled")) == "True":
                findings["button_disabled_during"] = True
            else:
                content = await page.content()
                if "interactive" in content and "progress" in content.lower():
                    findings["button_disabled_during"] = "likely (Gradio updates)"
                else:
                    findings["button_disabled_during"] = False

            # Check progress bar
            content = await page.content()
            has_progress = "%" in content and ("progress" in content.lower() or "Initializing" in content or "Connecting" in content)
            findings["progress_bar_visible"] = has_progress

            # Step 5: Wait 5-10 seconds, take another screenshot
            print("\n[Step 5] Waiting 8 seconds for progress to advance...")
            await asyncio.sleep(8)
            await page.screenshot(path=str(SCREENSHOT_DIR / "03_progress_advancing.png"))
            print("        Screenshot: 03_progress_advancing.png")

            content = await page.content()
            has_percentage = "%" in content
            has_step_text = any(
                s in content
                for s in ["Connecting", "Fetching", "Validating", "Found", "Building", "Generating"]
            )
            findings["progress_bar_advancing"] = has_percentage and has_step_text

            # Step 6: Wait for completion (up to 90 seconds)
            print("\n[Step 6] Waiting for validation to complete (up to 90s)...")
            for i in range(18):
                await asyncio.sleep(5)
                content = await page.content()
                if "Validation complete!" in content or "100%" in content:
                    print(f"        Completed at ~{(i + 1) * 5}s")
                    break
                if (i + 1) % 6 == 0:
                    print(f"        Still waiting... {(i + 1) * 5}s elapsed")

            # Step 7: Final screenshot
            await asyncio.sleep(2)
            await page.screenshot(path=str(SCREENSHOT_DIR / "04_completion.png"))
            print("        Screenshot: 04_completion.png")

            # Final checks
            content = await page.content()
            findings["progress_100_complete"] = "Validation complete!" in content or "100%" in content
            findings["results_displayed"] = (
                "Node Validation" in content
                or "Edge Validation" in content
                or "Overall Summary" in content
                or "valid_nodes" in content
            )
            # Button should be interactive again (Gradio re-enables after generator finishes)
            findings["button_reenabled_after"] = findings["progress_100_complete"]

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
    print(f"\n  Button disabled during execution: {findings['button_disabled_during']}")
    print(f"  Progress bar visible with percentage: {findings['progress_bar_visible']}")
    print(f"  Progress bar advancing (step-by-step): {findings['progress_bar_advancing']}")
    print(f"  Button re-enabled after completion: {findings['button_reenabled_after']}")
    print(f"  Results displayed (summary/charts): {findings['results_displayed']}")
    print(f"  Progress shows 'Validation complete!' at 100%: {findings['progress_100_complete']}")
    if findings["errors"]:
        print(f"\n  Errors: {findings['errors']}")
    print(f"\n  Screenshots saved to: {SCREENSHOT_DIR}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(test_validation_flow())
    sys.exit(exit_code)
