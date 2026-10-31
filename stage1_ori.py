import os
import re
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import pypdf

from database import init_db, is_instrument_recorded, insert_or_update_lp
from storage import upload_file_to_cloud

load_dotenv()

BRIGHT_DATA_WS_ENDPOINT = os.getenv("BRIGHT_DATA_WS_ENDPOINT")
USE_LOCAL_BROWSER = os.getenv("USE_LOCAL_BROWSER", "false" if BRIGHT_DATA_WS_ENDPOINT else "true").lower() in ("true", "1", "yes")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads/lis_pendens"))
DEBUG_DIR = os.getenv("DEBUG_DIR", os.path.join(BASE_DIR, "debug_screenshots"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)


def extract_case_number_from_pdf(pdf_path: str) -> str:
    """Reads Page 1 of the PDF and extracts Florida court case numbers."""
    case_patterns = [
        # Explicit label: "Case No: 2024-CA-012345" or "CASE NO.: 24-CC-009871"
        r"(?:Case\s*No\.?|Case\s*#|CASE\s*NO\.?)\s*[:\s]*([0-9]{2,4}-[A-Z]{2}-[0-9]{4,7}[^\s\n,]*)",
        # Pattern like "24-CA-012345" or "2024-CC-001234"
        r"([0-9]{2,4}-[A-Z]{2}-[0-9]{4,7}(?:-DIV-[A-Z0-9]+)?)",
        # Florida UCN (Uniform Case Number) format: 292024CA012345A001HC
        r"(29[0-9]{4}[A-Z]{2}[0-9]{6}[A-Z0-9]*)",
    ]

    try:
        reader = pypdf.PdfReader(pdf_path)
        if not reader.pages:
            return ""
        page_text = reader.pages[0].extract_text() or ""

        for pattern in case_patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                cleaned = match.group(1).strip().strip(".,;:()")
                return cleaned
    except Exception as e:
        print(f"[-] [PDF Parser] Error reading {pdf_path}: {e}")

    return ""

    return ""


async def run_stage1_ori(target_date: str = None) -> int:
    """
    Automates Hillsborough Lis Pendens downloads for target_date (MM/DD/YYYY).
    Returns count of documents processed.
    """
    init_db()

    if not target_date:
        # Default to yesterday
        target_date = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")

    print(f"\n{'='*60}")
    print(f"[*] STAGE 1: Hillsborough Official Records (ORI) Collection")
    print(f"[*] Target Recording Date: {target_date}")
    print(f"{'='*60}")

    async with async_playwright() as p:
        if USE_LOCAL_BROWSER or not BRIGHT_DATA_WS_ENDPOINT or "YOUR_CUSTOMER_ID" in BRIGHT_DATA_WS_ENDPOINT:
            print("[*] Launching Local Chromium Browser (Free / Direct ISP connection)...")
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
        else:
            print("[*] Connecting to Bright Data Scraping Browser...")
            browser = await p.chromium.connect_over_cdp(BRIGHT_DATA_WS_ENDPOINT)
            context = await browser.new_context(accept_downloads=True)

        page = await context.new_page()

        try:
            # 1. Open Hillsborough Official Records Search
            print("[*] Navigating to Hillsborough Official Records portal...")
            await page.goto("https://publicaccess.hillsclerk.com/oripublicaccess/", wait_until="networkidle", timeout=60000)

            # 2. Under Search Types, click Document Type
            print("[*] Selecting 'Document Type' search...")
            doc_type_tab = page.locator("text='Document Type'").first
            await doc_type_tab.wait_for(state="visible", timeout=15000)
            await doc_type_tab.click()
            await page.wait_for_timeout(1000)

            # 3. Under Search Fields, click the box labeled Document Type Search
            chosen_container = page.locator(".chosen-choices, #chosen_input_OBKey__1285_1").first
            await chosen_container.click()
            await page.wait_for_timeout(500)

            # 4. Select (LP) LIS PENDENS (distinguish from RELLP)
            print("[*] Selecting '(LP) LIS PENDENS' from dropdown...")
            # We target the exact active list item for (LP) LIS PENDENS
            lp_option = page.locator("ul.chosen-results li:has-text('(LP) LIS PENDENS'), ul.chosen-results li[data-option-array-index='25']").first
            if await lp_option.is_visible():
                await lp_option.click()
            else:
                # Type LP to filter if not immediately visible
                input_field = page.locator("#chosen_input_OBKey__1285_1, input.chosen-search-input").first
                await input_field.fill("LP")
                await page.wait_for_timeout(500)
                await lp_option.click()

            print("[+] Selected '(LP) LIS PENDENS'")
            await page.wait_for_timeout(800)

            # 5. Recording Begin Date & End Date
            print(f"[*] Setting date range to {target_date}...")
            begin_input = page.locator("#OBKey__1634_1, input[placeholder='MM/DD/YYYY'].record-begin").first
            end_input = page.locator("#OBKey__1634_2, input[placeholder='MM/DD/YYYY'].record-end").first

            await begin_input.click()
            await begin_input.fill(target_date)
            await page.keyboard.press("Tab")

            await end_input.click()
            await end_input.fill(target_date)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(500)

            # 6. Click Search (handle double click if calendar popup merely closes)
            print("[*] Submitting search...")
            search_button = page.locator(".search-action-buttons button:has-text('Search'), button:has-text('Search')").first
            await search_button.click()
            await page.wait_for_timeout(1500)

            # 6. Click Search
            print("[*] Submitting search...")
            search_button = page.locator(".search-action-buttons button:has-text('Search'), button:has-text('Search')").first
            await search_button.click()
            await page.wait_for_timeout(2000)

            # Wait for real result rows (ignore the hidden template row: .jsgrid-nodata-row)
            print("[*] Waiting for search results table...")
            row_selector = "tr.jsgrid-row, tr.jsgrid-alt-row, table tbody tr:not(.jsgrid-nodata-row)"
            results_rows = page.locator(row_selector)

            try:
                await results_rows.first.wait_for(state="visible", timeout=25000)
            except Exception:
                # Check if truly no records filed on this date
                body_text = await page.content()
                if "No records found" in body_text or "No items to display" in body_text:
                    print(f"[!] No Lis Pendens records filed on {target_date}.")
                    await browser.close()
                    return 0
                # Sometimes the first click closed a datepicker overlay; click Search once more
                print("[*] Retrying search button click...")
                await search_button.click()
                await results_rows.first.wait_for(state="visible", timeout=25000)

            await page.wait_for_timeout(2000)

            # Optimize: Select 100 items per page to minimize pagination
            try:
                items_per_page = page.locator("select[aria-label*='Items per page'], select:has(option[value='100']), select:has(option[value='50'])").first
                if await items_per_page.is_visible():
                    await items_per_page.select_option(value="100")
                    await page.wait_for_timeout(2000)
            except Exception:
                pass

            # 7-13: Process Results Table
            total_processed = 0
            has_next_page = True
            page_index = 1

            while has_next_page:
                print(f"\n[*] Scanning page {page_index}...")
                rows = await page.locator("tr.jsgrid-row, tr.jsgrid-alt-row, table tbody tr:not(.jsgrid-nodata-row)").all()
                print(f"[*] Found {len(rows)} rows on current page.")

                for idx, row in enumerate(rows):
                    row_text = await row.inner_text()
                    if "LIS PENDENS" not in row_text.upper():
                        continue

                    # 8. In the Name column, click the linked party name
                    party_link = row.locator("td a").first
                    if not await party_link.is_visible():
                        continue

                    party_name = (await party_link.inner_text()).strip()
                    print(f"\n--- [{total_processed + 1}] Processing: {party_name} ---")

                    await party_link.click()
                    await page.wait_for_timeout(2500)

                    # 9. Extract Document Information (Instrument #)
                    doc_info_panel = page.locator(".doc-info, #documentInfo, div[class*='document-info']").first
                    doc_info_text = await doc_info_panel.inner_text() if await doc_info_panel.is_visible() else ""

                    inst_match = re.search(r"Instrument\s*(?:#|No\.?|Number)?\s*[:\s]*([0-9A-Z]+)", doc_info_text, re.I)
                    if inst_match:
                        instrument_number = inst_match.group(1).strip()
                    else:
                        # Fallback: check all modal text for instrument # pattern
                        full_modal_text = await page.content()
                        fallback_match = re.search(r"Instrument\s*(?:#|No\.?)?\s*[:\s]*([0-9]{8,12})", full_modal_text, re.I)
                        instrument_number = fallback_match.group(1) if fallback_match else f"INST_{datetime.now().strftime('%Y%m%d%H%M%S')}_{idx}"

                    print(f"    ├── Instrument #: {instrument_number}")

                    # Check deduplication
                    if is_instrument_recorded(instrument_number):
                        print(f"    └── Already collected. Skipping download.")
                        # Close popup modal
                        close_btn = page.locator("button.close, .ui-dialog-titlebar-close, [aria-label='Close'], button:has-text('✕')").first
                        if await close_btn.is_visible():
                            await close_btn.click()
                        await page.wait_for_timeout(1000)
                        continue

                    # 10. Prepare filename: Hillsborough_LP_[recording-date]_[instrument-number].pdf
                    clean_date = target_date.replace("/", "-")
                    filename = f"Hillsborough_LP_{clean_date}_{instrument_number}.pdf"
                    pdf_path = os.path.join(DOWNLOAD_DIR, filename)

                    # 11. Download the PDF
                    download_success = False
                    try:
                        # Look for download button in the toolbar
                        download_btn = page.locator(
                            "button[title='Download'], [aria-label='Download'], button#download, .pdf-toolbar button.download"
                        ).first
                        
                        async with page.expect_download(timeout=15000) as download_info:
                            await download_btn.click()
                        download = await download_info.value
                        await download.save_as(pdf_path)
                        download_success = True
                        print(f"    ├── Saved PDF: {filename}")
                    except Exception as dl_err:
                        print(f"    [!] Toolbar click download missed: {dl_err}. Checking iframe stream...")
                        # Fallback: retrieve directly from PDF iframe source
                        pdf_iframe = page.locator("iframe[src*='pdf'], embed[src*='pdf'], iframe#pdfViewer").first
                        if await pdf_iframe.is_visible():
                            src = await pdf_iframe.get_attribute("src")
                            if src and "blob:" not in src:
                                resp = await page.request.get(src)
                                with open(pdf_path, "wb") as f:
                                    f.write(await resp.body())
                                download_success = True
                                print(f"    ├── Saved PDF via direct stream: {filename}")

                    # Upload original vector PDF to S3 Cloud Bucket if configured
                    cloud_pdf_url = pdf_path
                    if download_success and os.path.exists(pdf_path):
                        uploaded_url = upload_file_to_cloud(pdf_path, f"hillsborough/{clean_date}/{filename}")
                        if uploaded_url:
                            cloud_pdf_url = uploaded_url

                    # 12. Parse Page 1 for Case No.
                    case_no = ""
                    if download_success and os.path.exists(pdf_path):
                        case_no = extract_case_number_from_pdf(pdf_path)
                        print(f"    ├── Extracted Case No: '{case_no}'")
                    else:
                        print(f"    [!] Warning: PDF could not be saved for {instrument_number}")

                    # Store in SQLite database
                    insert_or_update_lp(
                        instrument_number=instrument_number,
                        recording_date=target_date,
                        party_name=party_name,
                        case_number=case_no,
                        pdf_path=cloud_pdf_url if download_success else ""
                    )
                    total_processed += 1

                    # 13. Click the X at the upper-right corner of the document popup
                    close_btn = page.locator("button.close, .ui-dialog-titlebar-close, [aria-label='Close'], button:has-text('✕')").first
                    if await close_btn.is_visible():
                        await close_btn.click()
                    await page.wait_for_timeout(1000)

                # Pagination: Check for Next Page
                next_btn = page.locator("a:has-text('Next'), a[title*='next' i], .k-pager-next:not(.k-state-disabled)").first
                if await next_btn.is_visible() and await next_btn.is_enabled():
                    # Ensure it's not the disabled state
                    class_attr = await next_btn.get_attribute("class") or ""
                    if "disabled" in class_attr:
                        has_next_page = False
                    else:
                        print(f"[*] Navigating to next results page ({page_index + 1})...")
                        await next_btn.click()
                        page_index += 1
                        await page.wait_for_timeout(3000)
                else:
                    has_next_page = False

            await browser.close()
            print(f"\n[✓] Stage 1 Completed: Processed {total_processed} Lis Pendens records.")
            return total_processed

        except Exception as e:
            print(f"\n[-] Exception during Stage 1: {e}")
            screenshot_path = os.path.join(DEBUG_DIR, f"stage1_error_{datetime.now().strftime('%H%M%S')}.png")
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"[!] Saved error screenshot to: {screenshot_path}")
            except Exception:
                pass
            await browser.close()
            raise e

if __name__ == "__main__":
    asyncio.run(run_stage1_ori())
