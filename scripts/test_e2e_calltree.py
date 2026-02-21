"""
E2E test: Index repo, validate, and check call tree rendering.
Uses Playwright with Gradio-specific input selectors.
"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCREENSHOTS = Path("data/test_screenshots")
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

BASE_URL = "http://127.0.0.1:7860"
GIT_URL = "https://github.com/AnshuSuroliya/Delphi-Test.git"
BRANCH = "master"
PROJECT_ID = "3151"
RUN_ID = "4912"


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        # Step 1: Navigate
        print("[1] Navigating to TrustBot...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)
        await page.screenshot(path=str(SCREENSHOTS / "01_home.png"))
        print("    Screenshot: 01_home.png")

        # Step 2: Code Indexer tab — fill and submit
        print("[2] Indexing repository...")
        # Click Code Indexer tab
        tabs = await page.locator("button[role='tab']").all()
        for tab in tabs:
            txt = (await tab.inner_text()).strip()
            if "Code Indexer" in txt or "1." in txt:
                await tab.click()
                break
        await asyncio.sleep(1)

        # Fill Git URL — Gradio textboxes are <textarea> or <input> inside label containers
        git_input = page.locator("textarea, input").filter(has_text="").nth(0)
        all_inputs = await page.locator("textarea, input[type='text']").all()
        print(f"    Found {len(all_inputs)} text inputs")

        # Try filling by placeholder
        git_field = page.get_by_placeholder("https://github.com/username/repo.git")
        branch_field = page.get_by_placeholder("main")

        try:
            await git_field.fill(GIT_URL, timeout=3000)
            await branch_field.fill(BRANCH, timeout=3000)
            print("    Filled Git URL and Branch via placeholder")
        except Exception as e:
            print(f"    Placeholder fill failed: {e}")
            # Fallback: fill by index
            for inp in all_inputs:
                ph = await inp.get_attribute("placeholder") or ""
                if "github" in ph.lower() or "repo" in ph.lower():
                    await inp.fill(GIT_URL)
                    print(f"    Filled Git URL via fallback")
                elif ph == "main":
                    await inp.fill(BRANCH)
                    print(f"    Filled Branch via fallback")

        await page.screenshot(path=str(SCREENSHOTS / "02_indexer_filled.png"))

        # Click Clone and Index button
        clone_btn = page.locator("button").filter(has_text="Clone and Index")
        await clone_btn.click()
        print("    Clone and Index clicked. Waiting...")

        # Wait for indexing (up to 90s)
        for i in range(30):
            await asyncio.sleep(3)
            content = await page.content()
            if "Indexing Complete" in content or "Codebase is ready" in content:
                print(f"    Indexing completed at ~{(i+1)*3}s")
                break
            if "Error" in content and "git" in content.lower():
                print(f"    Indexing error at ~{(i+1)*3}s")
                break
            if (i + 1) % 5 == 0:
                print(f"    Still indexing... {(i+1)*3}s")
        else:
            print("    Indexing timeout at 90s")

        await page.screenshot(path=str(SCREENSHOTS / "03_indexer_result.png"))
        print("    Screenshot: 03_indexer_result.png")

        # Step 3: Validate tab
        print("[3] Running validation...")
        tabs = await page.locator("button[role='tab']").all()
        for tab in tabs:
            txt = (await tab.inner_text()).strip()
            if "Validate" in txt or "2." in txt:
                await tab.click()
                break
        await asyncio.sleep(1)

        # Fill Project ID and Run ID
        all_inputs = await page.locator("textarea, input[type='text']").all()
        for inp in all_inputs:
            ph = await inp.get_attribute("placeholder") or ""
            if "3151" in ph:
                await inp.fill(PROJECT_ID)
                print("    Filled Project ID")
            elif "4912" in ph:
                await inp.fill(RUN_ID)
                print("    Filled Run ID")

        await page.screenshot(path=str(SCREENSHOTS / "04_validate_filled.png"))

        # Click Validate
        validate_btn = page.locator("button").filter(has_text="Validate All Flows")
        await validate_btn.click()
        print("    Validate clicked. Waiting...")

        # Wait for validation (up to 120s)
        for i in range(40):
            await asyncio.sleep(3)
            content = await page.content()
            if "Validation complete" in content or "trust" in content.lower():
                # Check if we have actual results (not just the button text)
                if "Agent Validation" in content or "Flow " in content:
                    print(f"    Validation completed at ~{(i+1)*3}s")
                    break
            if (i + 1) % 5 == 0:
                print(f"    Still validating... {(i+1)*3}s")
        else:
            print("    Validation timeout at 120s")

        await page.screenshot(path=str(SCREENSHOTS / "05_validation_result.png"))
        print("    Screenshot: 05_validation_result.png")

        # Step 4: Check Call Tree Diagrams
        print("[4] Checking Call Tree Diagrams...")
        calltree_accordion = page.locator("button, span, div").filter(has_text="Call Tree Diagrams")
        try:
            await calltree_accordion.first.click(timeout=3000)
            await asyncio.sleep(2)
        except Exception:
            print("    Could not find Call Tree Diagrams accordion")

        await page.screenshot(path=str(SCREENSHOTS / "06_calltree.png"))

        # Check for iframe (Mermaid rendering)
        iframes = await page.locator("iframe").all()
        print(f"    Iframes found: {len(iframes)}")

        # Check for mermaid SVG content
        content = await page.content()
        has_mermaid_svg = "mermaid" in content.lower() and ("<svg" in content.lower() or "graph TD" in content)
        has_calltree_text = "[ROOT]" in content or "|-- " in content or "`-- " in content
        print(f"    Mermaid content present: {has_mermaid_svg}")
        print(f"    Text call tree present: {has_calltree_text}")

        # Step 5: Check Detailed Report
        print("[5] Checking Detailed Report...")
        report_accordion = page.locator("button, span, div").filter(has_text="Detailed Report")
        try:
            await report_accordion.first.click(timeout=3000)
            await asyncio.sleep(2)
        except Exception:
            print("    Could not find Detailed Report accordion")

        await page.screenshot(path=str(SCREENSHOTS / "07_detailed_report.png"), full_page=True)

        # Check report content
        content = await page.content()
        has_agent1 = "Agent 1" in content
        has_agent2 = "Agent 2" in content
        has_calltree = "Call Tree" in content
        has_flow = "Flow 1" in content
        print(f"    Agent 1 section: {has_agent1}")
        print(f"    Agent 2 section: {has_agent2}")
        print(f"    Call Tree in report: {has_calltree}")
        print(f"    Flow sections: {has_flow}")

        # Take one more screenshot scrolled down
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(1)
        await page.screenshot(path=str(SCREENSHOTS / "08_report_scrolled.png"))

        print("\n=== TEST COMPLETE ===")
        print(f"Screenshots saved to: {SCREENSHOTS.resolve()}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
