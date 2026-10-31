import os
import re
import asyncio
from typing import Optional
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import pypdf

from database import get_pending_hover_cases, update_hover_results, init_db
from storage import upload_file_to_cloud

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCKET_DIR = os.getenv("DOCKET_DIR_ORANGE", os.path.join(BASE_DIR, "downloads/orange/court_dockets"))
DEBUG_DIR = os.getenv("DEBUG_DIR_ORANGE", os.path.join(BASE_DIR, "debug_screenshots/orange"))

os.makedirs(DOCKET_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

MYECLERK_SEARCH_URL = "https://myeclerk.myorangeclerk.com/Cases/Search?caseType=CV"


def extract_debt_from_complaint_pdf(pdf_path: str) -> Optional[float]:
    """Scans the Complaint PDF for unpaid principal balance or debt amount."""
    debt_patterns = [
        r"(?:unpaid\s*principal\s*balance|principal\s*amount|indebtedness|debt\s*amount|due\s*and\s*owing)[^0-9$]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})",
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*(?:together\s*with\s*interest|in\s*principal|due\s*under\s*the\s*note)",
        r"(?:judgment\s*amount|amount\s*claimed|total\s*sum)[^0-9$]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})"
    ]
    try:
        reader = pypdf.PdfReader(pdf_path)
        for page in reader.pages[:4]:
            text = page.extract_text() or ""
            for pat in debt_patterns:
                m = re.search(pat, text, re.I)
                if m:
                    raw = m.group(1).replace(",", "")
                    val = float(raw)
                    if val > 1000:
                        return val
    except Exception as e:
        print(f"    [!] Error parsing Complaint PDF for debt: {e}")
    return None


async def process_orange_case(page, case_record: dict):
    case_no = case_record["case_number"].strip()
    inst_num = case_record["instrument_number"]

    print(f"\n[*] [Orange Court] Searching myEClerk for Case: '{case_no}' (Inst #{inst_num})...")

    try:
        await page.goto(MYECLERK_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)

        # Accept disclaimer if displayed
        disclaimer = page.locator("button#btnAccept, input[value*='Accept'], a:has-text('Accept'), button:has-text('Accept')").first
        if await disclaimer.is_visible():
            await disclaimer.click()
            await page.wait_for_timeout(1500)

        # Input case number
        case_input = page.locator("input#CaseNumber, input[name*='CaseNumber'], input#caseNumber, input[placeholder*='Case']").first
        if not await case_input.is_visible():
            # Try alternate search by case tab
            case_tab = page.locator("a:has-text('Case Number'), button:has-text('Case Number')").first
            if await case_tab.is_visible():
                await case_tab.click()
                await page.wait_for_timeout(1000)
            case_input = page.locator("input[type='text']").first

        if await case_input.is_visible():
            await case_input.fill(case_no)
            search_btn = page.locator("button[type='submit'], input[type='submit'][value*='Search'], button:has-text('Search')").first
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(4000)
        else:
            print("    [-] Case number input not found on myEClerk search page.")
            update_hover_results(inst_num, hover_status="ERROR", error_log="Search input not located")
            return

        # Check for case results
        case_link = page.locator(f"a:has-text('{case_no}'), tr:has-text('{case_no}') a, table.results a, table tbody tr a").first
        if not await case_link.is_visible():
            print(f"    [-] Case {case_no} not returned by myEClerk (may be sealed or newly filed).")
            update_hover_results(inst_num, hover_status="NOT_FOUND", error_log="Case not found in myEClerk")
            return

        print(f"    ├── Opening Case Docket Details...")
        await case_link.click()
        await page.wait_for_timeout(3500)

        # 1. Extract Claim Amount / Amount in Controversy from case details header
        page_html = await page.content()
        debt_amount: Optional[float] = None

        header_patterns = [
            r"(?:Amount\s*in\s*Controversy|Claim\s*Amount|Claimed\s*Amount|Total\s*Debt)\s*[:\$]*\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})",
            r"(?:Principal\s*Balance|Judgment\s*Amount)\s*[:\$]*\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})",
            r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*(?:Amount|Claim)"
        ]

        for reg in header_patterns:
            match = re.search(reg, page_html, re.IGNORECASE)
            if match:
                raw_amt = match.group(1).replace(",", "")
                try:
                    debt_amount = float(raw_amt)
                    print(f"    ├── Extracted Debt/Claim from Court Header: ${debt_amount:,.2f}")
                    break
                except ValueError:
                    pass

        # 2. Locate and download the 1 Key Court Docket PDF (Foreclosure Complaint)
        complaint_link = page.locator(
            "tr:has-text('COMPLAINT') a[title*='View'], tr:has-text('COMPLAINT') a:has-text('View'), tr:has-text('FORECLOSURE') a[href*='Document'], tr:has-text('COMPLAINT') a"
        ).first

        docket_download_path = None
        clean_case = case_no.replace("/", "-").replace(" ", "_")
        filename = f"Orange_Docket_{clean_case}_Complaint.pdf"
        target_path = os.path.join(DOCKET_DIR, filename)

        if await complaint_link.is_visible():
            print(f"    ├── Found Complaint docket link. Streaming vector PDF...")
            try:
                async with page.expect_download(timeout=15000) as dl_info:
                    await complaint_link.click()
                dl = await dl_info.value
                await dl.save_as(target_path)
                print(f"    ├── Downloaded Court Docket PDF: {filename} ({os.path.getsize(target_path)} bytes)")

                # If debt amount wasn't in header, parse Complaint PDF text
                if not debt_amount and os.path.exists(target_path):
                    parsed_debt = extract_debt_from_complaint_pdf(target_path)
                    if parsed_debt:
                        debt_amount = parsed_debt
                        print(f"    ├── Extracted Debt Amount from Complaint PDF text: ${debt_amount:,.2f}")

                s3_key = f"orange/dockets/{filename}"
                cloud_url = upload_file_to_cloud(target_path, s3_key)
                docket_download_path = cloud_url if cloud_url else target_path
            except Exception as dl_err:
                print(f"    [!] Could not auto-download Complaint PDF: {dl_err}")

        update_hover_results(
            instrument_number=inst_num,
            hover_status="PROCESSED" if debt_amount or docket_download_path else "PARTIAL",
            debt_amount=debt_amount,
            docket_pdf_path=docket_download_path
        )
        print(f"    [✓] Successfully updated Case {case_no}: Debt=${debt_amount or 0:,.2f}, Docket={docket_download_path is not None}")

    except Exception as e:
        print(f"    [-] Exception processing case {case_no}: {e}")
        update_hover_results(inst_num, hover_status="ERROR", error_log=str(e)[:200])


async def run_orange_stage2_myeclerk():
    """Orange County Stage 2: Court Docket & Debt Amount Extraction."""
    init_db()

    pending_cases = get_pending_hover_cases(county="orange")
    print(f"\n{'='*60}")
    print(f"[*] ORANGE COUNTY STAGE 2: Civil Court Docket & Debt Extraction")
    print(f"[*] Total Pending Cases: {len(pending_cases)}")
    print(f"{'='*60}")

    if not pending_cases:
        print("[*] No pending Orange County cases to process. All up to date.")
        return 0

    async with async_playwright() as p:
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

        processed_count = 0
        for case in pending_cases:
            await process_orange_case(page, case)
            processed_count += 1
            await page.wait_for_timeout(1000)

        await browser.close()
        print(f"\n[✓] Orange County Stage 2 Completed: {processed_count} cases processed.")
        return processed_count


if __name__ == "__main__":
    asyncio.run(run_orange_stage2_myeclerk())
