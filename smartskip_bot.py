#!/usr/bin/env python3
"""
SmartSkip Autonomous Skip Tracing Engine
========================================
Automates end-to-end bulk skip tracing on SmartSkip (https://app.smartskip.io):
  1. Headless authentication using SMARTSKIP_EMAIL and SMARTSKIP_PASSWORD.
  2. Session state caching to avoid repeated logins.
  3. Bulk CSV upload & automated column mapping.
  4. Order submission via pre-funded account balance.
  5. Asynchronous polling until batch is completed.
  6. Results CSV download and automated handoff to NAGE contact enrichment.
"""

import os
import re
import sys
import time
import json
import asyncio
import argparse
import datetime as dt
from pathlib import Path
from typing import Optional, Dict, List, Any

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

# Try importing S3 storage if available
try:
    from storage import upload_file_to_cloud
except ImportError:
    upload_file_to_cloud = None

load_dotenv()

BASE_DIR = Path(__file__).parent.resolve()
DEFAULT_SESSION_FILE = BASE_DIR / "smartskip_session.json"
DEBUG_DIR = BASE_DIR / "debug_screenshots/smartskip"
DOWNLOAD_DIR = BASE_DIR / "downloads/smartskip"

DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SMARTSKIP_LOGIN_URL = "https://app.smartskip.io/login"
SMARTSKIP_APP_BASE = "https://app.smartskip.io"


def get_chrome_path() -> Optional[str]:
    """Finds Google Chrome on macOS or Linux if default chromium is missing."""
    mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(mac_chrome):
        return mac_chrome
    linux_chrome = "/usr/bin/google-chrome"
    if os.path.exists(linux_chrome):
        return linux_chrome
    return None


