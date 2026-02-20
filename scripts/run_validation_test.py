"""
Automated validation test - Final version with force click and proper button targeting.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def test_validation():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[ERROR] Playwright not installed")
        return 1
    
    print("=" * 70)
    print("  TrustBot Validation Test - Project 3151, Run 4912")
    print("=" * 70)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=1000)  # Slow down for visibility
        context = await browser.new_context(viewport={'width': 1600, 'height': 1000})
        page = await context.new_page()
        
        try:
            print("\n[Step 1] Loading TrustBot at http://127.0.0.1:7860...")
            await page.goto("http://127.0.0.1:7860", wait_until="networkidle", timeout=20000)
            print("           Page loaded successfully\n")
            
            await asyncio.sleep(4)
            
            print("[Step 2] Filling Project ID = 3151...")
            project_inputs = await page.locator('label:has-text("Project ID") ~ div input, input[placeholder*="3151"]').all()
            if not project_inputs:
                # Try finding any input in the first row
                project_inputs = await page.locator('input[type="text"], textarea').all()
            
            if len(project_inputs) >= 1:
                await project_inputs[0].fill("3151", force=True)
                print("           Project ID entered\n")
            else:
                print("           ERROR: No input fields found\n")
                return 1
            
            print("[Step 3] Filling Run ID = 4912...")
            if len(project_inputs) >= 2:
                await project_inputs[1].fill("4912", force=True)
                print("           Run ID entered\n")
            else:
                print("           ERROR: Run ID input not found\n")
                return 1
            
            await asyncio.sleep(2)
            
            print("[Step 4] Looking for 'Validate All Flows' button...")
            # Look for the actual validation button (not tab button)
            all_buttons = await page.locator('button').all()
            validate_btn = None
            
            for idx, btn in enumerate(all_buttons):
                try:
                    text = await btn.inner_text(timeout=1000)
                    if text and "Validate All Flows" in text:
                        validate_btn = btn
                        print(f"           Found button #{idx}: '{text}'\n")
                        break
                except:
                    continue
            
            if not validate_btn:
                print("           WARNING: 'Validate All Flows' button not found")
                print("           Trying alternative: any button with 'Validate' that's not a tab\n")
                
                for idx, btn in enumerate(all_buttons):
                    try:
                        text = await btn.inner_text(timeout=1000)
                        role = await btn.get_attribute("role")
                        if text and "validate" in text.lower() and role != "tab":
                            validate_btn = btn
                            print(f"           Found button #{idx}: '{text}' (role={role})\n")
                            break
                    except:
                        continue
            
            if validate_btn:
                print("[Step 5] Clicking 'Validate All Flows' button...")
                try:
                    # Use JavaScript click to bypass overlays
                    await validate_btn.evaluate("el => el.click()")
                    print("           Button clicked via JavaScript!\n")
                except Exception as e:
                    print(f"           ERROR clicking button: {e}\n")
                    return 1
            else:
                print("           ERROR: Could not find validate button\n")
                return 1
            
            print("[Step 6] Waiting for validation to complete...")
            print("           (This may take 1-2 minutes depending on graph size)\n")
            
            # Wait and monitor
            for i in range(24):  # 24 x 5 seconds = 2 minutes
                await asyncio.sleep(5)
                if (i + 1) % 6 == 0:
                    print(f"           Still waiting... {(i+1)*5}s elapsed")
            
            print("\n[Step 7] Validation should be complete or in progress")
            print("           Browser will stay open for 20 seconds to review results...\n")
            
            await asyncio.sleep(20)
            
            print("=" * 70)
            print("  TEST COMPLETE")
            print("=" * 70)
            print("\n  The validation request was submitted successfully!")
            print("  Check the TrustBot UI (browser window) for results.\n")
            
        except Exception as e:
            print(f"\n[ERROR] Test failed: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(10)
            return 1
        finally:
            print("\nClosing browser...")
            await browser.close()
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(test_validation())
    sys.exit(exit_code)
