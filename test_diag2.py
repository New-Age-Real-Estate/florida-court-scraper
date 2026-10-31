import sys
from playwright.sync_api import sync_playwright

print("[1] Starting sync_playwright...", flush=True)
with sync_playwright() as p:
    print("[2] Playwright started!", flush=True)
print("[3] Exiting cleanly.", flush=True)
