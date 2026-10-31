import os
import re
import urllib.parse
from typing import Tuple, Optional
from playwright.async_api import Page


def extract_orange_instrument_number(cells: list, page_content: str, fallback_idx: int) -> str:
    """Extracts the 8 to 12 digit official recording instrument number."""
    for c in cells:
        c_clean = c.strip()
        if re.match(r"^202[0-9]{7,9}$", c_clean):
            return c_clean

    m_inst = re.search(r"Doc(?:ument)?\s*(?:#|Number|No\.?)?\s*[:\s]*([0-9]{8,12})", page_content, re.I)
    if m_inst:
        return m_inst.group(1).strip()

    return f"ORANGE_LP_{fallback_idx}"


def extract_orange_case_number(cells: list, page_content: str) -> str:
    """Extracts Florida uniform case number from cells or page details."""
    case_patterns = [
        r"\b(20[0-9]{2}-[A-Z]{2}-[0-9]{4,7}(?:-O)?)\b",
        r"\b([0-9]{2,4}-[A-Z]{2,4}-[0-9]{4,7})\b",
        r"\b(48[0-9]{4}[A-Z]{2}[0-9]{6}[A-Z0-9]*)\b"
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

    return ""


async def download_orange_vector_pdf(page: Page, pdf_path: str) -> bool:
    """Downloads the official full-resolution vector PDF via direct link or stream."""
    try:
        dl_link = page.locator("a[href*='printDoc'], a[href*='download'], a[title*='Download'], button[title*='Download'], a:has-text('View Document'), a:has-text('Download Image')").first
        if await dl_link.is_visible():
            async with page.expect_download(timeout=12000) as dl_info:
                await dl_link.click()
            dl = await dl_info.value
            await dl.save_as(pdf_path)
            return True
    except Exception:
        pass

    try:
        pdf_iframe = page.locator("iframe[src*='pdf'], embed[src*='pdf'], iframe#docDisplay, iframe").first
        if await pdf_iframe.is_visible():
            src = await pdf_iframe.get_attribute("src")
            if src:
                full_src = urllib.parse.urljoin(page.url, src)
                parsed = urllib.parse.urlparse(full_src)
                qs = urllib.parse.parse_qs(parsed.query)
                pdf_url = urllib.parse.urljoin(full_src, qs["file"][0]) if "file" in qs else full_src
                if pdf_url.startswith("http"):
                    resp = await page.request.get(pdf_url)
                    if resp.status == 200:
                        with open(pdf_path, "wb") as f:
                            f.write(await resp.body())
                        return True
    except Exception:
        pass

    return False
