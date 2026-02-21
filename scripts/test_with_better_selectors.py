#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test with better selectors and more debugging.
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
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return 1

    screenshots_dir = Path("data/test_screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context(viewport={"width": 1920, "height": 1200})
        page = await context.new_page()

        try:
            print("Navigating to http://127.0.0.1:7860...")
            await page.goto("http://127.0.0.1:7860", timeout=30000)
            await asyncio.sleep(5)
            
            # Go to Validate tab
            print("Clicking on Validate tab...")
            validate_tab = page.locator("button[role='tab']").filter(has_text="2. Validate").first
            if await validate_tab.count() > 0:
                await validate_tab.click(force=True)
                await asyncio.sleep(3)
                print("[OK] On Validate tab")
            
            content = await page.content()
            print(f"\nPage content length: {len(content)} chars")
            print(f"Contains '84%': {('84%' in content)}")
            print(f"Contains 'trust': {('trust' in content.lower())}")
            print(f"Contains 'Call Tree Diagrams': {('Call Tree Diagrams' in content)}")
            print(f"Contains 'Detailed Report': {('Detailed Report' in content)}")
            
            # Take initial screenshot
            screenshot = screenshots_dir / "initial_page.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            print(f"\n[SCREENSHOT] {screenshot.name}")
            
            # Scroll to see accordions
            print("\nScrolling down...")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            
            screenshot = screenshots_dir / "scrolled_bottom.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            print(f"[SCREENSHOT] {screenshot.name}")
            
            # Try to find accordions using different selectors
            print("\nSearching for accordion elements...")
            
            # Method 1: Look for any element containing the text
            elements = await page.query_selector_all("*")
            accordion_texts = ["Call Tree Diagrams", "Detailed Report", "Agent 1 Output", "Agent 2 Output"]
            
            for accordion_text in accordion_texts:
                print(f"\n  Searching for: {accordion_text}")
                found = False
                for elem in elements[:100]:  # Check first 100 elements
                    try:
                        text = await elem.inner_text()
                        if accordion_text in text and len(text) < 100:
                            found = True
                            tag = await elem.evaluate("el => el.tagName")
                            classes = await elem.get_attribute("class")
                            print(f"    Found in <{tag}> with class='{classes}'")
                            break
                    except:
                        continue
                
                if not found:
                    print(f"    NOT FOUND in first 100 elements")
            
            # Method 2: Try Gradio accordion selectors
            print("\nTrying Gradio-specific selectors...")
            gradio_selectors = [
                ".label",
                "[class*='accordion']",
                "[class*='label']",
                "span.label",
                "div.label",
            ]
            
            for selector in gradio_selectors:
                elements = await page.query_selector_all(selector)
                if len(elements) > 0:
                    print(f"  {selector}: found {len(elements)} elements")
                    for elem in elements[:5]:
                        try:
                            text = await elem.inner_text()
                            if text and len(text) < 100:
                                print(f"    - {text[:50]}")
                        except:
                            pass
            
            # Method 3: Just click anything that looks like an accordion
            print("\nAttempting to click expandable sections...")
            
            # Find all clickable elements
            clickables = await page.query_selector_all("span, div, button")
            for clickable in clickables:
                try:
                    text = await clickable.inner_text()
                    if "Call Tree Diagrams" in text and len(text) < 50:
                        print(f"\nFound 'Call Tree Diagrams' element, attempting click...")
                        await clickable.scroll_into_view_if_needed()
                        await asyncio.sleep(1)
                        await clickable.click(force=True)
                        await asyncio.sleep(3)
                        
                        screenshot = screenshots_dir / "accordion_calltree.png"
                        await page.screenshot(path=str(screenshot), full_page=True)
                        print(f"[SCREENSHOT] {screenshot.name}")
                        
                        # Check what's visible
                        content = await page.content()
                        iframes = await page.query_selector_all("iframe")
                        print(f"\n[ANALYSIS] Call Tree Diagrams:")
                        print(f"  Iframes: {len(iframes)}")
                        print(f"  Has 'mermaid': {('mermaid' in content.lower())}")
                        print(f"  Has 'graph TD': {('graph TD' in content)}")
                        print(f"  Has 'No call tree': {('No call tree' in content)}")
                        
                        break
                except Exception as e:
                    continue
            
            for clickable in clickables:
                try:
                    text = await clickable.inner_text()
                    if "Detailed Report" in text and len(text) < 50:
                        print(f"\nFound 'Detailed Report' element, attempting click...")
                        await clickable.scroll_into_view_if_needed()
                        await asyncio.sleep(1)
                        await clickable.click(force=True)
                        await asyncio.sleep(3)
                        
                        screenshot = screenshots_dir / "accordion_report_1.png"
                        await page.screenshot(path=str(screenshot), full_page=True)
                        print(f"[SCREENSHOT] {screenshot.name}")
                        
                        # Check what's visible
                        content = await page.content()
                        code_blocks = await page.query_selector_all("pre, code")
                        tables = await page.query_selector_all("table")
                        
                        print(f"\n[ANALYSIS] Detailed Report:")
                        print(f"  Code blocks: {len(code_blocks)}")
                        print(f"  Tables: {len(tables)}")
                        print(f"  Has '[ROOT]': {('[ROOT]' in content)}")
                        print(f"  Has '|--': {('|--' in content)}")
                        print(f"  Has 'Agent 1': {('Agent 1' in content)}")
                        print(f"  Has 'Agent 2': {('Agent 2' in content)}")
                        
                        # Scroll and take more screenshots
                        for i in range(2, 5):
                            await page.evaluate("window.scrollBy(0, 400)")
                            await asyncio.sleep(1)
                            screenshot = screenshots_dir / f"accordion_report_{i}.png"
                            await page.screenshot(path=str(screenshot), full_page=True)
                            print(f"[SCREENSHOT] {screenshot.name}")
                        
                        break
                except Exception as e:
                    continue
            
            print("\nPausing for 5 seconds...")
            await asyncio.sleep(5)

        finally:
            await browser.close()

    print("\n" + "="*70)
    print(f"Screenshots saved to: {screenshots_dir.absolute()}")
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
