import sqlite3
import os
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(__file__), "hillsborough_records.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=45.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    """Initializes the SQLite schema."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lis_pendens (
                instrument_number TEXT PRIMARY KEY,
                recording_date TEXT,
                party_name TEXT,
                case_number TEXT,
                pdf_path TEXT,
                debt_amount REAL,
                hover_status TEXT DEFAULT 'PENDING',
                docket_pdf_path TEXT,
                error_log TEXT,
                county TEXT DEFAULT 'hillsborough',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cur.execute("ALTER TABLE lis_pendens ADD COLUMN county TEXT DEFAULT 'hillsborough'")
        except Exception:
            pass
        conn.commit()

def is_instrument_recorded(instrument_number: str) -> bool:
    """Checks if a document has already been downloaded and verified."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pdf_path FROM lis_pendens WHERE instrument_number = ?", (instrument_number,))
        row = cur.fetchone()
        if not row:
            return False
        pdf_path = row[0]
        if pdf_path and (pdf_path.startswith("http://") or pdf_path.startswith("https://") or pdf_path.startswith("s3://") or os.path.isfile(pdf_path)):
            return True
        return False

def insert_or_update_lp(
    instrument_number: str,
    recording_date: str,
    party_name: str,
    case_number: str,
    pdf_path: str,
    county: str = "hillsborough"
):
    """Inserts or updates an extracted Lis Pendens record."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO lis_pendens (
                instrument_number, recording_date, party_name, case_number, pdf_path, county, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(instrument_number) DO UPDATE SET
                case_number = excluded.case_number,
                pdf_path = excluded.pdf_path,
                county = excluded.county,
                updated_at = CURRENT_TIMESTAMP
        """, (instrument_number, recording_date, party_name, case_number, pdf_path, county))
        conn.commit()

def get_pending_hover_cases(county: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all records that have a case number but haven't had their docket/debt pulled yet."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if county:
            county_clean = county.lower().strip()
            cur.execute("""
                SELECT instrument_number, recording_date, party_name, case_number, pdf_path, county
                FROM lis_pendens 
                WHERE case_number IS NOT NULL 
                  AND trim(case_number) != '' 
                  AND hover_status = 'PENDING'
                  AND (county = ? OR (? = 'hillsborough' AND county IS NULL))
            """, (county_clean, county_clean))
        else:
            cur.execute("""
                SELECT instrument_number, recording_date, party_name, case_number, pdf_path, county
                FROM lis_pendens 
                WHERE case_number IS NOT NULL 
                  AND trim(case_number) != '' 
                  AND hover_status = 'PENDING'
            """)
        return [dict(row) for row in cur.fetchall()]

def update_hover_results(
    instrument_number: str,
    hover_status: str,
    debt_amount: Optional[float] = None,
    docket_pdf_path: Optional[str] = None,
    error_log: Optional[str] = None
):
    """Updates the record with HOVER debt amount and docket info."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE lis_pendens
            SET hover_status = ?,
                debt_amount = COALESCE(?, debt_amount),
                docket_pdf_path = COALESCE(?, docket_pdf_path),
                error_log = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE instrument_number = ?
        """, (hover_status, debt_amount, docket_pdf_path, error_log, instrument_number))
        conn.commit()

def get_daily_summary(target_date: str) -> Dict[str, Any]:
    """Generates a summary of collected records for a given date."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM lis_pendens WHERE recording_date = ?", (target_date,))
        total_lps = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM lis_pendens WHERE recording_date = ? AND case_number != ''", (target_date,))
        cases_identified = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*), SUM(debt_amount) FROM lis_pendens WHERE recording_date = ? AND debt_amount IS NOT NULL", (target_date,))
        row = cur.fetchone()
        cases_with_debt = row[0]
        total_debt_value = row[1] or 0.0

        return {
            "target_date": target_date,
            "total_lps_collected": total_lps,
            "cases_identified": cases_identified,
            "cases_with_debt": cases_with_debt,
            "total_debt_value": total_debt_value
        }

