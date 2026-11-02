import os
import re
import asyncio
from typing import Optional
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from database import get_pending_hover_cases, update_hover_results
from storage import upload_file_to_cloud

load_dotenv()

BRIGHT_DATA_WS_ENDPOINT = os.getenv("BRIGHT_DATA_WS_ENDPOINT")
USE_LOCAL_BROWSER = os.getenv("USE_LOCAL_BROWSER", "false" if BRIGHT_DATA_WS_ENDPOINT else "true").lower() in ("true", "1", "yes")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
raw_docket = os.getenv("DOCKET_DIR", "downloads/court_dockets")
DOCKET_DIR = raw_docket if os.path.isabs(raw_docket) else os.path.normpath(os.path.join(BASE_DIR, raw_docket))
raw_debug = os.getenv("DEBUG_DIR", "debug_screenshots")
DEBUG_DIR = raw_debug if os.path.isabs(raw_debug) else os.path.normpath(os.path.join(BASE_DIR, raw_debug))

os.makedirs(DOCKET_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

HOVER_SEARCH_URL = "https://hover.hillsclerk.com/html/case/caseSearch.html"


async def process_single_case(page, case_record: dict):
    case_no = case_record["case_number"].strip()
    inst_num = case_record["instrument_number"]

    print(f"\n[*] Searching HOVER for Case No: {case_no} (Inst #{inst_num})...")

    try:
        await page.goto(HOVER_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)

        # 1. Parse Case Number into Year, Type, and Sequence
        # Example formats: '26-CA-010772' -> Year: '26', Type: 'CA', Seq: '010772'
        m = re.search(r"(\d{2,4})-?([A-Z]{2})-?(\d+)", case_no)
        if m:
            yr = m.group(1)[-2:]
            court_type = m.group(2).upper()
            seq = m.group(3).zfill(6)
        else:
            yr = "26"
            court_type = "CA"
            seq = case_no

        # Fill segmented inputs on HOVER
        year_input = page.locator("#txtYear")
        if await year_input.is_visible():
            await year_input.fill(yr)
            await page.select_option("#ddlCourtType", court_type)
            await page.fill("#txtNumber", seq)
        else:
            case_input = page.locator("input#caseNumber:not([hidden]), input[name='caseNumber']:not([hidden])").first
            await case_input.fill(case_no)

        search_btn = page.locator("button[aria-label='Search'], button#searchBtn, button[type='submit']:has-text('Search')").first
        await search_btn.click()
        await page.wait_for_timeout(3000)

        # 2. Check for results & Open case summary
        details_btn = page.locator("button[aria-label='Displays detailed Case Information Page and Images, as available']").first
        case_row = page.locator(f"a:has-text('{case_no}'), tr:has-text('{case_no}') a").first

        if await details_btn.is_visible():
            await details_btn.click()
            await page.wait_for_timeout(3000)
        elif await case_row.is_visible():
            await case_row.click()
            await page.wait_for_timeout(3000)
        else:
            print(f"[-] Case {case_no} not found or may be sealed/pending indexing.")
            update_hover_results(inst_num, hover_status="NOT_FOUND", error_log="Case not found in HOVER search results")
            return

        # 3. Extract Debt / Claim Amount
        # Florida civil mortgage foreclosures and collections cases display claim amount in:
        # - Case Details / Amount in Controversy
        # - Initial Complaint docket entry
        debt_amount: Optional[float] = None
        page_html = await page.content()

        debt_regexes = [
            r"(?:Amount\s*in\s*Controversy|Claim\s*Amount|Principal\s*Balance|Debt\s*Amount|Judgment\s*Amount)\s*[:\$]*\s*([0-9,]+\.[0-9]{2})",
            r"(?:Demand\s*Amount)\s*[:\$]*\s*([0-9,]+\.[0-9]{2})",
            r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*(?:principal|due|claimed)",
        ]

        for reg in debt_regexes:
            match = re.search(reg, page_html, re.IGNORECASE)
            if match:
                raw_amt = match.group(1).replace(",", "")
                try:
                    debt_amount = float(raw_amt)
                    print(f"    ├── Found Claim/Debt Amount: ${debt_amount:,.2f}")
                    break
                except ValueError:
                    pass

        # 4. Locate and download the initial Complaint or Lis Pendens court filing
        docket_download_path = None
        complaint_link = page.locator(
            "tr:has-text('COMPLAINT') a[title*='View'], tr:has-text('COMPLAINT') a:has-text('View'), tr:has-text('LIS PENDENS') a"
        ).first

        if await complaint_link.is_visible():
            clean_case = case_no.replace("/", "-").replace(" ", "_")
            target_filename = f"HOVER_Docket_{clean_case}_Complaint.pdf"
            docket_download_path = os.path.join(DOCKET_DIR, target_filename)

            try:
                async with page.expect_download(timeout=15000) as dl_info:
                    await complaint_link.click()
                dl = await dl_info.value
                await dl.save_as(docket_download_path)
                print(f"    ├── Downloaded Complaint Docket: {target_filename}")
                cloud_docket_url = upload_file_to_cloud(docket_download_path, f"hillsborough/dockets/{target_filename}")
                if cloud_docket_url:
                    docket_download_path = cloud_docket_url
            except Exception as dl_err:
                print(f"    [!] Could not auto-download docket PDF: {dl_err}")

        # 5. Save results to database
        update_hover_results(
            instrument_number=inst_num,
            hover_status="COMPLETED",
            debt_amount=debt_amount,
            docket_pdf_path=docket_download_path
        )
        print(f"    └── Saved HOVER details to database.")

    except Exception as e:
        print(f"[-] Error processing {case_no} in HOVER: {e}")
        update_hover_results(inst_num, hover_status="ERROR", error_log=str(e))


async def run_stage2_hover():
    """Fetches all pending cases identified in Stage 1 and pulls their HOVER records."""
    pending_cases = get_pending_hover_cases()
    if not pending_cases:
        print("[*] No pending cases found to search in HOVER.")
        return 0

    print(f"\n{'='*60}")
    print(f"[*] STAGE 2: HOVER Court Docket & Debt Extraction")
    print(f"[*] Total Pending Cases: {len(pending_cases)}")
    print(f"{'='*60}")

    async with async_playwright() as p:
        if USE_LOCAL_BROWSER or not BRIGHT_DATA_WS_ENDPOINT or "YOUR_CUSTOMER_ID" in BRIGHT_DATA_WS_ENDPOINT:
            print("[*] Launching Local Chromium Browser for HOVER...")
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        else:
            print("[*] Connecting to Bright Data Scraping Browser...")
            browser = await p.chromium.connect_over_cdp(BRIGHT_DATA_WS_ENDPOINT)
            context = await browser.new_context(accept_downloads=True)

        page = await context.new_page()

        for case_info in pending_cases:
            await process_single_case(page, case_info)
            await page.wait_for_timeout(1500)

        await browser.close()
        print("\n[✓] Stage 2 Completed.")
        return len(pending_cases)


if __name__ == "__main__":
    asyncio.run(run_stage2_hover())
