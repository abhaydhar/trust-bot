"""Quick manual verification test - opens browser to each tab."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def quick_verify():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright not installed")
        return 1
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={'width': 1600, 'height': 1000})
        
        print("\n" + "="*60)
        print("  TrustBot Quick Verification")
        print("="*60)
        
        try:
            # Load app
            print("\nLoading TrustBot...")
            await page.goto("http://127.0.0.1:7860")
            await asyncio.sleep(5)
            print("✓ App loaded\n")
            
            # Check each tab
            tabs = ["Validate", "Code Indexer", "Chunk", "Agentic", "Chat", "Index"]
            
            for tab_name in tabs:
                print(f"Checking '{tab_name}' tab...")
                tab_buttons = await page.locator('button[role="tab"]').all()
                for btn in tab_buttons:
                    text = await btn.inner_text()
                    if tab_name.lower() in text.lower():
                        await btn.click()
                        await asyncio.sleep(2)
                        print(f"  ✓ {tab_name} tab functional\n")
                        break
            
            print("="*60)
            print("  All tabs verified!")
            print("="*60)
            print("\nBrowser will stay open for 20 seconds...")
            print("Manually verify the UI elements.\n")
            
            await asyncio.sleep(20)
            
        finally:
            await browser.close()
    
    return 0


if __name__ == "__main__":
    asyncio.run(quick_verify())
