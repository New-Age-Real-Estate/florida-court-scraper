#!/usr/bin/env python3
"""SmartSkip Pipeline Bridge.

Orchestrates automated contact enrichment with 100% schema alignment:
  1. EXPORT: Queries Supabase for newly staged leads needing skip tracing
     and compiles an institutional-grade SmartSkip bulk CSV containing all
     required and supplemental columns:
     - Input Name, First Name, Last Name
     - Property Address Full, Property Address, Property City, Property State, Property Zip
     - Mailing Address Full, Mailing Address, Mailing City, Mailing State, Mailing Zip
     - NAGE Subject Ref, NAGE_REF, Case Number
  2. SKIP: Uses SmartSkipBot (Playwright headless) to upload the batch,
     order skip tracing via account credits, and download results.
  3. IMPORT: Parses enriched phone numbers and emails following NAGE rules:
     - Only 'Subject' rows enrich the owner (Relatives/Associates excluded)
     - Disconnected numbers are filtered out
     - Mobile/Landline type and formatting standardized
     - Updates `public.reports` with phone, type, email, and contact availability.

Usage:
  # Export pending leads to CSV:
  python tools/smartskip_bridge.py --export

  # Export and run headless skip tracing via SmartSkip portal:
  python tools/smartskip_bridge.py --run-skip

  # Import results CSV into database:
  python tools/smartskip_bridge.py --import-csv path/to/results.csv

  # Full automated daily cycle:
  python tools/smartskip_bridge.py --auto
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATHS = [
    Path.home() / "nage-runtime" / "env" / "document-worker.env",
    Path("/Users/Breezy/nage-runtime/env/document-worker.env"),
]

BATCH_DIR = BASE_DIR / "downloads" / "smartskip"
BATCH_DIR.mkdir(parents=True, exist_ok=True)

COUNTY_DEFAULT_CITIES = {
    "hillsborough": "TAMPA",
    "broward": "FORT LAUDERDALE",
    "orange": "ORLANDO",
    "duval": "JACKSONVILLE",
    "sarasota": "SARASOTA",
    "manatee": "BRADENTON",
}


def load_environment(env_path: Optional[Path] = None) -> Dict[str, str]:
    target_path = env_path
    if target_path is None:
        for p in DEFAULT_ENV_PATHS:
            if p.exists():
                target_path = p
                break

    env = dict(os.environ)
    if target_path and target_path.exists():
        for line in target_path.read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            try:
                env[key.strip()] = " ".join(shlex.split(value, comments=True))
            except Exception:
                env[key.strip()] = value.strip().strip("'\"")

    if "SUPABASE_URL" not in env or "SUPABASE_SERVICE_ROLE_KEY" not in env:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment.")
    return env


def parse_name(raw_name: str) -> Tuple[str, str, str]:
    """Deterministically extracts (Input Name, First Name, Last Name)."""
    if not raw_name:
        return "", "", ""
    primary = raw_name.split(";")[0].strip()
    cleaned = re.sub(
        r"\b(EST|ESTATE|TR|TRUSTEE|LLC|INC|CORP|REVOCABLE|LIVING|TRUST)\b",
        "",
        primary,
        flags=re.IGNORECASE,
    ).strip()

    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",", 1)]
        last = parts[0]
        first = parts[1].split()[0] if parts[1] else ""
        full = f"{first} {last}".strip()
        return full, first, last

    parts = cleaned.split()
    if len(parts) == 1:
        return parts[0], parts[0], ""
    elif len(parts) == 2:
        return f"{parts[1]} {parts[0]}", parts[1], parts[0]
    else:
        return f"{parts[1]} {parts[0]}", parts[1], parts[0]


def parse_address(addr_str: str, county: str = "hillsborough") -> Tuple[str, str, str, str]:
    """Splits full situs address into (Street, City, State, Zip)."""
    if not addr_str:
        return "", "", "FL", ""
    parts = [p.strip() for p in addr_str.split(",") if p.strip()]
    if len(parts) >= 3:
        street = parts[0]
        city = parts[1]
        state_zip = parts[2]
        m = re.search(r"([A-Z]{2})?\s*(\d{5})?", state_zip)
        state = m.group(1) if m and m.group(1) else "FL"
        zip_code = m.group(2) if m and m.group(2) else ""
        return street, city, state, zip_code
    elif len(parts) == 2:
        street = parts[0]
        city_zip = parts[1]
        m = re.search(r"^([A-Z\s]+?)(?:\s+(\d{5}))?$", city_zip)
        city = (
            m.group(1).strip()
            if m and m.group(1)
            else COUNTY_DEFAULT_CITIES.get(county.lower(), "TAMPA")
        )
        zip_code = m.group(2) if m and m.group(2) else ""
        return street, city, "FL", zip_code
    else:
        street = parts[0]
        city = COUNTY_DEFAULT_CITIES.get(county.lower(), "TAMPA")
        return street, city, "FL", ""


def format_phone(phone: str) -> str:
    """Formats 10-digit number into standard (XXX) XXX-XXXX format."""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone


class SmartSkipBridge:
    def __init__(self, env: Dict[str, str]):
        self.env = env
        self.base_url = env["SUPABASE_URL"].rstrip("/")
        self.headers = {
            "apikey": env["SUPABASE_SERVICE_ROLE_KEY"],
            "Authorization": f"Bearer {env['SUPABASE_SERVICE_ROLE_KEY']}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self.session = requests.Session()

    def fetch_pending_reports(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Queries reports in READY_FOR_APPROVAL that lack skip-traced contacts."""
        j_url = f"{self.base_url}/rest/v1/document_processing_jobs"
        j_params = {
            "state": "eq.READY_FOR_APPROVAL",
            "archived_at": "is.null",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        j_resp = self.session.get(j_url, headers=self.headers, params=j_params, timeout=30)
        j_resp.raise_for_status()
        jobs = j_resp.json()

        staged_reports = []
        for job in jobs:
            rid = job.get("report_id")
            if not rid:
                continue
            r_url = f"{self.base_url}/rest/v1/reports"
            r_resp = self.session.get(
                r_url,
                headers=self.headers,
                params={"id": f"eq.{rid}"},
                timeout=30,
            )
            if not r_resp.ok:
                continue
            rows = r_resp.json()
            if rows:
                rep = rows[0]
                if not rep.get("owner_phone") or not rep.get("contact_phone_available"):
                    rep["_county"] = job.get("source_county_slug", "hillsborough")
                    rep["_case_number"] = job.get("case_number", "")
                    staged_reports.append(rep)

        return staged_reports

    def export_csv(self, reports: List[Dict[str, Any]], out_path: Optional[Path] = None) -> Optional[Path]:
        """Compiles SmartSkip bulk CSV with all required standard columns."""
        if not reports:
            print("[*] No staged reports requiring skip tracing.")
            return None

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        target_path = out_path or (BATCH_DIR / f"smartskip_batch_{timestamp}.csv")

        # Full spectrum SmartSkip fields aligned with document worker schema
        fieldnames = [
            "Input Name",
            "First Name",
            "Last Name",
            "Property Address Full",
            "Property Address",
            "Property City",
            "Property State",
            "Property Zip",
            "Mailing Address Full",
            "Mailing Address",
            "Mailing City",
            "Mailing State",
            "Mailing Zip",
            "NAGE Subject Ref",
            "NAGE_REF",
            "Case Number",
        ]

        rows_written = 0
        with target_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for rep in reports:
                full_name, first, last = parse_name(rep.get("owner_name") or "")
                raw_addr = rep.get("property_address") or ""
                county = rep.get("_county", "hillsborough")
                street, city, state, zip_code = parse_address(raw_addr, county=county)

                if not first and not last:
                    continue
                if not street:
                    continue

                full_situs = f"{street}, {city}, {state} {zip_code}".strip()
                ref_id = rep["id"]

                writer.writerow({
                    "Input Name": full_name or f"{first} {last}".strip(),
                    "First Name": first,
                    "Last Name": last,
                    "Property Address Full": full_situs,
                    "Property Address": street,
                    "Property City": city,
                    "Property State": state,
                    "Property Zip": zip_code,
                    "Mailing Address Full": full_situs,
                    "Mailing Address": street,
                    "Mailing City": city,
                    "Mailing State": state,
                    "Mailing Zip": zip_code,
                    "NAGE Subject Ref": f"NAGE-{ref_id}",
                    "NAGE_REF": ref_id,
                    "Case Number": rep.get("_case_number", ""),
                })
                rows_written += 1

        print(f"[+] Exported {rows_written} leads to complete SmartSkip CSV: {target_path}")
        return target_path

    def import_results(self, results_csv_path: Path) -> int:
        """Parses SmartSkip results and attaches verified phone/email contacts."""
        if not results_csv_path.exists():
            raise FileNotFoundError(f"Results CSV not found: {results_csv_path}")

        print(f"[*] Importing SmartSkip results from: {results_csv_path.name}...")

        updated_count = 0
        with results_csv_path.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = [h.strip() for h in (reader.fieldnames or [])]

            # Detect Reference column
            ref_col = None
            for cand in ["NAGE_REF", "NAGE Subject Ref", "Custom Reference", "Reference", "ID"]:
                if cand in headers:
                    ref_col = cand
                    break

            if not ref_col:
                print(f"[!] Warning: Could not find Reference column in {headers}")
                return 0

            # Find indexed phone numbers (Phone 1 .. Phone 20)
            phone_indexes = sorted({
                int(m.group(1))
                for key in headers
                if (m := re.fullmatch(r"Phone (\d+) number", key, re.IGNORECASE))
            })

            # Find indexed emails (Email 1 .. Email 10)
            email_cols = [c for c in headers if "email" in c.lower()]

            for row in reader:
                # Rule: Only 'Subject' rows enrich the verified owner
                relationship = (row.get("Relationship") or row.get("relationship") or "Subject").strip()
                if relationship and relationship.lower() not in ("subject", ""):
                    continue  # Skip relatives and associates

                raw_ref = (row.get(ref_col) or "").strip()
                # Strip NAGE- prefix if present
                report_id = raw_ref.removeprefix("NAGE-").strip()
                if not report_id:
                    continue

                # Extract best phone: scan indexed phones for first connected number
                selected_phone = None
                selected_phone_type = "Mobile"
                for idx in phone_indexes:
                    pnum_raw = (row.get(f"Phone {idx} number") or "").strip()
                    pdigits = re.sub(r"\D", "", pnum_raw)
                    if len(pdigits) < 10:
                        continue

                    pconn = (row.get(f"Phone {idx} connected") or "").lower()
                    if pconn == "disconnected":
                        continue  # Rule: Exclude disconnected phones

                    ptype = (row.get(f"Phone {idx} type") or "Mobile").strip().capitalize()
                    selected_phone = format_phone(pdigits)
                    selected_phone_type = ptype
                    break  # Take primary verified phone

                # If no indexed phone found, fallback to generic phone column
                if not selected_phone:
                    for pcol in [c for c in headers if "phone" in c.lower()]:
                        val = (row.get(pcol) or "").strip()
                        digits = re.sub(r"\D", "", val)
                        if len(digits) >= 10:
                            selected_phone = format_phone(digits)
                            break

                # Extract email
                selected_email = None
                for ecol in email_cols:
                    val = (row.get(ecol) or "").strip()
                    if "@" in val and "." in val:
                        selected_email = val.lower()
                        break

                if not selected_phone and not selected_email:
                    continue

                # Update Supabase report
                update_payload: Dict[str, Any] = {
                    "contact_route_status": "enriched_smartskip",
                }
                if selected_phone:
                    update_payload["owner_phone"] = selected_phone
                    update_payload["owner_phone_type"] = selected_phone_type
                    update_payload["contact_phone_available"] = True
                if selected_email:
                    update_payload["owner_email"] = selected_email
                    update_payload["contact_email_available"] = True

                u_url = f"{self.base_url}/rest/v1/reports"
                resp = self.session.patch(
                    u_url,
                    headers=self.headers,
                    params={"id": f"eq.{report_id}"},
                    json=update_payload,
                    timeout=30,
                )
                if resp.ok:
                    updated_count += 1
                    print(
                        f"  [+] Enriched Report {report_id[:8]}: "
                        f"Phone={selected_phone or 'N/A'} ({selected_phone_type}), "
                        f"Email={selected_email or 'N/A'}"
                    )
                else:
                    print(f"  [!] Failed to update report {report_id}: {resp.text}")

        print(f"[+] Successfully enriched {updated_count} reports with SmartSkip contacts.")
        return updated_count


async def run_bot_skip(csv_path: Path) -> Optional[Path]:
    """Invokes SmartSkipBot to upload CSV and retrieve enriched results."""
    from smartskip_bot import SmartSkipBot, async_playwright

    bot = SmartSkipBot(headless=True)
    async with async_playwright() as p:
        await bot.init_browser(p)
        try:
            await bot.ensure_logged_in()
            nav_ok = await bot.navigate_to_bulk_upload()
            if not nav_ok:
                raise RuntimeError("Failed to navigate to bulk upload section.")

            upload_ok = await bot.upload_csv(csv_path)
            if not upload_ok:
                raise RuntimeError("CSV upload failed.")

            # Order / poll for results
            results_path = await bot.wait_for_results()
            return results_path
        finally:
            if bot.browser:
                await bot.browser.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", type=Path, help="Path to document-worker.env")
    p.add_argument("--export", action="store_true", help="Export pending reports to SmartSkip CSV")
    p.add_argument("--run-skip", action="store_true", help="Execute headless browser skip tracing on exported CSV")
    p.add_argument("--import-csv", type=Path, help="Import completed SmartSkip results CSV into Supabase")
    p.add_argument("--auto", action="store_true", help="Run full export -> skip -> import cycle")
    p.add_argument("--limit", type=int, default=50, help="Maximum number of leads to export (default 50)")

    args = p.parse_args()

    env = load_environment(args.env)
    bridge = SmartSkipBridge(env)

    if args.import_csv:
        bridge.import_results(args.import_csv)
        return

    if args.export or args.auto or args.run_skip:
        reports = bridge.fetch_pending_reports(limit=args.limit)
        csv_file = bridge.export_csv(reports)

        if not csv_file:
            print("[*] No records to process.")
            return

        if args.run_skip or args.auto:
            print("[*] Launching autonomous SmartSkip bot...")
            results_file = asyncio.run(run_bot_skip(csv_file))
            if results_file and results_file.exists():
                bridge.import_results(results_file)


if __name__ == "__main__":
    main()
