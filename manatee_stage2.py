import os
import re
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import pypdf

from database import get_pending_hover_cases, update_hover_results, init_db
from storage import upload_file_to_cloud

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCKET_DIR = os.getenv("DOCKET_DIR_MANATEE", os.path.join(BASE_DIR, "downloads/manatee/court_dockets"))
DEBUG_DIR = os.getenv("DEBUG_DIR_MANATEE", os.path.join(BASE_DIR, "debug_screenshots/manatee"))

os.makedirs(DOCKET_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

MANATEE_COURT_SEARCH_URL = "https://records.manateeclerk.com/CourtRecords/Search/CaseNumber"
MANATEE_BASE_URL = "https://records.manateeclerk.com"


def extract_debt_from_complaint_pdf(pdf_path: str) -> Optional[float]:
    """Scans the Foreclosure Complaint PDF text for exact debt amount / unpaid principal balance."""
    debt_patterns = [
        r"(?:unpaid\s*principal\s*balance|principal\s*amount|indebtedness|debt\s*amount|due\s*and\s*owing)[^0-9$]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})",
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*(?:together\s*with\s*interest|in\s*principal|due\s*under\s*the\s*note)",
        r"(?:judgment\s*amount|amount\s*claimed|total\s*sum)[^0-9$]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})"
    ]
    try:
        reader = pypdf.PdfReader(pdf_path)
        for page in reader.pages[:5]:
            text = page.extract_text() or ""
            for pat in debt_patterns:
                m = re.search(pat, text, re.I)
                if m:
                    raw = m.group(1).replace(",", "")
                    val = float(raw)
                    if val > 1000:
                        return val
    except Exception as e:
        print(f"    [!] Error parsing Manatee Complaint PDF for debt: {e}")
    return None


async def process_manatee_case(page, context, case_record: Dict[str, Any]):
    case_no = case_record["case_number"].strip()
    inst_num = case_record["instrument_number"]

    print(f"\n[*] [Manatee Court] Querying Case: '{case_no}' (Inst #{inst_num})...")

    # Format variants: e.g. 2024CA001234 or 2024-CA-001234
    clean_case = re.sub(r"[^A-Za-z0-9]", "", case_no)
    dashed_case = re.sub(r"^([0-9]{4})([A-Za-z]{2})([0-9]+)$", r"\1-\2-\3", clean_case)

    try:
        await page.goto(MANATEE_COURT_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)

        # Accept disclaimer if visible
        disclaimer = page.locator("button#btnAccept, input[value*='Accept'], a:has-text('Accept'), button:has-text('Accept')").first
        if await disclaimer.is_visible():
            await disclaimer.click()
            await page.wait_for_timeout(1000)

        # Locate CaseNumber input
        case_input = page.locator("#CaseNumber, input[name='CaseNumber']").first
        if not await case_input.is_visible():
            print("    [-] CaseNumber input field not visible.")
            update_hover_results(inst_num, hover_status="ERROR", error_log="CaseNumber input not located")
            return

        # Try searching with clean case number
        await case_input.fill(clean_case)
        search_btn = page.locator("input[type='submit'][value*='Search'], button[type='submit']").first
        if await search_btn.is_visible():
            await search_btn.click()
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(3500)

        # Locate data rows (skip pagination/header rows)
        data_rows = await page.locator("#results-table tr.data-row").all()
        if not data_rows and dashed_case != clean_case:
            print(f"    [~] Retrying search with dashed format: '{dashed_case}'...")
            await case_input.fill(dashed_case)
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(4000)
            data_rows = await page.locator("#results-table tr.data-row").all()

        if not data_rows:
            print(f"    [-] Case {case_no} not found in Manatee Court Records.")
            update_hover_results(inst_num, hover_status="NOT_FOUND", error_log="No matching records")
            return

        # View case details (click Open Case button in first data row)
        first_row = data_rows[0]
        view_btn = first_row.locator("button[title*='Open Case'], button.btn, form[action*='Details'] button").first
        if not await view_btn.is_visible():
            print(f"    [-] Case {case_no} Open Case button not located.")
            update_hover_results(inst_num, hover_status="NOT_FOUND", error_log="Open Case button not found")
            return

        print(f"    ├── Opening Case Docket View...")
        await view_btn.click()
        await page.wait_for_timeout(4500)

        page_content = await page.content()

        # 1. Check for Claim Amount in Case Header
        debt_amount: Optional[float] = None
        header_patterns = [
            r"(?:Amount\s*in\s*Controversy|Claim\s*Amount|Damages\s*Claimed)[:\s]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})",
            r"(?:Total\s*Claim)[:\s]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})"
        ]
        for pat in header_patterns:
            m = re.search(pat, page_content, re.I)
            if m:
                raw_val = m.group(1).replace(",", "")
                debt_amount = float(raw_val)
                print(f"    ├── Claim Amount identified in Case Header: ${debt_amount:,.2f}")
                break

        # 2. Inspect #dockets table for Value Claim Form or Complaint
        docket_rows = await page.locator("#dockets tr, table.dockets tr, table tbody tr").all()
        target_docket_btn = None
        target_doc_title = "VALUE OF REAL PROPERTY OR MORTGAGE FORECLOSURE CLAIM"

        # Pass 1: Look for explicit Value of Real Property / Claim form
        for d_row in docket_rows:
            txt = (await d_row.inner_text()).upper()
            if "VALUE OF REAL PROPERTY" in txt or "MORTGAGE FORECLOSURE CLAIM" in txt or "VALUE CLAIM" in txt:
                btn = d_row.locator("button[title*='View Image'], button.btn, button[type='submit']").first
                if await btn.is_visible():
                    target_docket_btn = btn
                    target_doc_title = "VALUE OF REAL PROPERTY OR MORTGAGE FORECLOSURE CLAIM"
                    print(f"    ├── Identified Value Claim Form on docket.")
                    break

        # Pass 2: Fall back to Foreclosure Complaint if Value form not separate
        if not target_docket_btn:
            for d_row in docket_rows:
                txt = (await d_row.inner_text()).upper()
                if "COMPLAINT" in txt and ("FORECLOSURE" in txt or "MORTGAGE" in txt):
                    btn = d_row.locator("button[title*='View Image'], button.btn, button[type='submit']").first
                    if await btn.is_visible():
                        target_docket_btn = btn
                        target_doc_title = "VERIFIED COMPLAINT FOR FORECLOSURE OF MORTGAGE"
                        print(f"    ├── Identified Foreclosure Complaint on docket.")
                        break

        local_docket_path: Optional[str] = None
        if target_docket_btn:
            docket_filename = f"Manatee_Docket_{clean_case}_Value_Claim_Form.pdf"
            local_docket_path = os.path.join(DOCKET_DIR, docket_filename)

            try:
                # Click View Image button to load StandardsImageViewer
                await target_docket_btn.click()
                await page.wait_for_timeout(4500)

                img_locator = page.locator("img[src*='InstrumentResultJpg']").first
                if await img_locator.is_visible():
                    src = await img_locator.get_attribute("src") or ""
                    full_img_url = f"{MANATEE_BASE_URL}{src}" if src.startswith("/") else src
                    print(f"    ├── Downloading claim form image from {full_img_url[:65]}...")
                    resp = await page.request.get(full_img_url)
                    img_bytes = await resp.body()

                    from PIL import Image
                    import io
                    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    im.save(local_docket_path)
                    print(f"    ├── Saved converted Claim PDF: {local_docket_path} ({os.path.getsize(local_docket_path)} bytes)")
            except Exception as dl_err:
                print(f"    [!] Error acquiring claim form image: {dl_err}")

        # Drop court debt record if PDF acquired
        if local_docket_path and os.path.exists(local_docket_path) and os.path.getsize(local_docket_path) > 1000:
            try:
                from cloud_factory_intake import VerifiedCloudIntake
                c_intake = VerifiedCloudIntake()
                rec = c_intake.drop_court_debt_record(
                    county_slug="manatee",
                    raw_case_number=clean_case,
                    court_claim_pdf=Path(local_docket_path),
                    source_url=f"{MANATEE_BASE_URL}/CourtRecords/Case/Details",
                    document_title=target_doc_title,
                    instrument_number=inst_num,
                    status="HELD",
                )
                print(f"    ├── Dropped court debt record: {rec.get('id')} status={rec.get('body', {}).get('status')}")
            except Exception as drop_err:
                print(f"    [!] Error dropping court debt record: {drop_err}")

            if not debt_amount:
                debt_amount = extract_debt_from_complaint_pdf(local_docket_path)
                if debt_amount:
                    print(f"    ├── Extracted Debt Amount from text: ${debt_amount:,.2f}")

        # Upload Complaint Docket vector PDF to S3/Storage
        docket_pdf_s3_url = ""
        if local_docket_path and os.path.exists(local_docket_path):
            s3_docket_key = f"manatee/dockets/Manatee_Docket_{clean_case}_Value_Claim_Form.pdf"
            docket_pdf_s3_url = upload_file_to_cloud(local_docket_path, s3_docket_key) or local_docket_path

        update_hover_results(
            instrument_number=inst_num,
            hover_status="SUCCESS" if local_docket_path else "NO_FORM",
            debt_amount=debt_amount,
            docket_pdf_path=local_docket_path or docket_pdf_s3_url
        )

    except Exception as e:
        print(f"[-] Error processing Manatee case {case_no}: {e}")
        update_hover_results(inst_num, hover_status="ERROR", error_log=str(e))


async def run_manatee_stage2():
    """Extracts court docket PDFs and debt amounts for pending Manatee County cases."""
    init_db()
    pending = get_pending_hover_cases(county="manatee")
    if not pending:
        print("[*] No pending Manatee cases requiring court docket extraction.")
        return

    print(f"\n{'='*60}")
    print(f"[*] MANATEE COUNTY STAGE 2: Court Docket & Debt Extraction")
    print(f"[*] Total Pending Cases: {len(pending)}")
    print(f"{'='*60}")

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

        try:
            for case_rec in pending:
                await process_manatee_case(page, context, case_rec)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run_manatee_stage2())
