import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from database import is_instrument_recorded, insert_or_update_lp
from storage import upload_file_to_cloud

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads/orange/lis_pendens"))
DEBUG_DIR = os.getenv("DEBUG_DIR", os.path.join(BASE_DIR, "debug_screenshots/orange"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

ORANGE_RECORDER_URL = "https://or.occompt.com/recorder/web/"


async def run_orange_stage1(target_date: str = None) -> int:
    """
    Orange County Official Records (Comptroller - Tyler EagleRecorder) Scraper.
    Searches for all Lis Pendens recorded on target_date, downloads original vector PDFs,
    extracts metadata and case numbers, and syncs to S3.
    """
    if not target_date:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")

    print(f"\n{'='*60}")
    print(f"[*] ORANGE COUNTY STAGE 1: Comptroller Official Records (EagleRecorder)")
    print(f"[*] Target Recording Date: {target_date}")
    print(f"{'='*60}")

    async with async_playwright() as p:
        # Launch Chromium with anti-detection args
        print("[*] Launching Chromium Browser for Orange County Comptroller...")
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = await context.new_page()

        try:
            # 1. Open EagleRecorder Portal
            print("[*] Navigating to Orange County Comptroller portal...")
            await page.goto(ORANGE_RECORDER_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)

            # 2. Handle Legal Disclaimer ("I Accept") on both EagleRecorder and Self-Service
            print(f"[*] Current portal URL: {page.url}")
            accept_btn = page.locator("input[value='I Accept'], input[name='submit'][value*='Accept'], button:has-text('I Accept'), button#submitDisclaimerAccept").first
            if await accept_btn.is_visible():
                print("[*] Accepting portal legal disclaimer...")
                try:
                    await page.evaluate("""() => {
                        const btn = document.querySelector('#submitDisclaimerAccept') || document.querySelector('input[value=\"I Accept\"]');
                        if (btn) {
                            btn.disabled = false;
                            btn.click();
                        }
                    }""")
                except Exception:
                    await accept_btn.click()
                await page.wait_for_timeout(3000)

            # Check if portal redirected to Self-Service transfer button
            ss_btn = page.locator("button:has-text('Official Records Self-Service'), a:has-text('Official Records Self-Service')").first
            if await ss_btn.is_visible():
                print("[*] Navigating via Official Records Self-Service portal...")
                await ss_btn.click()
                await page.wait_for_timeout(3000)

            # 3. Navigate to Document Search if not directly on search form
            search_link = page.locator("a:has-text('Document Search'), a:has-text('Public Search'), a[href*='search.do'], a[href*='officialrecords']").first
            if await search_link.is_visible():
                print("[*] Opening Document Search...")
                await search_link.click()
                await page.wait_for_timeout(2500)

            # 4. Fill Recording Dates
            print(f"[*] Entering date range: {target_date}...")
            date_from = page.locator("input[name*='recordedDateStart'], input[name*='dateFrom'], input#recordedDateStart, input#dateFrom, input[placeholder*='MM/DD/YYYY']").first
            date_to = page.locator("input[name*='recordedDateEnd'], input[name*='dateTo'], input#recordedDateEnd, input#dateTo").first

            if await date_from.is_visible():
                await date_from.fill(target_date)
            if await date_to.is_visible():
                await date_to.fill(target_date)

            # 5. Fill Document Type for Lis Pendens
            print("[*] Filtering by Document Type: Lis Pendens (LP)...")
            doc_type_input = page.locator("input[name*='docType'], input#docType, input[placeholder*='Doc Type']").first
            if await doc_type_input.is_visible():
                await doc_type_input.fill("LP")
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(500)
            else:
                # Try select dropdown
                doc_type_select = page.locator("select[name*='docType'], select#docType").first
                if await doc_type_select.is_visible():
                    try:
                        await doc_type_select.select_option(label="LIS PENDENS")
                    except Exception:
                        try:
                            await doc_type_select.select_option(value="LP")
                        except Exception:
                            pass

            # 6. Click Search
            print("[*] Submitting search...")
            search_btn = page.locator("input#btnSearch, input[value='Search'], button#btnSearch, button:has-text('Search')").first
            await search_btn.click()
            await page.wait_for_timeout(3000)

            # 7. Check Results Table
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
                print("[!] Waiting for search results...")

            rows = await page.locator(row_selector).all()
            print(f"[*] Found {len(rows)} potential result rows.")

            total_processed = 0

            for idx, row in enumerate(rows):
                try:
                    row_text = await row.inner_text()
                    if not row_text.strip() or ("LP" not in row_text.upper() and "LIS" not in row_text.upper() and "PENDENS" not in row_text.upper()):
                        continue

                    # Extract Instrument # from row cells
                    cells = await row.locator("td").all_inner_texts()
                    instrument_number = None
                    for c in cells:
                        c_clean = c.strip()
                        # Orange county document numbers are typically 10-12 digits (e.g., 20240123456)
                        if re.match(r"^202[0-9]{7,9}$", c_clean):
                            instrument_number = c_clean
                            break

                    party_link = row.locator("td a").first
                    party_name = (await party_link.inner_text()).strip() if await party_link.is_visible() else f"Orange_Record_{idx}"

                    # Deduplication check before opening document
                    if instrument_number and is_instrument_recorded(instrument_number):
                        print(f"--- [{total_processed + 1}] {party_name} (#{instrument_number}): already collected. Skipping. ---")
                        total_processed += 1
                        continue

                    print(f"\n--- [{total_processed + 1}] Processing Orange County LP: {party_name} ---")

                    # Extract Case Number from table cells
                    case_no = ""
                    for c in cells:
                        m = re.search(r"\b(20[0-9]{2}-[A-Z]{2}-[0-9]{4,7}(?:-O)?|[0-9]{2,4}-[A-Z]{2}-[0-9]{4,7})\b", c, re.I)
                        if m:
                            case_no = m.group(1).upper()
                            break

                    # Click into document view
                    try:
                        await party_link.click(timeout=10000)
                    except Exception:
                        await party_link.click(force=True, timeout=10000)
                    await page.wait_for_timeout(3000)

                    # If instrument number was not in cells, extract from document details
                    if not instrument_number:
                        page_content = await page.content()
                        m_inst = re.search(r"Doc(?:ument)?\s*(?:#|Number|No\.?)?\s*[:\s]*([0-9]{8,12})", page_content, re.I)
                        if m_inst:
                            instrument_number = m_inst.group(1).strip()
                        else:
                            instrument_number = f"ORANGE_{datetime.now().strftime('%Y%m%d%H%M%S')}_{idx}"

                    # If case number not found in table, extract from document details
                    if not case_no:
                        page_content = await page.content()
                        m_case = re.search(r"\b(20[0-9]{2}-[A-Z]{2}-[0-9]{4,7}(?:-O)?|[0-9]{2,4}-[A-Z]{2}-[0-9]{4,7})\b", page_content, re.I)
                        if m_case:
                            case_no = m_case.group(1).upper()

                    print(f"    ├── Instrument #: {instrument_number}")
                    print(f"    ├── Extracted Case No: '{case_no}'")

                    # Prepare download filename
                    clean_date = target_date.replace("/", "-")
                    filename = f"Orange_LP_{clean_date}_{instrument_number}.pdf"
                    pdf_path = os.path.join(DOWNLOAD_DIR, filename)

                    # Download Vector PDF Document
                    download_success = False

                    # Method A: Direct EagleRecorder print/download link
                    try:
                        dl_link = page.locator("a[href*='printDoc'], a[href*='download'], a[title*='Download'], button[title*='Download'], a:has-text('View Document'), a:has-text('Download Image')").first
                        if await dl_link.is_visible():
                            async with page.expect_download(timeout=12000) as dl_info:
                                await dl_link.click()
                            dl = await dl_info.value
                            await dl.save_as(pdf_path)
                            download_success = True
                            print(f"    ├── Downloaded PDF via direct link: {filename}")
                    except Exception:
                        pass

                    # Method B: Iframe PDF stream
                    if not download_success:
                        try:
                            pdf_iframe = page.locator("iframe[src*='pdf'], embed[src*='pdf'], iframe#docDisplay, iframe").first
                            if await pdf_iframe.is_visible():
                                src = await pdf_iframe.get_attribute("src")
                                if src:
                                    import urllib.parse
                                    full_src = urllib.parse.urljoin(page.url, src)
                                    parsed = urllib.parse.urlparse(full_src)
                                    qs = urllib.parse.parse_qs(parsed.query)
                                    pdf_url = urllib.parse.urljoin(full_src, qs["file"][0]) if "file" in qs else full_src
                                    if pdf_url.startswith("http"):
                                        resp = await page.request.get(pdf_url)
                                        if resp.status == 200:
                                            with open(pdf_path, "wb") as f:
                                                f.write(await resp.body())
                                            download_success = True
                                            print(f"    ├── Saved PDF via direct stream: {filename}")
                        except Exception as e:
                            print(f"    [!] Stream fetch: {e}")

                    # Upload to S3 cloud storage
                    cloud_url = pdf_path
                    if download_success and os.path.exists(pdf_path):
                        s3_key = f"orange/{clean_date}/{filename}"
                        uploaded = upload_file_to_cloud(pdf_path, s3_key)
                        if uploaded:
                            cloud_url = uploaded

                    # Save to database
                    insert_or_update_lp(
                        instrument_number=instrument_number,
                        recording_date=target_date,
                        party_name=party_name,
                        case_number=case_no,
                        pdf_path=cloud_url if download_success else ""
                    )
                    total_processed += 1

                    # Return to results list if in detail view
                    back_btn = page.locator("a:has-text('Back to Results'), input[value*='Back'], button:has-text('Back')").first
                    if await back_btn.is_visible():
                        await back_btn.click()
                        await page.wait_for_timeout(1500)
                    else:
                        await page.go_back()
                        await page.wait_for_timeout(1500)

                except Exception as row_err:
                    print(f"    [!] Error processing row {idx + 1}: {row_err}")
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
            print(f"\n[-] Exception during Orange County Stage 1: {e}")
            screenshot_path = os.path.join(DEBUG_DIR, f"orange_error_{datetime.now().strftime('%H%M%S')}.png")
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"[!] Saved error screenshot to: {screenshot_path}")
            except Exception:
                pass
            await browser.close()
            raise e


if __name__ == "__main__":
    asyncio.run(run_orange_stage1())
