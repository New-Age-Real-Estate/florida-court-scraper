import asyncio
import os
from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(BASE_DIR, "debug_screenshots")
os.makedirs(DEBUG_DIR, exist_ok=True)

async def test_local_hillsborough():
    print("[*] Launching local Chromium browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        # Use realistic user agent & viewport
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        target_url = "https://publicaccess.hillsclerk.com/oripublicaccess/"
        print(f"[*] Navigating to {target_url} directly from local connection...")

        try:
            response = await page.goto(target_url, wait_until="networkidle", timeout=45000)
            status = response.status if response else "Unknown"
            title = await page.title()
            print(f"[+] HTTP Status: {status}")
            print(f"[+] Page Title: '{title}'")

            # Check for bot detection keywords
            content = await page.content()
            if "cf-browser-verification" in content or "Attention Required! | Cloudflare" in content:
                print("[-] Cloudflare challenge detected.")
            elif "Access Denied" in title or "403 Forbidden" in title:
                print("[-] 403 Forbidden / Access Denied detected.")
            else:
                print("[✓] SUCCESS! Page loaded cleanly without bot blocks.")

            # Save screenshot
            screenshot_path = os.path.join(DEBUG_DIR, "local_hillsborough_test.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            print(f"[+] Screenshot saved to: {screenshot_path}")

        except Exception as e:
            print(f"[-] Navigation failed: {e}")
            try:
                err_screenshot = os.path.join(DEBUG_DIR, "local_error.png")
                await page.screenshot(path=err_screenshot)
                print(f"[!] Saved error screenshot to: {err_screenshot}")
            except Exception:
                pass
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(test_local_hillsborough())
