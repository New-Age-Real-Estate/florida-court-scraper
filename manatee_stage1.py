import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext
import pypdf

from database import init_db, is_instrument_recorded, insert_or_update_lp, export_county_daily_csv
from storage import upload_file_to_cloud

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR_MANATEE", os.path.join(BASE_DIR, "downloads/manatee/lis_pendens"))
DEBUG_DIR = os.getenv("DEBUG_DIR_MANATEE", os.path.join(BASE_DIR, "debug_screenshots/manatee"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)

MANATEE_OR_SEARCH_URL = "https://records.manateeclerk.com/OfficialRecords/Search/InstrumentType"
MANATEE_BASE_URL = "https://records.manateeclerk.com"


def extract_manatee_case_number(cells: List[str], page_content: str, pdf_path: Optional[str] = None) -> str:
    """
    Extracts Florida uniform court case number (Manatee County format: County #41, 12th Circuit).
    Formats:
      - 2024-CA-001234 / 2024CA001234
      - 2024-CC-001234 / 2024CC001234
      - 41-2024-CA-001234-CAAX-XX / 412024CA001234
    """
    case_patterns = [
        r"\b(41[0-9]{4}[A-Z]{2}[0-9]{6}[A-Z0-9]*)\b",
        r"\b(41-[0-9]{4}-[A-Z]{2}-[0-9]{4,7}(?:-[A-Z0-9]+)*)\b",
        r"\b(20[0-9]{2}-[A-Z]{2}-[0-9]{4,7})\b",
        r"\b(20[0-9]{2}[A-Z]{2}[0-9]{4,7})\b",
        r"\b(20[0-9]{2}\s+[A-Z]{2}\s+[0-9]{4,7})\b",
        r"\b([0-9]{4}-[A-Z]{2}-[0-9]{4,7})\b"
    ]

    for c in cells:
        for p in case_patterns:
            m = re.search(p, c, re.I)
            if m:
                clean = re.sub(r"\s+", "", m.group(1)).upper()
                return clean

    for p in case_patterns:
        m = re.search(p, page_content, re.I)
        if m:
            clean = re.sub(r"\s+", "", m.group(1)).upper()
            return clean

    if pdf_path and os.path.exists(pdf_path):
        try:
            reader = pypdf.PdfReader(pdf_path)
            for page in reader.pages[:2]:
                text = page.extract_text() or ""
                for p in case_patterns:
                    m = re.search(p, text, re.I)
                    if m:
                        clean = re.sub(r"\s+", "", m.group(1)).upper()
                        return clean
        except Exception:
            pass

    return ""


async def run_manatee_stage1(target_date: Optional[str] = None) -> int:
    """
    Automates Manatee County Lis Pendens collection from records.manateeclerk.com.
    target_date format: MM/DD/YYYY
    """
    init_db()

    if not target_date:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")

    clean_date = target_date.replace("/", "-")
    dt = datetime.strptime(target_date, "%m/%d/%Y")
    iso_date = dt.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"[*] MANATEE COUNTY STAGE 1: Official Records Collection")
    print(f"[*] Target Recording Date: {target_date} (ISO: {iso_date})")
    print(f"[*] Search URL: {MANATEE_OR_SEARCH_URL}")
    print(f"{'='*60}")

    downloaded_count = 0

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
            print(f"[*] Navigating to Manatee Official Records search form...")
            await page.goto(MANATEE_OR_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)

            # Accept any modal disclaimer if present
            disclaimer_btn = page.locator("button:has-text('Accept'), a:has-text('Accept'), input[value*='Accept'], button#btnAccept, button:has-text('I Agree')").first
            if await disclaimer_btn.is_visible():
                print("[*] Accepting portal disclaimer...")
                await disclaimer_btn.click()
                await page.wait_for_timeout(2000)

            # Select Instrument Type: 17 = LIS PENDENS
            print("[*] Selecting Document Type: 'LIS PENDENS' (option value 17)...")
            type_select = page.locator("#SearchInputs_InstrumentTypeId, select[name*='InstrumentTypeId']").first
            if await type_select.is_visible():
                try:
                    await type_select.select_option(value="17")
                    print("[+] Selected option value 17 (LIS PENDENS).")
                except Exception:
                    await type_select.select_option(label="LIS PENDENS")
                    print("[+] Selected option by label 'LIS PENDENS'.")
            else:
                print("[!] Instrument Type select element not found by ID; searching via select tags...")
                all_selects = await page.locator("select").all()
                for sel in all_selects:
                    opts = await sel.inner_text()
                    if "LIS PENDENS" in opts:
                        await sel.select_option(value="17")
                        break

            # Fill Start Date and End Date (Format: YYYY-MM-DD)
            print(f"[*] Setting date range: {iso_date} to {iso_date}...")
            start_input = page.locator("#SearchInputs_StartDate, input[name*='StartDate']").first
            end_input = page.locator("#SearchInputs_EndDate, input[name*='EndDate']").first

            if await start_input.is_visible() and await end_input.is_visible():
                await page.evaluate(f"""() => {{
                    const s = document.querySelector('#SearchInputs_StartDate') || document.querySelector('input[name*=\"StartDate\"]');
                    const e = document.querySelector('#SearchInputs_EndDate') || document.querySelector('input[name*=\"EndDate\"]');
                    if (s) {{ s.value = '{iso_date}'; s.dispatchEvent(new Event('input', {{bubbles:true}})); s.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                    if (e) {{ e.value = '{iso_date}'; e.dispatchEvent(new Event('input', {{bubbles:true}})); e.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                }}""")
                print(f"[+] Entered date range: {iso_date}")
            else:
                print("[-] Could not find StartDate or EndDate input fields.")

            # Submit Search Form
            print("[*] Submitting search...")
            search_btn = page.locator("input[type='submit'][value*='Search'], input.btn-onload, button[type='submit']").first
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await page.evaluate("""() => {
                    const f = document.querySelector('form[action*=\"DoSearch\"]') || document.querySelector('form');
                    if (f) f.submit();
                }""")

            await page.wait_for_timeout(4000)
            print(f"[*] Results page loaded: {page.url} | Title: '{await page.title()}'")

            # Check for results table
            results_table = page.locator("#results-table, table.display, table.table, table.table-primary").first
            if not await results_table.is_visible():
                print("[*] Checking page for table or no-results message...")
                body_text = await page.inner_text("body")
                if "No records" in body_text or "0 records" in body_text or "no results" in body_text.lower():
                    print(f"[*] Zero Lis Pendens filings recorded in Manatee County on {target_date}.")
                    return 0

            # Find all result rows
            rows = await page.locator("table tbody tr, #results-table tbody tr").all()
            print(f"[*] Found {len(rows)} potential result rows in search table.")

            if not rows:
                print(f"[*] No result rows found for {target_date}.")
                return 0

            # Process each filing row
            for idx, row in enumerate(rows):
                try:
                    cells = [ (await c.inner_text()).strip() for c in await row.locator("td").all() ]
                    if not cells or len(cells) < 3:
                        continue

                    row_text = " | ".join(cells)

                    # Extract Instrument Number (numeric string of 8-12 digits)
                    inst_match = re.search(r"\b(20[0-9]{8,10})\b", row_text) or re.search(r"\b([0-9]{7,12})\b", row_text)
                    inst_num = inst_match.group(1) if inst_match else f"MANATEE_{clean_date}_{idx+1}"

                    # Extract parties
                    party_name = cells[3] if len(cells) > 3 else (cells[2] if len(cells) > 2 else "UNKNOWN")

                    # Check if already processed
                    if is_instrument_recorded(inst_num):
                        print(f"    [~] Instrument {inst_num} already recorded in DB. Skipping.")
                        continue

                    print(f"\n  [{idx+1}/{len(rows)}] Processing Manatee Instrument #{inst_num}...")
                    print(f"    ├── Parties: {party_name}")

                    # Find view / download link
                    view_link = row.locator("a[href*='DisplayInstrument'], a[href*='Document'], a:has-text('View'), a").first
                    view_href = await view_link.get_attribute("href") if await view_link.is_visible() else ""

                    pdf_filename = f"Manatee_LP_{clean_date}_{inst_num}.pdf"
                    local_pdf_path = os.path.join(DOWNLOAD_DIR, pdf_filename)

                    download_success = False

                    if view_href:
                        full_view_url = view_href if view_href.startswith("http") else f"{MANATEE_BASE_URL}{view_href}"
                        print(f"    ├── Opening document viewer: {full_view_url}")

                        doc_page = await context.new_page()
                        try:
                            # Listen for direct PDF download event
                            async with doc_page.expect_download(timeout=10000) as download_info:
                                await doc_page.goto(full_view_url, wait_until="domcontentloaded", timeout=30000)
                            download = await download_info.value
                            await download.save_as(local_pdf_path)
                            download_success = True
                            print(f"    ├── Downloaded vector PDF directly via browser download event.")
                        except Exception:
                            # Check if the page itself redirected to PDF or has an iframe/embed
                            await doc_page.wait_for_timeout(3000)
                            doc_url = doc_page.url
                            if doc_url.lower().endswith(".pdf") or "application/pdf" in (doc_page.url):
                                print(f"    ├── Direct PDF URL detected: {doc_url}")
                                pdf_data = await doc_page.evaluate("""async (url) => {
                                    const resp = await fetch(url);
                                    const buf = await resp.arrayBuffer();
                                    return Array.from(new Uint8Array(buf));
                                }""", doc_url)
                                with open(local_pdf_path, "wb") as f:
                                    f.write(bytes(pdf_data))
                                download_success = True
                            else:
                                # Look for PDF embed or download button
                                download_btn = doc_page.locator("a[href*='.pdf'], a:has-text('Download'), button:has-text('Download'), a[title*='Download']").first
                                if await download_btn.is_visible():
                                    async with doc_page.expect_download(timeout=10000) as dl_info:
                                        await download_btn.click()
                                    dl = await dl_info.value
                                    await dl.save_as(local_pdf_path)
                                    download_success = True
                                else:
                                    # Fallback: check for iframe embedding PDF
                                    iframe_src = await doc_page.evaluate("""() => {
                                        const f = document.querySelector('iframe[src*=\"pdf\"], embed[src*=\"pdf\"], object[data*=\"pdf\"]');
                                        return f ? (f.src || f.data) : null;
                                    }""")
                                    if iframe_src:
                                        iframe_full = iframe_src if iframe_src.startswith("http") else f"{MANATEE_BASE_URL}{iframe_src}"
                                        print(f"    ├── Extracted PDF iframe src: {iframe_full}")
                                        pdf_data = await doc_page.evaluate("""async (url) => {
                                            const resp = await fetch(url);
                                            const buf = await resp.arrayBuffer();
                                            return Array.from(new Uint8Array(buf));
                                        }""", iframe_full)
                                        with open(local_pdf_path, "wb") as f:
                                            f.write(bytes(pdf_data))
                                        download_success = True
                        finally:
                            await doc_page.close()

                    # Extract case number from row or downloaded PDF text
                    case_no = extract_manatee_case_number(cells, row_text, local_pdf_path if download_success else None)
                    print(f"    ├── Case Number: '{case_no or 'PENDING_EXTRACTION'}'")

                    # Upload to S3
                    s3_url = ""
                    if download_success and os.path.exists(local_pdf_path) and os.path.getsize(local_pdf_path) > 1000:
                        s3_key = f"manatee/{clean_date}/{pdf_filename}"
                        s3_url = upload_file_to_cloud(local_pdf_path, s3_key)
                        downloaded_count += 1
                        print(f"    └── Verified Vector PDF uploaded to S3: {s3_url}")
                    else:
                        s3_url = f"MANATEE_RECORD_{inst_num}"

                    # Insert record into SQLite DB
                    insert_or_update_lp(
                        instrument_number=inst_num,
                        recording_date=target_date,
                        party_name=party_name,
                        case_number=case_no,
                        pdf_path=s3_url,
                        county="manatee"
                    )

                except Exception as row_err:
                    print(f"    [-] Error processing row {idx}: {row_err}")

            print(f"\n[+] Manatee Stage 1 Complete: {downloaded_count} Lis Pendens vector PDFs collected.")
            return downloaded_count

        except Exception as e:
            print(f"[-] Fatal error in Manatee Stage 1: {e}")
            screenshot_path = os.path.join(DEBUG_DIR, f"manatee_error_{clean_date}.png")
            try:
                await page.screenshot(path=screenshot_path)
                upload_file_to_cloud(screenshot_path, f"manatee/debug/{os.path.basename(screenshot_path)}")
            except Exception:
                pass
            return 0
        finally:
            await browser.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    count = asyncio.run(run_manatee_stage1(target))
    print(f"Downloaded: {count}")
