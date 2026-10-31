import sys
import os

print("[1] Script started", flush=True)

try:
    import playwright
    print("[2] Playwright imported successfully", flush=True)
except Exception as e:
    print(f"[-] Import error: {e}", flush=True)

print("[3] Script finished", flush=True)
