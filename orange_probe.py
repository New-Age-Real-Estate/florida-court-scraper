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
            print(f"[*] Initial URL: {page.url} | Title: '{await page.title()}'")

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
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    print(f"[-] Could not click reCAPTCHA: {e}")

            # Accept disclaimer
            print("[*] Submitting disclaimer acceptance...")
            await page.evaluate("""() => {
                const b = document.querySelector('#submitDisclaimerAccept') || document.querySelector('input[value=\"I Accept\"]');
                if (b) { b.disabled = false; b.click(); }
            }""")
            await page.wait_for_timeout(4000)
            print(f"[*] Post-disclaimer URL: {page.url} | Title: '{await page.title()}'")

            # Discover all navigation links/tiles on the landing page
            links = await page.locator("a, button, div.tile, .ui-btn").all()
            print(f"[*] Discovered {len(links)} navigation elements on landing page:")
            search_target = None
            for idx, el in enumerate(links[:35]):
                tag = await el.evaluate("el => el.tagName")
                href = await el.get_attribute("href") or ""
                txt = (await el.inner_text()).strip().replace('\n', ' ')
                cid = await el.get_attribute("id") or ""
                if txt or href:
                    print(f"    [{idx}] <{tag} id='{cid}' href='{href}'> '{txt[:50]}'")
                if not search_target and ("official" in (txt + href).lower() or "search/officialrecords" in href.lower()):
                    search_target = el

            # Navigate to Search Page via DOM click
            print("[*] Navigating to Official Records search form via DOM...")
            try:
                clicked = await page.evaluate("""() => {
                    const el = document.querySelector("a[href*='search/DOCSEARCH'], a[href*='search/'], a[href*='officialrecords']");
                    if (el) { el.click(); return el.href; }
                    const allLinks = Array.from(document.querySelectorAll('a'));
                    const recLink = allLinks.find(a => (a.textContent || '').includes('Official Records') || (a.textContent || '').includes('Search'));
                    if (recLink) { recLink.click(); return recLink.href; }
                    return null;
                }""")
                print(f"[+] Clicked navigation link via DOM: {clicked}")
            except Exception as e:
                print(f"[-] DOM click exception: {e}")

            await page.wait_for_timeout(4000)
            print(f"[*] Current Page URL: {page.url} | Title: '{await page.title()}'")

            # Inspect all inputs, selects, buttons on current page
            inputs = await page.locator("input, select, button, a.ui-btn").all()
            print(f"[*] Total form controls: {len(inputs)}")
            for idx, inp in enumerate(inputs[:40]):
                tag = await inp.evaluate("el => el.tagName")
                inp_id = await inp.get_attribute("id") or ""
                inp_name = await inp.get_attribute("name") or ""
                inp_type = await inp.get_attribute("type") or ""
                inp_placeholder = await inp.get_attribute("placeholder") or ""
                val = await inp.get_attribute("value") or ""
                txt = (await inp.inner_text()).strip()[:30] if tag in ("BUTTON", "A") else ""
                print(f"    [{idx}] <{tag} id='{inp_id}' name='{inp_name}' type='{inp_type}' placeholder='{inp_placeholder}' value='{val}'> {txt}")

            # Test entering date and document type
            target_date = "10/22/2026"
            date_from = page.locator("input[name*='recordingDate-from'], input[name*='recordedDateStart'], input[name*='dateFrom'], input#recordingDate-from, input#recordedDateStart, input#dateFrom, input[placeholder*='MM/DD/YYYY']").first
            date_to = page.locator("input[name*='recordingDate-to'], input[name*='recordedDateEnd'], input[name*='dateTo'], input#recordingDate-to, input#recordedDateEnd, input#dateTo").first

            if await date_from.is_visible():
                print(f"[*] Filling recording dates with {target_date}...")
                await date_from.fill(target_date)
            if await date_to.is_visible():
                await date_to.fill(target_date)

            doc_type_input = page.locator("input[name*='docType'], input#docType, input[placeholder*='Doc Type'], input[name*='documentType']").first
            if await doc_type_input.is_visible():
                print("[*] Filling docType input with 'LP'...")
                await doc_type_input.fill("LP")
                await page.keyboard.press("Tab")

            search_btn = page.locator("button#submitSearch, input#btnSearch, input[value='Search'], button#btnSearch, button:has-text('Search'), a.ui-btn:has-text('Search'), input[type='submit']").first
            if await search_btn.is_visible():
                print("[*] Submitting search form...")
                await search_btn.click()
                await page.wait_for_timeout(5000)
                print(f"[*] Post-search URL: {page.url} | Title: '{await page.title()}'")

                # Check for results
                rows = await page.locator("table tr:not(:first-child)").all()
                print(f"[*] Found {len(rows)} table rows in search results!")
                for idx, r in enumerate(rows[:5]):
                    row_txt = (await r.inner_text()).replace('\n', ' | ')
                    print(f"    [Row {idx+1}] {row_txt[:120]}")

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

            # Inspect all selects and inputs
            selects = await page.locator("select").all()
            for s in selects:
                sid = await s.get_attribute("id") or ""
                sname = await s.get_attribute("name") or ""
                opts = await s.locator("option").all_inner_texts()
                print(f"    <SELECT id='{sid}' name='{sname}'> Options: {opts[:10]}")

            inputs = await page.locator("input, button").all()
            for inp in inputs[:15]:
                itag = await inp.evaluate("el => el.tagName")
                iid = await inp.get_attribute("id") or ""
                iname = await inp.get_attribute("name") or ""
                itype = await inp.get_attribute("type") or ""
                ival = await inp.get_attribute("value") or ""
                itxt = (await inp.inner_text()).strip()
                print(f"    <{itag} id='{iid}' name='{iname}' type='{itype}' value='{ival}'> {itxt}")

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
