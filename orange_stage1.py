import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from database import is_instrument_recorded, insert_or_update_lp, init_db
from storage import upload_file_to_cloud
from orange_helpers import (
    extract_orange_instrument_number,
    extract_orange_case_number,
    download_orange_vector_pdf
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads/orange/lis_pendens"))
DEBUG_DIR = os.getenv("DEBUG_DIR", os.path.join(BASE_DIR, "debug_screenshots/orange"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

ORANGE_RECORDER_URL = "https://selfservice.or.occompt.com/ssweb/user/disclaimer"
ORANGE_SEARCH_URL = "https://selfservice.or.occompt.com/ssweb/search/officialrecords"


async def run_orange_stage1(target_date: str = None) -> int:
    """Orange County Comptroller Official Records Lis Pendens pipeline."""
    init_db()

    if not target_date:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")

    print(f"\n{'='*60}")
    print(f"[*] ORANGE COUNTY STAGE 1: Official Records Collection")
    print(f"[*] Target Recording Date: {target_date}")
    print(f"{'='*60}")

    async with async_playwright() as p:
        print("[*] Launching Chromium Browser (Direct Connection)...")
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = await context.new_page()

        try:
            print("[*] Navigating to Orange County Comptroller Self-Service portal...")
            await page.goto(ORANGE_RECORDER_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)

            # Accept disclaimer
            print("[*] Accepting portal legal disclaimer...")
            try:
                for frame in page.frames:
                    if "recaptcha" in frame.url:
                        checkbox = frame.locator("#recaptcha-anchor, .recaptcha-checkbox").first
                        if await checkbox.is_visible():
                            await checkbox.click(timeout=5000)
                            await page.wait_for_timeout(3000)
                await page.evaluate("""() => {
                    const btn = document.querySelector('#submitDisclaimerAccept') || document.querySelector('input[value=\"I Accept\"]');
                    if (btn) { btn.disabled = false; btn.click(); }
                }""")
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            # Navigate to Official Records search form
            print(f"[*] Navigating to Official Records search form...")
            try:
                clicked_info = await page.evaluate("""() => {
                    const el = document.querySelector("a[href*='search/DOCSEARCH'], a[href*='search/'], a[href*='officialrecords']");
                    if (el) {
                        el.click();
                        return el.href;
                    }
                    const allLinks = Array.from(document.querySelectorAll('a'));
                    const recLink = allLinks.find(a => (a.textContent || '').includes('Official Records') || (a.textContent || '').includes('Search'));
                    if (recLink) {
                        recLink.click();
                        return recLink.href;
                    }
                    return null;
                }""")
                print(f"[*] Triggered DOM click on search link: {clicked_info}")
            except Exception as nav_e:
                print(f"[!] DOM click fallback: {nav_e}")
                nav_link = page.locator("a[href*='search/'], a[href*='officialrecords'], a:has-text('Official Records')").first
                await nav_link.click(force=True, timeout=5000)

            await page.wait_for_timeout(4000)
            print(f"[*] Loaded: {page.url} | Title: '{await page.title()}'")

            print(f"[*] Entering date range: {target_date}...")
            date_from = page.locator("input[name*='recordingDate-from'], input[name*='recordedDateStart'], input[name*='dateFrom'], input#recordingDate-from, input#recordedDateStart, input#dateFrom, input[placeholder*='MM/DD/YYYY']").first
            date_to = page.locator("input[name*='recordingDate-to'], input[name*='recordedDateEnd'], input[name*='dateTo'], input#recordingDate-to, input#recordedDateEnd, input#dateTo").first

            if await date_from.is_visible():
                await date_from.fill(target_date)
            if await date_to.is_visible():
                await date_to.fill(target_date)

            print("[*] Filtering by Document Type: Lis Pendens (LP)...")
            doc_type_input = page.locator("input[name*='docType'], input#docType, input[placeholder*='Doc Type'], input[name*='documentType']").first
            if await doc_type_input.is_visible():
                await doc_type_input.fill("LP")
                await page.keyboard.press("Tab")
            else:
                doc_type_select = page.locator("select[name*='docType'], select#docType, select[name*='documentType']").first
                if await doc_type_select.is_visible():
                    try:
                        await doc_type_select.select_option(label="LIS PENDENS")
                    except Exception:
                        try:
                            await doc_type_select.select_option(value="LP")
                        except Exception:
                            pass

            print(f"[*] Submitting search...")
            search_btn = page.locator("button#submitSearch, input#btnSearch, input[value='Search'], button#btnSearch, button:has-text('Search'), a.ui-btn:has-text('Search'), a:has-text('Search'), input[type='submit']").first
            try:
                await search_btn.click(timeout=8000)
            except Exception:
                print("[*] Search button click fallback: pressing Enter...")
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(4000)

            row_selector = "table.results tr:not(:first-child), table#searchResults tr:not(:first-child), table[class*='results'] tr:not(:first-child), table.report tr:not(:first-child)"
            result_rows = page.locator(row_selector)

            try:
                await result_rows.first.wait_for(state="visible", timeout=25000)
            except Exception:
                page_text = await page.content()
                if "No records found" in page_text or "0 results" in page_text.lower():
                    print(f"[!] No Lis Pendens filed in Orange County on {target_date}.")
                    await browser.close()
                    return 0

            rows = await page.locator(row_selector).all()
            print(f"[*] Found {len(rows)} potential result rows.")

            total_processed = 0
            for idx, row in enumerate(rows):
                try:
                    row_text = await row.inner_text()
                    if not row_text.strip() or ("LP" not in row_text.upper() and "LIS" not in row_text.upper()):
                        continue

                    cells = await row.locator("td").all_inner_texts()
                    party_link = row.locator("td a").first
                    party_name = (await party_link.inner_text()).strip() if await party_link.is_visible() else f"Orange_Record_{idx}"
                    page_content = await page.content()

                    instrument_number = extract_orange_instrument_number(cells, page_content, idx)

                    if is_instrument_recorded(instrument_number):
                        print(f"--- [{total_processed + 1}] {party_name} (#{instrument_number}): already recorded. Skipping. ---")
                        total_processed += 1
                        continue

                    print(f"\n--- [{total_processed + 1}] Processing Orange County LP: {party_name} (#{instrument_number}) ---")

                    try:
                        await party_link.click(timeout=10000)
                    except Exception:
                        await party_link.click(force=True, timeout=10000)
                    await page.wait_for_timeout(2500)

                    doc_content = await page.content()
                    case_no = extract_orange_case_number(cells, doc_content)
                    print(f"    ├── Extracted Case No: '{case_no}'")

                    clean_date = target_date.replace("/", "-")
                    filename = f"Orange_LP_{clean_date}_{instrument_number}.pdf"
                    pdf_path = os.path.join(DOWNLOAD_DIR, filename)

                    download_success = await download_orange_vector_pdf(page, pdf_path)
                    cloud_url = pdf_path
                    if download_success and os.path.exists(pdf_path):
                        print(f"    ├── Downloaded Vector PDF: {filename} ({os.path.getsize(pdf_path)} bytes)")
                        s3_key = f"orange/{clean_date}/{filename}"
                        uploaded = upload_file_to_cloud(pdf_path, s3_key)
                        if uploaded:
                            cloud_url = uploaded

                    insert_or_update_lp(
                        instrument_number=instrument_number,
                        recording_date=target_date,
                        party_name=party_name,
                        case_number=case_no,
                        pdf_path=cloud_url if download_success else ""
                    )
                    total_processed += 1

                    back_btn = page.locator("a:has-text('Back to Results'), input[value*='Back'], button:has-text('Back')").first
                    if await back_btn.is_visible():
                        await back_btn.click()
                    else:
                        await page.go_back()
                    await page.wait_for_timeout(1500)

                except Exception as row_err:
                    print(f"    [!] Error row {idx + 1}: {row_err}")
                    try:
                        await page.go_back()
                        await page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    continue

            await browser.close()
            print(f"\n[✓] Orange County Stage 1 Completed: Processed {total_processed} Lis Pendens records.")
            return total_processed

        except Exception as e:
            print(f"\n[-] Orange County Stage 1 Exception: {e}")
            await browser.close()
            raise e


if __name__ == "__main__":
    asyncio.run(run_orange_stage1())
