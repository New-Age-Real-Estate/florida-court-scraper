import asyncio
import os
import re
from playwright.async_api import async_playwright

async def probe_orange_comptroller():
    print("\n" + "="*60)
    print("[PROBE 1] Testing Orange County Comptroller (selfservice.or.occompt.com)")
    print("="*60)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = await context.new_page()
        try:
            url = "https://selfservice.or.occompt.com/ssweb/user/disclaimer"
            print(f"[*] Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            print(f"[*] Final URL: {page.url} | Title: '{await page.title()}'")

            # Check for reCAPTCHA iframe
            recaptcha_frame = None
            for frame in page.frames:
                if "recaptcha" in frame.url:
                    recaptcha_frame = frame
                    print(f"[*] Found reCAPTCHA frame: {frame.url[:80]}...")
                    break

            if recaptcha_frame:
                print("[*] Attempting to click reCAPTCHA checkbox in iframe...")
                try:
                    checkbox = recaptcha_frame.locator("#recaptcha-anchor, .recaptcha-checkbox").first
                    await checkbox.click(timeout=5000)
                    print("[+] Clicked reCAPTCHA checkbox!")
                    await page.wait_for_timeout(4000)
                except Exception as e:
                    print(f"[-] Could not click reCAPTCHA: {e}")

            # Check submit button state
            btn = page.locator("#submitDisclaimerAccept")
            if await btn.is_visible():
                is_disabled = await btn.is_disabled()
                print(f"[*] Submit button visible. Disabled: {is_disabled}")
                # Try clicking or enabling
                await page.evaluate("() => { const b = document.querySelector('#submitDisclaimerAccept'); if (b) { b.disabled = false; b.click(); } }")
                await page.wait_for_timeout(3000)
                print(f"[*] Post-disclaimer URL: {page.url} | Title: '{await page.title()}'")

            # Navigate to Official Records search form
            search_url = "https://selfservice.or.occompt.com/ssweb/search/officialrecords"
            print(f"[*] Navigating to search form: {search_url}...")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            print(f"[*] Search page URL: {page.url} | Title: '{await page.title()}'")

            # List visible forms, inputs, selects, and buttons on search page
            inputs = await page.locator("input, button, select, a.ui-btn").all()
            print(f"[*] Total elements on search page: {len(inputs)}")
            for idx, inp in enumerate(inputs[:30]):
                tag = await inp.evaluate("el => el.tagName")
                inp_id = await inp.get_attribute("id") or ""
                inp_name = await inp.get_attribute("name") or ""
                inp_type = await inp.get_attribute("type") or ""
                inp_placeholder = await inp.get_attribute("placeholder") or ""
                text = (await inp.inner_text()).strip()[:40] if tag in ("BUTTON", "A") else ""
                print(f"    [{idx}] <{tag} id='{inp_id}' name='{inp_name}' type='{inp_type}' placeholder='{inp_placeholder}'> {text}")

            os.makedirs("downloads/debug_orange", exist_ok=True)
            await page.screenshot(path="downloads/debug_orange/comptroller_search_probe.png", full_page=True)
            print("[+] Saved probe screenshot: downloads/debug_orange/comptroller_search_probe.png")

        except Exception as e:
            print(f"[-] Comptroller probe error: {e}")
        finally:
            await browser.close()


async def probe_orange_clerk_of_courts():
    print("\n" + "="*60)
    print("[PROBE 2] Testing Orange County Clerk of Courts (myeclerk.myorangeclerk.com)")
    print("="*60)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = await context.new_page()
        try:
            url = "https://myeclerk.myorangeclerk.com/Cases/Search?caseType=CV"
            print(f"[*] Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)
            print(f"[*] Final URL: {page.url} | Title: '{await page.title()}'")

            # Look for DateFrom and DateTo
            date_from = page.locator("#DateFrom, input[name='DateFrom']").first
            date_to = page.locator("#DateTo, input[name='DateTo']").first
            search_btn = page.locator("input[type='submit'][value*='Search'], button:has-text('Search'), input#btnSearch").first

            target_date = "10/22/2026"
            if await date_from.is_visible() and await date_to.is_visible():
                print(f"[*] Filling DateFrom and DateTo with {target_date}...")
                await date_from.fill(target_date)
                await date_to.fill(target_date)
                await page.wait_for_timeout(500)

                if await search_btn.is_visible():
                    print("[*] Clicking Search on myEClerk...")
                    await search_btn.click()
                    await page.wait_for_timeout(5000)
                    print(f"[*] Search results page URL: {page.url} | Title: '{await page.title()}'")

                    # Check for results table
                    rows = await page.locator("table tbody tr, table tr:not(:first-child)").all()
                    print(f"[*] Found {len(rows)} result rows in myEClerk Civil search!")
                    for idx, r in enumerate(rows[:5]):
                        txt = (await r.inner_text()).replace('\n', ' | ')
                        print(f"    [Row {idx+1}] {txt[:120]}")

            os.makedirs("downloads/debug_orange", exist_ok=True)
            await page.screenshot(path="downloads/debug_orange/myeclerk_probe.png", full_page=True)
            print("[+] Saved probe screenshot: downloads/debug_orange/myeclerk_probe.png")

        except Exception as e:
            print(f"[-] myEClerk probe error: {e}")
        finally:
            await browser.close()


async def main():
    await probe_orange_comptroller()
    await probe_orange_clerk_of_courts()

if __name__ == "__main__":
    asyncio.run(main())
