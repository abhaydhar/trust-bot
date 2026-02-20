"""
Browser E2E tests for TrustBot using Playwright.

Run with: pytest tests/test_browser.py -v
Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest


@pytest.fixture(scope="module")
def app_server():
    """Start TrustBot in background, yield, then stop."""
    proc = subprocess.Popen(
        ["python", "-m", "trustbot.main"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=".",
    )
    time.sleep(5)  # Wait for Gradio to start
    yield proc
    proc.terminate()
    proc.wait(timeout=10)


@pytest.mark.skip(reason="Requires running TrustBot server on port 7860")
@pytest.mark.asyncio
async def test_browser_navigate_and_ui():
    """Test that browser can navigate to TrustBot and find key UI elements."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright not installed")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            # Start app in background
            proc = subprocess.Popen(
                ["python", "-m", "trustbot.main"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(6)

            # Navigate to TrustBot
            response = await page.goto("http://127.0.0.1:7860", timeout=15000)
            assert response is not None
            assert response.status == 200

            # Check page title or heading
            content = await page.content()
            assert "TrustBot" in content or "trustbot" in content.lower()

            # Look for Validate tab / inputs
            await page.wait_for_selector("input, button, textarea", timeout=5000)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            await browser.close()