def export_county_daily_csv(county: str, target_date: str) -> str:
    """Exports all records for a specific county and date to CSV and uploads to S3."""
    import csv
    from storage import upload_file_to_cloud

    county_clean = county.lower().strip()
    clean_date = target_date.replace("/", "-")
    csv_filename = f"{county_clean}_report_{clean_date}.csv"
    base_dir = os.path.dirname(__file__)
    csv_path = os.path.join(base_dir, csv_filename)

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT instrument_number, recording_date, party_name, case_number, pdf_path, debt_amount
            FROM lis_pendens
            WHERE recording_date = ? AND (county = ? OR (county IS NULL AND ? = 'hillsborough') OR pdf_path LIKE '%' || ? || '%')
            ORDER BY created_at DESC
        """, (target_date, county_clean, county_clean, county_clean))
        rows = [dict(r) for r in cur.fetchall()]

    fieldnames = [
        "instrument_number",
        "recording_date",
        "county",
        "party_name",
        "case_number",
        "pdf_path",
        "debt_amount"
    ]

    with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "instrument_number": r.get("instrument_number", ""),
                "recording_date": r.get("recording_date", ""),
                "county": county_clean.title(),
                "party_name": r.get("party_name", ""),
                "case_number": r.get("case_number", ""),
                "pdf_path": r.get("pdf_path", ""),
                "debt_amount": f"${r.get('debt_amount', 0):,.2f}" if r.get("debt_amount") else ""
            })

    # Upload to S3 under county partition
    s3_key_date = f"{county_clean}/{clean_date}/{csv_filename}"
    s3_key_reports = f"{county_clean}/reports/{csv_filename}"
    upload_file_to_cloud(csv_path, s3_key_date)
    upload_file_to_cloud(csv_path, s3_key_reports)
    print(f"[*] Exported {len(rows)} {county_clean.title()} records to CSV: {csv_filename} and uploaded to S3.")

    # Generate canonical NAGE S3 Manifest (LP/<County>/<Date>/ and COURT-DEBT-RECORDS)
    export_county_nage_s3_manifest(county, target_date, rows)

    return csv_filename


def export_county_nage_s3_manifest(county: str, target_date: str, rows: Optional[List[Dict[str, Any]]] = None):
    """
    Exports daily Lis Pendens & Court Docket records directly to AWS S3
    matching the canonical NAGE cloud lake layout:
      - s3://<bucket>/LP/<County>/<Date>/<Notice>.pdf
      - s3://<bucket>/LP/<County>/<Date>/<Companion>.pdf
      - s3://<bucket>/LP/<County>/<Date>/index.csv
      - s3://<bucket>/COURT-DEBT-RECORDS/<county>/<CaseNumber>/court-source.pdf
      - s3://<bucket>/COURT-DEBT-RECORDS/<county>/<CaseNumber>/case.receipt.json
    """
    import csv
    import datetime
    import hashlib
    import json
    import re
    from storage import upload_file_to_cloud

    county_clean = county.lower().strip()
    county_title = county_clean.title()
    county_codes = {
        "hillsborough": ("29", "hover.hillsclerk.com"),
        "orange": ("48", "myeclerk.myorangeclerk.com"),
        "manatee": ("41", "records.manateeclerk.com"),
        "sarasota": ("58", "clerknet.sarasotaclerk.com"),
        "duval": ("16", "core.duvalclerk.com"),
        "broward": ("06", "eclerk.browardclerk.org")
    }
    code, domain = county_codes.get(county_clean, ("", "court.fl.gov"))

    # Convert MM/DD/YYYY to YYYY-MM-DD
    iso_date = target_date
    if "/" in target_date:
        parts = target_date.split("/")
        if len(parts) == 3:
            iso_date = f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"

    if rows is None:
        with get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT instrument_number, recording_date, party_name, case_number, pdf_path, docket_pdf_path, debt_amount
                FROM lis_pendens
                WHERE recording_date = ? AND (county = ? OR (county IS NULL AND ? = 'hillsborough') OR pdf_path LIKE '%' || ? || '%')
                ORDER BY created_at DESC
            """, (target_date, county_clean, county_clean, county_clean))
            rows = [dict(r) for r in cur.fetchall()]

    base_dir = os.path.dirname(__file__)
    index_filename = f"index_{county_clean}_{iso_date}.csv"
    index_local_path = os.path.join(base_dir, index_filename)

    index_rows = []

    for r in rows:
        inst = r.get("instrument_number", "").strip()
        raw_case = r.get("case_number", "").strip()
        party = r.get("party_name", "UNKNOWN").strip()
        pdf_path = r.get("pdf_path", "")
        docket_pdf_path = r.get("docket_pdf_path", "")

        # Normalize UCN
        raw_clean = raw_case.replace(" ", "").upper()
        m = re.search(r'(?:(\d{2})[-_]?)?((?:19|20)?\d{2})[-_]?([A-Z]{2})[-_]?0*(\d{4,8})', raw_clean)
        if m:
            c_prefix, yr, kind, num = m.groups()
            prefix = c_prefix or code
            if len(yr) == 2: yr = "20" + yr
            ucn = f"{prefix}-{yr}-{kind}-{int(num):06d}"
        else:
            ucn = raw_clean

        safe_party = re.sub(r'[^A-Za-z0-9 ]', '', party).strip()[:40]

        if ucn:
            lp_s3_name = f"{ucn} - LIS PENDENS - {safe_party} [{inst}].pdf"
        else:
            lp_s3_name = f"{inst} - LIS PENDENS - {safe_party}.pdf"

        # 1. Upload LP Notice directly to S3 canonical path
        s3_lp_key = f"LP/{county_title}/{iso_date}/{lp_s3_name}"
        if pdf_path and os.path.exists(pdf_path):
            upload_file_to_cloud(pdf_path, s3_lp_key)

        # 2. Upload Court Debt Companion & Case Receipt directly to S3
        if docket_pdf_path and os.path.exists(docket_pdf_path):
            if ucn:
                dock_s3_name = f"{ucn} - COURT DEBT - Complaint.pdf"
            else:
                dock_s3_name = f"{inst} - COURT DEBT - Complaint.pdf"

            s3_dock_key = f"LP/{county_title}/{iso_date}/{dock_s3_name}"
            upload_file_to_cloud(docket_pdf_path, s3_dock_key)

            case_slug = ucn.replace("-", "") if ucn else f"INST{inst}"
            s3_court_pdf_key = f"COURT-DEBT-RECORDS/{county_clean}/{case_slug}/court-source.pdf"
            s3_receipt_key = f"COURT-DEBT-RECORDS/{county_clean}/{case_slug}/case.receipt.json"

            upload_file_to_cloud(docket_pdf_path, s3_court_pdf_key)

            receipt_data = {
                "county": county_clean,
                "court_case_number": case_slug,
                "lp_instrument_numbers": [str(inst)],
                "source_url": f"https://{domain}/",
                "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "checked_by": "Automated Scraper (Stage 2)",
                "document_title": "FORECLOSURE COMPLAINT / VALUE CLAIM FORM",
                "filed_date": iso_date,
                "case_binding": "ATTENDED_COURT_CASE",
                "status": "HELD",
                "pdf": "court-source.pdf"
            }
            tmp_receipt_path = os.path.join(base_dir, f"receipt_{case_slug}.json")
            with open(tmp_receipt_path, "w") as f_rec:
                json.dump(receipt_data, f_rec, indent=2)
            upload_file_to_cloud(tmp_receipt_path, s3_receipt_key)
            try:
                os.remove(tmp_receipt_path)
            except OSError:
                pass

        index_rows.append({
            "instrument": inst,
            "recorded": r.get("recording_date", ""),
            "uuid": hashlib.sha256(inst.encode()).hexdigest().upper(),
            "pages": 1,
            "plaintiff": "",
            "defendants": party,
            "legal": "",
            "case_number": ucn,
            "pdf": s3_lp_key,
            "pdf_status": "NEW"
        })

    # Write canonical index.csv and upload to S3
    if index_rows:
        fieldnames = ["instrument", "recorded", "uuid", "pages", "plaintiff", "defendants", "legal", "case_number", "pdf", "pdf_status"]
        with open(index_local_path, "w", newline="", encoding="utf-8") as f_idx:
            writer = csv.DictWriter(f_idx, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(index_rows)

        s3_index_key = f"LP/{county_title}/{iso_date}/index.csv"
        upload_file_to_cloud(index_local_path, s3_index_key)
        try:
            os.remove(index_local_path)
        except OSError:
            pass
        print(f"[+] Synced canonical NAGE S3 index: s3://.../{s3_index_key}")


