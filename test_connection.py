import os
import asyncio
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

BRIGHT_DATA_WS_ENDPOINT = os.getenv("BRIGHT_DATA_WS_ENDPOINT")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.getenv("DEBUG_DIR", os.path.join(BASE_DIR, "debug_screenshots"))
os.makedirs(DEBUG_DIR, exist_ok=True)

async def test_connection():
    if not BRIGHT_DATA_WS_ENDPOINT or "YOUR_CUSTOMER_ID" in BRIGHT_DATA_WS_ENDPOINT:
        print("[!] ERROR: Please set your valid BRIGHT_DATA_WS_ENDPOINT in .env first.")
        print("    Example: wss://brd-customer-xxxx-zone-scraping_browser:yyyy@brd.superproxy.io:9222")
        return

    print("[*] Connecting to Bright Data Scraping Browser via CDP...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(BRIGHT_DATA_WS_ENDPOINT)
            print("[+] Connection established successfully!")
            
            context = await browser.new_context()
            page = await context.new_page()

            target_url = "https://publicaccess.hillsclerk.com/oripublicaccess/"
            print(f"[*] Navigating to {target_url}...")
            await page.goto(target_url, wait_until="networkidle", timeout=60000)

            title = await page.title()
            print(f"[+] Page Title: '{title}'")

            screenshot_path = os.path.join(DEBUG_DIR, "test_hillsclerk.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            print(f"[+] Diagnostic screenshot saved to: {screenshot_path}")

            await browser.close()
            print("[✓] Bright Data connection test PASSED!")

        except Exception as e:
            print(f"[-] Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
