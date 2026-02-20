"""
Browser control tool for automated testing and UI validation.

Uses Playwright for headless browser automation.
"""

from __future__ import annotations

import asyncio
import logging
from trustbot.tools.base import BaseTool

logger = logging.getLogger("trustbot.tools.browser")


class BrowserTool(BaseTool):
    """
    Tool for browser automation — navigate, click, fill forms, take screenshots.
    Used for end-to-end testing of the TrustBot web UI.
    """

    name = "browser"
    description = (
        "Control a browser for automated testing. "
        "Navigate to URLs, click elements, fill forms, take screenshots."
    )

    def __init__(self) -> None:
        super().__init__()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def initialize(self) -> None:
        """Launch Playwright browser."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "Playwright is required for browser control. "
                "Install with: pip install playwright && playwright install chromium"
            )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()
        logger.info("Browser tool initialized (Chromium headless)")

    async def shutdown(self) -> None:
        """Close browser and Playwright."""
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("Browser tool shut down")

    @property
    def page(self):
        """Get the current page. Raises if not initialized."""
        if self._page is None:
            raise RuntimeError("Browser not initialized. Call initialize() first.")
        return self._page

    async def navigate(self, url: str, wait_until: str = "networkidle") -> dict:
        """
        Navigate to a URL.
        wait_until: 'load' | 'domcontentloaded' | 'networkidle' | 'commit'
        """
        response = await self.page.goto(url, wait_until=wait_until, timeout=30000)
        return {
            "url": self.page.url,
            "status": response.status if response else None,
            "title": await self.page.title(),
        }

    async def click(self, selector: str) -> dict:
        """Click an element by CSS selector."""
        await self.page.click(selector, timeout=5000)
        return {"action": "clicked", "selector": selector}

    async def fill(self, selector: str, value: str) -> dict:
        """Fill an input by CSS selector."""
        await self.page.fill(selector, value, timeout=5000)
        return {"action": "filled", "selector": selector}

    async def get_text(self, selector: str) -> dict:
        """Get text content of an element."""
        elem = await self.page.query_selector(selector)
        text = await elem.text_content() if elem else None
        return {"selector": selector, "text": text}

    async def screenshot(self, path: str | None = None) -> dict:
        """Take a screenshot. Returns path if path provided."""
        if path:
            await self.page.screenshot(path=path)
            return {"screenshot": path}
        return {"screenshot": "taken (no path)"}

    async def wait_for_selector(self, selector: str, timeout: int = 10000) -> dict:
        """Wait for an element to appear."""
        await self.page.wait_for_selector(selector, timeout=timeout)
        return {"found": selector}
