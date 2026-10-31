import sqlite3
import os
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(__file__), "hillsborough_records.db")

def get_connection():
    return sqlite3.connect(DB_PATH)

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

    return csv_filename

