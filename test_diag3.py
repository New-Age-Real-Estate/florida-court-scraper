import sys
import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
endpoint = os.getenv("BRIGHT_DATA_WS_ENDPOINT")

print(f"[1] Connecting to CDP: {endpoint[:45]}...", flush=True)

try:
    with sync_playwright() as p:
        print("[2] Spawning connection...", flush=True)
        browser = p.chromium.connect_over_cdp(endpoint, timeout=30000)
        print("[3] Connected successfully!", flush=True)
        page = browser.new_page()
        print("[4] New page created!", flush=True)
        page.goto("https://example.com", timeout=30000)
        print(f"[5] Navigated! Title: {page.title()}", flush=True)
        browser.close()
        print("[6] Closed browser cleanly.", flush=True)
except Exception as e:
    print(f"[-] Error encountered: {e}", flush=True)

print("[7] All done.", flush=True)