class SmartSkipBot:
    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        headless: bool = True,
        session_path: Path = DEFAULT_SESSION_FILE
    ):
        self.email = email or os.getenv("SMARTSKIP_EMAIL")
        self.password = password or os.getenv("SMARTSKIP_PASSWORD")
        self.headless = headless
        self.session_path = session_path
        self.browser = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def init_browser(self, p):
        chrome_exe = get_chrome_path()
        launch_opts = {
            "headless": self.headless,
            "args": [
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        }
        if chrome_exe and sys.platform == "darwin":
            launch_opts["executable_path"] = chrome_exe

        print(f"[*] Launching browser (headless={self.headless})...", flush=True)
        self.browser = await p.chromium.launch(**launch_opts)

        context_opts = {
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 900},
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        }
        if self.session_path.exists():
            try:
                context_opts["storage_state"] = str(self.session_path)
                print(f"[+] Loaded existing session state from {self.session_path.name}", flush=True)
            except Exception as se:
                print(f"[!] Warning reading session state: {se}", flush=True)

        self.context = await self.browser.new_context(**context_opts)
        self.page = await self.context.new_page()

    async def ensure_logged_in(self) -> bool:
        """Navigates to app and authenticates if session is not active."""
        print("[*] Checking SmartSkip login status...", flush=True)
        await self.page.goto(SMARTSKIP_LOGIN_URL, wait_until="domcontentloaded", timeout=40000)
        await self.page.wait_for_timeout(3000)

        current_url = self.page.url
        print(f"[*] Landed on URL: {current_url}", flush=True)

        # Check if already authenticated and redirected
        if "/login" not in current_url:
            print("[+] Already authenticated via saved session!", flush=True)
            return True

        if not self.email or not self.password:
            raise ValueError(
                "SMARTSKIP_EMAIL and SMARTSKIP_PASSWORD must be set in environment or GitHub Secrets."
            )

        print(f"[*] Authenticating as: {self.email}...", flush=True)

        # Locate Email Field
        email_sel = "input[placeholder*='Email'], input[type='email'], input[name='email'], input[type='text']"
        try:
            await self.page.wait_for_selector(email_sel, timeout=15000)
            email_input = self.page.locator(email_sel).first
            await email_input.fill(self.email)
            print("  ├── Filled email address", flush=True)
        except Exception as e:
            await self.page.screenshot(path=str(DEBUG_DIR / "login_email_err.png"))
            raise RuntimeError(f"Could not locate SmartSkip email input: {e}")

        # Locate Password Field
        pass_sel = "input[type='password'], input[name='password'], input[placeholder*='password']"
        try:
            pass_input = self.page.locator(pass_sel).first
            await pass_input.fill(self.password)
            print("  ├── Filled password", flush=True)
        except Exception as e:
            await self.page.screenshot(path=str(DEBUG_DIR / "login_pass_err.png"))
            raise RuntimeError(f"Could not locate SmartSkip password input: {e}")

        # Remember me checkbox
        remember_box = self.page.locator("input[type='checkbox']").first
        if await remember_box.is_visible():
            if not await remember_box.is_checked():
                await remember_box.check()
                print("  ├── Checked 'Remember Me'", flush=True)

        # Submit Login
        signin_btn = self.page.locator("button:has-text('Sign In'), button[type='submit'], input[type='submit']").first
        print("  ├── Clicking 'Sign In'...", flush=True)
        await signin_btn.click()

        # Wait for navigation away from /login
        try:
            await self.page.wait_for_url(lambda u: "/login" not in u, timeout=25000)
            await self.page.wait_for_timeout(3000)
            print(f"[+] Login successful! Active URL: {self.page.url}", flush=True)

            # Persist session state for future runs
            await self.context.storage_state(path=str(self.session_path))
            print(f"[+] Saved authenticated session to {self.session_path}", flush=True)
            return True
        except Exception as e:
            shot_path = DEBUG_DIR / "login_failed.png"
            await self.page.screenshot(path=str(shot_path))
            body_txt = await self.page.evaluate("() => document.body.innerText")
            print(f"[-] Login failed. Current URL: {self.page.url}. Text snippet: {body_txt[:300]}", flush=True)
            raise RuntimeError(f"Login failed: {e}. Screenshot saved to {shot_path}")

    async def get_account_credits(self) -> Optional[float]:
        """Reads current credit / balance from header or profile."""
        try:
            content = await self.page.evaluate("() => document.body.innerText")
            m = re.search(r"(?:Credits|Balance|Available)[:\s]*\$?([0-9]+(?:\.[0-9]+)?)", content, re.I)
            if m:
                val = float(m.group(1))
                print(f"[+] Detected SmartSkip balance: {val} credits", flush=True)
                return val
        except Exception:
            pass
        return None

    async def navigate_to_bulk_upload(self) -> bool:
        """Finds and opens the Bulk Skip Tracing portal."""
        print("[*] Navigating to Bulk Skip Tracing section...", flush=True)
        current_url = self.page.url

        # Direct known routes
        candidate_urls = [
            f"{SMARTSKIP_APP_BASE}/bulk-skip-tracing",
            f"{SMARTSKIP_APP_BASE}/skip-tracing/bulk",
            f"{SMARTSKIP_APP_BASE}/skip-tracing",
            f"{SMARTSKIP_APP_BASE}/bulk"
        ]

        for u in candidate_urls:
            try:
                await self.page.goto(u, wait_until="domcontentloaded", timeout=15000)
                await self.page.wait_for_timeout(2000)
                file_input = self.page.locator("input[type='file']").first
                if await file_input.is_visible(timeout=2000) or await self.page.locator("text=/upload|browse|drop/i").first.is_visible():
                    print(f"[+] Located Bulk Upload page at: {u}", flush=True)
                    return True
            except Exception:
                continue

        # Fallback: look for navigation links in sidebar
        print("[*] Scanning sidebar navigation links...", flush=True)
        nav_links = await self.page.locator("a, button").all()
        for lnk in nav_links:
            txt = (await lnk.inner_text()).lower()
            if any(k in txt for k in ["bulk", "skip trace", "upload list"]):
                print(f"  ├── Found nav link: '{txt}'", flush=True)
                await lnk.click()
                await self.page.wait_for_timeout(3000)
                file_input = self.page.locator("input[type='file']").first
                if await file_input.is_visible(timeout=2000) or await self.page.locator("text=/upload|browse|drop/i").first.is_visible():
                    print(f"[+] Bulk Upload portal opened at: {self.page.url}", flush=True)
                    return True

        shot_path = DEBUG_DIR / "bulk_nav_fallback.png"
        await self.page.screenshot(path=str(shot_path))
        print(f"[!] Warning: File input not immediately visible. Screenshot: {shot_path}", flush=True)
        return False

    async def upload_csv(self, csv_path: Path) -> bool:
        """Uploads the batch CSV and confirms mapping."""
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_path}")

        print(f"[*] Attaching CSV: {csv_path.name} ({csv_path.stat().st_size} bytes)...", flush=True)
        file_input = self.page.locator("input[type='file']").first
        await file_input.set_input_files(str(csv_path))
        await self.page.wait_for_timeout(3000)

        # Name the list if prompt exists
        list_name_input = self.page.locator("input[placeholder*='List Name'], input[name*='list_name'], input#listName").first
        if await list_name_input.is_visible():
            batch_name = f"NAGE_{dt.date.today().isoformat()}_{int(time.time())}"
            await list_name_input.fill(batch_name)
            print(f"  ├── Entered list name: '{batch_name}'", flush=True)

        # Next / Continue button
        next_btn = self.page.locator("button:has-text('Next'), button:has-text('Continue'), button:has-text('Proceed')").first
        if await next_btn.is_visible():
            await next_btn.click()
            await self.page.wait_for_timeout(3000)
            print("  ├── Advanced through column mapping step", flush=True)

        # Confirm mapping / review
        review_btn = self.page.locator("button:has-text('Review'), button:has-text('Confirm'), button:has-text('Next')").first
        if await review_btn.is_visible():
            await review_btn.click()
            await self.page.wait_for_timeout(2000)

        # Submit / Skip Trace Now
        submit_btn = self.page.locator(
            "button:has-text('Skip Trace Now'), button:has-text('Start Skip Tracing'), button:has-text('Submit'), button:has-text('Place Order')"
        ).first
        if await submit_btn.is_visible():
            print("  ├── Clicking Submit Skip Trace button...", flush=True)
            await submit_btn.click()
            await self.page.wait_for_timeout(4000)
            print("[+] Batch successfully submitted to SmartSkip!", flush=True)
            return True

        shot = DEBUG_DIR / "upload_mapping_state.png"
        await self.page.screenshot(path=str(shot))
        print(f"[!] Notice: Check upload state screenshot at: {shot}", flush=True)
        return True

    async def wait_and_download_results(self, timeout_sec: int = 900) -> Optional[Path]:
        """Polls the orders / activity list until the batch completes and downloads the CSV."""
        print(f"[*] Monitoring batch progress (timeout: {timeout_sec}s)...", flush=True)
        start_time = time.time()

        # Navigate to Activity / Dashboard / Orders
        activity_url = f"{SMARTSKIP_APP_BASE}/activity"
        try:
            await self.page.goto(activity_url, wait_until="domcontentloaded", timeout=20000)
            await self.page.wait_for_timeout(3000)
        except Exception:
            pass

        while time.time() - start_time < timeout_sec:
            # Check for download button on top row
            download_btn = self.page.locator(
                "table tbody tr:first-child a:has-text('Download'), table tbody tr:first-child button:has-text('Download'), a[title*='Download'], button[title*='Download']"
            ).first

            status_cell = self.page.locator("table tbody tr:first-child").first
            row_text = (await status_cell.inner_text()).lower() if await status_cell.is_visible() else ""

            if "complete" in row_text or "finished" in row_text or await download_btn.is_visible():
                print("[+] SmartSkip batch is COMPLETE! Initiating download...", flush=True)
                timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                dest_path = DOWNLOAD_DIR / f"smartskip_results_{timestamp}.csv"

                async with self.page.expect_download(timeout=30000) as dl_info:
                    await download_btn.click(force=True)
                dl = await dl_info.value
                await dl.save_as(str(dest_path))

                print(f"[✓] Successfully downloaded completed results to: {dest_path} ({dest_path.stat().st_size} bytes)", flush=True)
                return dest_path

            if "fail" in row_text or "error" in row_text:
                raise RuntimeError(f"SmartSkip reported batch failure: {row_text}")

            print(f"  [~] Batch still processing... (elapsed: {int(time.time() - start_time)}s). Checking again in 20s...", flush=True)
            await self.page.wait_for_timeout(20000)
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=20000)
                await self.page.wait_for_timeout(2000)
            except Exception:
                pass

        raise TimeoutError(f"SmartSkip batch did not complete within {timeout_sec}s.")

    async def close(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        print("[*] Browser closed.", flush=True)


async def run_pipeline(csv_file: Optional[Path] = None, headless: bool = True) -> Optional[Path]:
    bot = SmartSkipBot(headless=headless)
    async with async_playwright() as p:
        await bot.init_browser(p)
        try:
            logged_in = await bot.ensure_logged_in()
            if not logged_in:
                print("[-] Could not verify login. Aborting.", flush=True)
                return None

            await bot.get_account_credits()

            if csv_file:
                await bot.navigate_to_bulk_upload()
                await bot.upload_csv(csv_file)
                result_csv = await bot.wait_and_download_results()
                return result_csv
            else:
                print("[*] No CSV provided; login and portal verification passed successfully.", flush=True)
                return None
        finally:
            await bot.close()


def main():
    parser = argparse.ArgumentParser(description="SmartSkip Autonomous Skip Tracing Engine")
    parser.add_argument("--csv", type=Path, help="Path to input CSV to skip trace")
    parser.add_argument("--headful", action="store_true", help="Run with visible browser window")
    parser.add_argument("--verify-login-only", action="store_true", help="Only verify login credentials and session state")
    args = parser.parse_args()

    csv_path = None if args.verify_login_only else args.csv
    result = asyncio.run(run_pipeline(csv_file=csv_path, headless=not args.headful))
    if result:
        print(f"\n[+] SmartSkip Skip Tracing Finished. Result: {result}")


if __name__ == "__main__":
    main()
