import os
import re
import asyncio
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

        # If 0 results, retry with dashed case format
        results_rows = await page.locator("#results-table tbody tr").all()
        if not results_rows and dashed_case != clean_case:
            print(f"    [~] Retrying search with dashed format: '{dashed_case}'...")
            await case_input.fill(dashed_case)
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(3500)
            results_rows = await page.locator("#results-table tbody tr").all()

        if not results_rows:
            print(f"    [-] Case {case_no} not found in Manatee Court Records (may be sealed or pending clerk entry).")
            update_hover_results(inst_num, hover_status="NOT_FOUND", error_log="Case not returned in court search")
            return

        # View case details (first result row View link)
        first_row = results_rows[0]
        view_link = first_row.locator("a:has-text('View'), a[href*='Details'], a[href*='Case']").first
        if not await view_link.is_visible():
            view_link = first_row.locator("a").first

        print(f"    ├── Opening Case Docket View...")
        await view_link.click()
        await page.wait_for_timeout(4000)

        page_content = await page.content()

        # 1. Check for Claim Amount in header
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

        # 2. Inspect Court Dockets for Complaint PDF
        docket_rows = await page.locator("table.dockets tbody tr, table.docket tbody tr, table tbody tr").all()
        complaint_link = None

        for d_row in docket_rows:
            txt = (await d_row.inner_text()).lower()
            if "complaint" in txt or "foreclosure" in txt or "mortgage" in txt:
                link = d_row.locator("a[href*='.pdf'], a[href*='Document'], a:has-text('View'), a").first
                if await link.is_visible():
                    complaint_link = link
                    print(f"    ├── Identified Foreclosure Complaint Docket entry.")
                    break

        docket_pdf_s3_url = ""
        if complaint_link:
            docket_filename = f"Manatee_Docket_{clean_case}_Complaint.pdf"
            local_docket_path = os.path.join(DOCKET_DIR, docket_filename)
            view_href = await complaint_link.get_attribute("href") or ""

            if view_href:
                full_view = view_href if view_href.startswith("http") else f"{MANATEE_BASE_URL}{view_href}"
                doc_page = await context.new_page()
                try:
                    async with doc_page.expect_download(timeout=8000) as dl_info:
                        await doc_page.goto(full_view, wait_until="domcontentloaded", timeout=25000)
                    dl = await dl_info.value
                    await dl.save_as(local_docket_path)
                except Exception:
                    # Check if iframe embeds PDF or direct stream
                    await doc_page.wait_for_timeout(2000)
                    embed_src = await doc_page.evaluate("""() => {
                        const f = document.querySelector('iframe[src*=\"pdf\"], embed[src*=\"pdf\"], object[data*=\"pdf\"]');
                        return f ? (f.src || f.data) : null;
                    }""")
                    if embed_src:
                        full_embed = embed_src if embed_src.startswith("http") else f"{MANATEE_BASE_URL}{embed_src}"
                        pdf_data = await doc_page.evaluate("""async (url) => {
                            const resp = await fetch(url);
                            const buf = await resp.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        }""", full_embed)
                        with open(local_docket_path, "wb") as f:
                            f.write(bytes(pdf_data))
                finally:
                    await doc_page.close()

                if os.path.exists(local_docket_path) and os.path.getsize(local_docket_path) > 1000:
                    if not debt_amount:
                        debt_amount = extract_debt_from_complaint_pdf(local_docket_path)
                        if debt_amount:
                            print(f"    ├── Extracted Debt Amount from Complaint text: ${debt_amount:,.2f}")

                    # Upload Complaint Docket vector PDF to S3
                    s3_docket_key = f"manatee/dockets/{docket_filename}"
                    docket_pdf_s3_url = upload_file_to_cloud(local_docket_path, s3_docket_key) or ""
                    print(f"    └── Complaint Docket Vector PDF uploaded to S3: {docket_pdf_s3_url}")

        if debt_amount:
            print(f"[+] SUCCESS: Debt Amount for Case {case_no}: ${debt_amount:,.2f}")
            update_hover_results(
                instrument_number=inst_num,
                hover_status="SUCCESS",
                debt_amount=debt_amount,
                docket_pdf_path=docket_pdf_s3_url
            )
        else:
            print(f"[~] Case {case_no} reviewed; no debt figure extracted.")
            update_hover_results(
                instrument_number=inst_num,
                hover_status="NO_DEBT_FOUND",
                docket_pdf_path=docket_pdf_s3_url
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
