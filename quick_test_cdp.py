import asyncio
import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

endpoint = os.getenv("BRIGHT_DATA_WS_ENDPOINT")

async def test():
    print(f"[*] Testing CDP endpoint: {endpoint[:45]}...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(endpoint, timeout=20000)
            print("[+] Successfully connected to Bright Data Scraping Browser!")
            page = await browser.new_page()
            await page.goto("https://example.com", timeout=15000)
            title = await page.title()
            print(f"[+] Successfully loaded page: '{title}'")
            await browser.close()
            print("[✓] ALL TESTS PASSED!")
        except Exception as e:
            print(f"[-] Test failed: {e}")

if __name__ == "__main__":
    asyncio.run(test())
