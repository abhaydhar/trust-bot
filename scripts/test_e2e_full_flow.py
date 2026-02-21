"""
Full end-to-end 3-agent validation flow test.

1. Code Indexer: Clone https://github.com/AnshuSuroliya/Delphi-Test.git (branch: master)
2. Validate: Project 3151, Run 4912
3. Report: indexing stats, validation results, confirmed/phantom edges, trust scores
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "ui_test_e2e"


async def test_e2e_full_flow():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return 1

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("  TrustBot Full E2E 3-Agent Validation Flow")
    print("=" * 70)

    report = {
        "indexing_success": False,
        "functions_indexed": None,
        "edges_indexed": None,
        "validation_success": False,
        "confirmed_edges": None,
        "phantom_edges": None,
        "missing_edges": None,
        "trust_score": None,
        "errors": [],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await context.new_page()

        try:
            # Step 1: Navigate
            print("\n[Step 1] Navigating to http://127.0.0.1:7860...")
            await page.goto("http://127.0.0.1:7860", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Step 2: Code Indexer - Enter Git URL (AnshuSuroliya/Delphi-Test), Branch: master
            print("\n[Step 2] Code Indexer - Entering Git URL and Branch (master)...")
            git_input = page.get_by_placeholder("https://github.com/username/repo.git").first
            branch_input = page.get_by_placeholder("main").first
            await git_input.wait_for(state="visible", timeout=5000)
            await git_input.fill("https://github.com/AnshuSuroliya/Delphi-Test.git")
            await branch_input.fill("master")

            await asyncio.sleep(0.5)
            clone_btn = page.locator('button:has-text("Clone and Index Repository")')
            await clone_btn.click()
            print("        Clone and Index clicked. Waiting for indexing (10-40s)...")

            # Wait for indexing
            for i in range(16):
                await asyncio.sleep(3)
                content = await page.content()
                if "Indexing Complete" in content or "Indexing complete" in content:
                    report["indexing_success"] = True
                    # Parse functions and edges
                    m_func = re.search(r"Functions indexed[:\s]+(\d+)", content, re.I)
                    m_edges = re.search(r"Call graph edges[:\s]+(\d+)", content, re.I)
                    if m_func:
                        report["functions_indexed"] = int(m_func.group(1))
                    if m_edges:
                        report["edges_indexed"] = int(m_edges.group(1))
                    print(f"        Indexing completed at ~{(i+1)*3}s")
                    break
                if "Error:" in content and "Repository" not in content:
                    # Check for error (but "Repository not found" is different)
                    err_match = re.search(r"Error:\s*([^<]+)", content)
                    if err_match and "Repository not found" not in err_match.group(1):
                        report["errors"].append(err_match.group(1)[:200])
                if (i + 1) % 5 == 0:
                    print(f"        Still waiting... {(i+1)*3}s elapsed")
            else:
                content = await page.content()
                if "Error:" in content:
                    err_match = re.search(r"Error:\s*([^<]+)", content)
                    if err_match:
                        report["errors"].append(err_match.group(1)[:300])

            await page.screenshot(path=str(SCREENSHOT_DIR / "01_indexing_result.png"))
            print("        Screenshot: 01_indexing_result.png")

            # Step 3: Click Validate tab
            print("\n[Step 3] Clicking '2. Validate' tab...")
            validate_tab = page.locator('button[role="tab"]:has-text("2. Validate")')
            await validate_tab.click()
            await asyncio.sleep(2)

            # Step 4: Enter Project ID, Run ID, click Validate
            print("\n[Step 4] Entering Project ID=3151, Run ID=4912, clicking Validate...")
            project_input = page.get_by_placeholder("e.g. 3151")
            run_input = page.get_by_placeholder("e.g. 4912")
            await project_input.first.wait_for(state="visible", timeout=10000)
            await project_input.first.fill("3151")
            await run_input.first.fill("4912")

            validate_btn = page.locator('button:has-text("Validate All Flows")')
            await validate_btn.click()

            # Screenshot during progress (after ~5s)
            await asyncio.sleep(5)
            await page.screenshot(path=str(SCREENSHOT_DIR / "02_validation_progress.png"))
            print("        Screenshot: 02_validation_progress.png")

            # Wait for completion (up to 90s)
            print("\n[Step 5] Waiting for validation to complete (up to 90s)...")
            for i in range(18):
                await asyncio.sleep(5)
                content = await page.content()
                if "Validation complete!" in content or "3-Agent Validation" in content:
                    report["validation_success"] = True
                    # Parse results
                    m_conf = re.search(r"Confirmed[:\s]+(\d+)", content)
                    m_phantom = re.search(r"Phantom[^:]*[:\s]+(\d+)", content)
                    m_missing = re.search(r"Missing[^:]*[:\s]+(\d+)", content)
                    m_trust = re.search(r"Trust Score[:\s]+(\d+)%", content, re.I)
                    m_avg_trust = re.search(r"Average Trust Score[:\s]+(\d+)%", content, re.I)
                    if m_conf:
                        report["confirmed_edges"] = int(m_conf.group(1))
                    if m_phantom:
                        report["phantom_edges"] = int(m_phantom.group(1))
                    if m_missing:
                        report["missing_edges"] = int(m_missing.group(1))
                    if m_avg_trust:
                        report["trust_score"] = int(m_avg_trust.group(1))
                    elif m_trust:
                        report["trust_score"] = int(m_trust.group(1))
                    print(f"        Completed at ~{(i+1)*5}s")
                    break
                if (i + 1) % 6 == 0:
                    print(f"        Still waiting... {(i+1)*5}s elapsed")

            await asyncio.sleep(2)
            await page.screenshot(path=str(SCREENSHOT_DIR / "03_validation_complete.png"))
            print("        Screenshot: 03_validation_complete.png")

        except Exception as e:
            report["errors"].append(str(e))
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
            await page.screenshot(path=str(SCREENSHOT_DIR / "99_error.png"))
            return 1
        finally:
            await browser.close()

    # Report
    print("\n" + "=" * 70)
    print("  E2E FLOW REPORT")
    print("=" * 70)
    print(f"\n  Code Indexer: {'SUCCESS' if report['indexing_success'] else 'FAILED'}")
    print(f"  Functions indexed: {report['functions_indexed']}")
    print(f"  Call graph edges indexed: {report['edges_indexed']}")
    print(f"\n  Validation: {'SUCCESS' if report['validation_success'] else 'FAILED'}")
    print(f"  Confirmed edges: {report['confirmed_edges']}")
    print(f"  Phantom edges: {report['phantom_edges']}")
    print(f"  Missing edges: {report['missing_edges']}")
    print(f"  Trust score: {report['trust_score']}%")
    if report["errors"]:
        print(f"\n  Errors: {report['errors']}")
    print(f"\n  Screenshots: {SCREENSHOT_DIR}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(test_e2e_full_flow())
    sys.exit(exit_code)
