import os
import re
import asyncio
import urllib.parse
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import pypdf

from database import init_db, is_instrument_recorded, insert_or_update_lp
from storage import upload_file_to_cloud

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR_SARASOTA", os.path.join(BASE_DIR, "downloads/sarasota/lis_pendens"))
DEBUG_DIR = os.getenv("DEBUG_DIR_SARASOTA", os.path.join(BASE_DIR, "debug_screenshots/sarasota"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

SARASOTA_PORTAL_URL = "https://secure.sarasotaclerk.com"


def extract_sarasota_case_number(cells: list, page_content: str, pdf_path: str = None) -> str:
    """Extracts Florida uniform court case number (Sarasota County format)."""
    case_patterns = [
        r"\b(20[0-9]{2}-[A-Z]{2}-[0-9]{4,7})\b",
        r"\b([0-9]{4}-[A-Z]{2}-[0-9]{4,7})\b",
        r"\b([0-9]{2,4}-[A-Z]{2,4}-[0-9]{4,7})\b",
        r"\b(58[0-9]{4}[A-Z]{2}[0-9]{6}[A-Z0-9]*)\b"
    ]
    for c in cells:
        for p in case_patterns:
            m = re.search(p, c, re.I)
            if m:
                return m.group(1).upper()

    for p in case_patterns:
        m = re.search(p, page_content, re.I)
        if m:
            return m.group(1).upper()

    if pdf_path and os.path.exists(pdf_path):
        try:
            reader = pypdf.PdfReader(pdf_path)
            if reader.pages:
                text = reader.pages[0].extract_text() or ""
                for p in case_patterns:
                    m = re.search(p, text, re.I)
                    if m:
                        return m.group(1).upper()
        except Exception:
            pass

    return ""


async def run_sarasota_stage1(target_date: str = None) -> int:
    """Automates Sarasota County Lis Pendens collection."""
    init_db()

    if not target_date:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")

    print(f"\n{'='*60}")
    print(f"[*] SARASOTA COUNTY STAGE 1: Official Records Collection")
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
            print("[*] Navigating to Sarasota County Official Records portal...")
            await page.goto(SARASOTA_PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)

            # Accept disclaimer
            accept_btn = page.locator("button:has-text('Accept'), a:has-text('Accept'), input[value*='Accept'], button#btnAccept").first
            if await accept_btn.is_visible():
                print("[*] Accepting portal disclaimer...")
                await accept_btn.click()
                await page.wait_for_timeout(2000)

            # Look for Official Records Search link
            or_link = page.locator("a:has-text('Official Records'), a:has-text('Search Records'), a[href*='OR'], a[href*='search']").first
            if await or_link.is_visible():
                await or_link.click()
                await page.wait_for_timeout(2500)

            print(f"[*] Search Form URL: {page.url} | Title: '{await page.title()}'")

            # DOM injection of date range & document type
            print(f"[*] Populating date range ({target_date}) and document type (LIS PENDENS) via DOM...")
            await page.evaluate("""(tDate) => {
                const s = document.querySelector("input[name*='BeginDate'], input[name*='StartDate'], input#StartDate, input#BeginDate");
                const e = document.querySelector("input[name*='EndDate'], input#EndDate");
                const d = document.querySelector("input[name*='DocType'], input#DocType, select[name*='DocType']");
                if (s) { s.value = tDate; s.dispatchEvent(new Event('change', {bubbles: true})); }
                if (e) { e.value = tDate; e.dispatchEvent(new Event('change', {bubbles: true})); }
                if (d && d.tagName === 'INPUT') { d.value = 'LP'; d.dispatchEvent(new Event('change', {bubbles: true})); }
            }""", target_date)

            # Also try dropdown selection if DocType is a select element
            doc_select = page.locator("select[name*='DocType'], select#DocType").first
            if await doc_select.is_visible():
                try:
                    await doc_select.select_option(label="LIS PENDENS")
                except Exception:
                    try:
                        await doc_select.select_option(value="LP")
                    except Exception:
                        pass

            print("[*] Submitting search...")
            search_btn = page.locator("input[type='submit'][value*='Search'], button:has-text('Search'), input#btnSearch").first
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button, input[type=\"submit\"]')).find(b => (b.value || b.textContent || '').includes('Search'));
                    if (btn) btn.click();
                }""")

            await page.wait_for_timeout(6000)
            print(f"[*] Post-search URL: {page.url}")

            row_selector = "table#tblResults tbody tr, table.results tbody tr, table tbody tr"
            result_rows = page.locator(row_selector)

            try:
                await result_rows.first.wait_for(state="visible", timeout=20000)
            except Exception:
                page_text = await page.content()
                if "no records" in page_text.lower() or "0 results" in page_text.lower():
                    print(f"[!] No Lis Pendens filed in Sarasota County on {target_date}.")
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
                    instrument_match = re.search(r"\b(202[0-9]{7,9})\b", row_text)
                    instrument_number = instrument_match.group(1) if instrument_match else f"SARASOTA_LP_{idx}"

                    if is_instrument_recorded(instrument_number):
                        print(f"--- [{total_processed + 1}] Sarasota LP #{instrument_number}: already recorded. Skipping. ---")
                        total_processed += 1
                        continue

                    party_link = row.locator("td a").first
                    party_name = (await party_link.inner_text()).strip() if await party_link.is_visible() else f"Sarasota_Record_{idx}"

                    print(f"\n--- [{total_processed + 1}] Processing Sarasota County LP: {party_name} (#{instrument_number}) ---")

                    link_href = await party_link.get_attribute("href") if await party_link.is_visible() else None
                    doc_page = None
                    target_page = page

                    if link_href and not link_href.startswith("javascript:"):
                        full_doc_url = urllib.parse.urljoin(page.url, link_href)
                        doc_page = await context.new_page()
                        await doc_page.goto(full_doc_url, wait_until="domcontentloaded", timeout=30000)
                        await doc_page.wait_for_timeout(2000)
                        target_page = doc_page
                    else:
                        try:
                            await party_link.click(timeout=6000)
                            await page.wait_for_timeout(2000)
                        except Exception:
                            pass

                    clean_date = target_date.replace("/", "-")
                    filename = f"Sarasota_LP_{clean_date}_{instrument_number}.pdf"
                    pdf_path = os.path.join(DOWNLOAD_DIR, filename)

                    download_success = False
                    try:
                        dl_btn = target_page.locator("a[href*='download'], a[title*='Download'], button[title*='Download'], a:has-text('View Document'), a:has-text('PDF')").first
                        if await dl_btn.is_visible():
                            async with target_page.expect_download(timeout=10000) as dl_info:
                                await dl_btn.click(force=True)
                            dl = await dl_info.value
                            await dl.save_as(pdf_path)
                            download_success = True
                    except Exception:
                        pass

                    if not download_success:
                        try:
                            embed_el = target_page.locator("iframe[src*='pdf'], embed[src*='pdf'], iframe").first
                            if await embed_el.is_visible():
                                src = await embed_el.get_attribute("src")
                                if src:
                                    pdf_url = urllib.parse.urljoin(target_page.url, src)
                                    resp = await target_page.request.get(pdf_url)
                                    if resp.status == 200:
                                        with open(pdf_path, "wb") as f:
                                            f.write(await resp.body())
                                        download_success = True
                        except Exception:
                            pass

                    cloud_url = pdf_path
                    if download_success and os.path.exists(pdf_path):
                        print(f"    ├── Downloaded Vector PDF: {filename} ({os.path.getsize(pdf_path)} bytes)")
                        s3_key = f"sarasota/{clean_date}/{filename}"
                        uploaded = upload_file_to_cloud(pdf_path, s3_key)
                        if uploaded:
                            cloud_url = uploaded

                    doc_content = await target_page.content()
                    case_no = extract_sarasota_case_number(cells, doc_content, pdf_path if download_success else None)
                    print(f"    ├── Extracted Case No: '{case_no}'")

                    insert_or_update_lp(
                        instrument_number=instrument_number,
                        recording_date=target_date,
                        party_name=party_name,
                        case_number=case_no,
                        pdf_path=cloud_url if download_success else "",
                        county="sarasota"
                    )
                    total_processed += 1

                    if doc_page:
                        await doc_page.close()
                    else:
                        back_btn = page.locator("a:has-text('Back to Results'), input[value*='Back'], button:has-text('Back')").first
                        if await back_btn.is_visible():
                            await back_btn.click(force=True)
                        else:
                            await page.go_back()
                        await page.wait_for_timeout(1500)

                except Exception as row_err:
                    print(f"    [!] Error row {idx + 1}: {row_err}")
                    continue

            await browser.close()
            print(f"\n[✓] Sarasota County Stage 1 Completed: Processed {total_processed} Lis Pendens records.")
            return total_processed

        except Exception as e:
            print(f"\n[-] Sarasota County Stage 1 Exception: {e}")
            await browser.close()
            raise e


if __name__ == "__main__":
    asyncio.run(run_sarasota_stage1())
