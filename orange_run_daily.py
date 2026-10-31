import os
import csv
import sqlite3
import argparse
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

from orange_stage1 import run_orange_stage1
from storage import upload_file_to_cloud, notify_downstream_machine
from database import get_connection, init_db

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def calculate_target_date(manual_date: str = None, force_today: bool = False) -> str:
    """Calculates the target recording date."""
    if manual_date:
        return manual_date

    now = datetime.now()
    if force_today:
        return now.strftime("%m/%d/%Y")

    weekday = now.weekday()
    if weekday == 0:  # Monday -> Friday
        target = now - timedelta(days=3)
    elif weekday == 6:  # Sunday -> Friday
        target = now - timedelta(days=2)
    else:  # Tuesday-Saturday -> Yesterday
        target = now - timedelta(days=1)

    return target.strftime("%m/%d/%Y")


def export_orange_daily_csv(target_date: str) -> str:
    """Exports all Orange County records for the given date to a CSV file."""
    clean_date = target_date.replace("/", "-")
    csv_filename = f"orange_report_{clean_date}.csv"
    csv_path = os.path.join(BASE_DIR, csv_filename)

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT instrument_number, recording_date, party_name, case_number, pdf_path, debt_amount
            FROM lis_pendens
            WHERE recording_date = ? AND (county = 'orange' OR pdf_path LIKE '%orange%' OR party_name LIKE '%Orange%')
            ORDER BY created_at DESC
        """, (target_date,))
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print(f"[*] No records in database for Orange County {target_date} to export.")
        return csv_filename

    fieldnames = [
        "instrument_number",
        "recording_date",
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
                "party_name": r.get("party_name", ""),
                "case_number": r.get("case_number", ""),
                "pdf_path": r.get("pdf_path", ""),
                "debt_amount": f"${r.get('debt_amount', 0):,.2f}" if r.get("debt_amount") else ""
            })

    print(f"[+] Exported daily Orange County CSV to: {csv_path} ({len(rows)} records)")

    # Sync to S3
    cloud_url = upload_file_to_cloud(csv_path, f"orange/{clean_date}/{csv_filename}")
    if cloud_url and cloud_url != csv_path:
        print(f"[+] Synced Orange County report to S3: {cloud_url}")

    return csv_filename


async def main():
    parser = argparse.ArgumentParser(description="Orange County Daily Lis Pendens Scraper")
    parser.add_argument("--date", type=str, help="Target recording date in MM/DD/YYYY format.")
    parser.add_argument("--today", action="store_true", help="Force collection for today.")
    args = parser.parse_args()

    init_db()

    target_date = calculate_target_date(manual_date=args.date, force_today=args.today)

    print(f"\n=======================================================")
    print(f" Orange County (Orlando) Lis Pendens Pipeline")
    print(f" Target Date: {target_date}")
    print(f" Started At:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"=======================================================\n")

    # Run Stage 1: Comptroller Official Records
    total_lps = await run_orange_stage1(target_date)

    # Export & Summary
    csv_filename = export_orange_daily_csv(target_date)

    print(f"\n=======================================================")
    print(f" ORANGE COUNTY COLLECTION SUMMARY FOR {target_date}")
    print(f"=======================================================")
    print(f"  • Total Lis Pendens Downloaded: {total_lps}")
    print(f"  • Report Generated:            {csv_filename}")
    print(f"=======================================================\n")

    # Notify downstream machine
    notify_downstream_machine({
        "county": "Orange",
        "target_date": target_date,
        "total_lps_collected": total_lps,
        "status": "COMPLETED",
        "csv_report": f"orange/{target_date.replace('/', '-')}/{csv_filename}",
        "timestamp": datetime.now().isoformat()
    })


if __name__ == "__main__":
    asyncio.run(main())
