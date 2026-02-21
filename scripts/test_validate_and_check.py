"""Single-session E2E: validate then expand accordions and screenshot."""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

# Safe stdout encoding for Windows (avoids spawn issues with reconfigure)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SCREENSHOTS = Path("data/test_screenshots")
SCREENSHOTS.mkdir(parents=True, exist_ok=True)


def _chromium_args() -> list[str]:
    """Args to reduce spawn failures on Windows."""
    return ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed. Run: pip install playwright && playwright install chromium")
        return 1

    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="Run with visible browser")
    parser.add_argument("--port", type=int, default=7860, help="TrustBot port")
    args = parser.parse_args()

    async with async_playwright() as p:
        # Headless mode is more reliable on Windows (avoids "spawn: Aborted")
        browser = await p.chromium.launch(
            headless=not args.headed,
            args=_chromium_args(),
        )
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        # Navigate
        url = f"http://127.0.0.1:{args.port}"
        print(f"[1] Navigate to {url}...")
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        # Go to Validate tab (Gradio uses "2. Validate" as tab label)
        print("[2] Validate tab...")
        validate_tab = page.locator("button[role='tab']").filter(has_text="Validate")
        await validate_tab.click()
        await asyncio.sleep(3)  # Wait for tab content to render

        # Fill inputs - Gradio Textbox can be input or textarea; use placeholder
        pid = page.locator('input[placeholder*="3151"], textarea[placeholder*="3151"]').first
        rid = page.locator('input[placeholder*="4912"], textarea[placeholder*="4912"]').first
        await pid.wait_for(state="visible", timeout=15000)
        await pid.fill("3151", timeout=10000)
        await rid.fill("4912", timeout=10000)

        # Validate
        print("[3] Running validation...")
        btn = page.locator("button").filter(has_text="Validate All Flows")
        await btn.click()

        for i in range(60):
            await asyncio.sleep(2)
            content = await page.content()
            if "3-Agent Validation" in content and "Flow" in content:
                print(f"    Done at ~{(i+1)*2}s")
                break
            if (i + 1) % 5 == 0:
                print(f"    Waiting... {(i+1)*2}s")
        await asyncio.sleep(2)
        await page.screenshot(path=str(SCREENSHOTS / "v1_summary.png"))

        # Expand Call Tree Diagrams
        print("[4] Expanding Call Tree Diagrams...")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        accordions = await page.locator("button.open-button, button.label-wrap, span.label-text").all()
        for acc in accordions:
            txt = await acc.inner_text()
            if "Call Tree" in txt:
                await acc.click()
                print(f"    Clicked: {txt}")
                break
        await asyncio.sleep(3)
        await page.screenshot(path=str(SCREENSHOTS / "v2_calltree_expanded.png"), full_page=False)

        # Check iframe
        iframes = await page.locator("iframe").all()
        print(f"    Iframes: {len(iframes)}")
        if iframes:
            frame = iframes[0]
            src = await frame.get_attribute("srcdoc") or ""
            print(f"    srcdoc length: {len(src)}")
            print(f"    Has 'graph TD': {'graph TD' in src}")
            print(f"    Has 'mermaid': {'mermaid' in src}")

        # Expand Detailed Report
        print("[5] Expanding Detailed Report...")
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        accordions = await page.locator("button.open-button, button.label-wrap, span.label-text").all()
        for acc in accordions:
            txt = await acc.inner_text()
            if "Detailed Report" in txt:
                await acc.click()
                print(f"    Clicked: {txt}")
                break
        await asyncio.sleep(2)

        # Scroll down inside the page to see report content
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1)
        await page.screenshot(path=str(SCREENSHOTS / "v3_report_top.png"))

        # Keep scrolling to see call trees
        for i in range(4):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(0.5)
            await page.screenshot(path=str(SCREENSHOTS / f"v4_report_scroll_{i+1}.png"))

        # Check content
        content = await page.content()
        print(f"    Has [ROOT]: {'[ROOT]' in content}")
        print(f"    Has '|-- ': {'|-- ' in content or '|--' in content}")
        print(f"    Has 'Call Tree': {'Call Tree' in content}")
        print(f"    Has 'Agent 1': {'Agent 1' in content}")
        print(f"    Has tables: {'<table' in content.lower()}")

        print("\n=== DONE ===")
        await asyncio.sleep(2 if args.headed else 0)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
