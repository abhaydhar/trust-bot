#!/usr/bin/env python
"""
Standalone browser test script for TrustBot.

Starts TrustBot, waits for it to be ready, then uses Playwright to:
1. Navigate to the UI
2. Take a screenshot
3. Verify key elements exist

Usage:
    python scripts/run_browser_test.py

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "trustbot.main"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parent.parent,
    )

    print("Waiting for TrustBot to start (6s)...")
    await asyncio.sleep(6)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed. Run: pip install playwright && playwright install chromium")
        proc.terminate()
        return 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            print("Navigating to http://127.0.0.1:7860 ...")
            response = await page.goto("http://127.0.0.1:7860", timeout=15000)
            if not response or response.status != 200:
                print(f"ERROR: Bad response: {response}")
                return 1

            print("Page loaded. Taking screenshot...")
            screenshot_path = Path("data") / "browser_test_screenshot.png"
            screenshot_path.parent.mkdir(exist_ok=True)
            await page.screenshot(path=str(screenshot_path))
            print(f"Screenshot saved to {screenshot_path}")

            content = await page.content()
            if "TrustBot" in content or "trustbot" in content.lower():
                print("SUCCESS: TrustBot UI verified")
            else:
                print("WARNING: TrustBot branding not found in page")

            await browser.close()
        except Exception as e:
            print(f"ERROR: {e}")
            return 1
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
