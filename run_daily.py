import os
import csv
import sqlite3
import argparse
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

from stage1_ori import run_stage1_ori
from stage2_hover import run_stage2_hover
from orange_stage1 import run_orange_stage1
from orange_run_daily import export_orange_daily_csv
from storage import upload_file_to_cloud, notify_downstream_machine, verify_s3_contents
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


def export_daily_csv(target_date: str) -> str:
    """Exports all Hillsborough records for target_date to CSV."""
    clean_date = target_date.replace("/", "-")
    csv_filename = f"hillsborough_report_{clean_date}.csv"
    csv_path = os.path.join(BASE_DIR, csv_filename)

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT instrument_number, recording_date, party_name, case_number, pdf_path, debt_amount
            FROM lis_pendens
            WHERE recording_date = ? AND (instrument_number NOT LIKE 'ORANGE_%' AND pdf_path NOT LIKE '%orange%')
            ORDER BY created_at DESC
        """, (target_date,))
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print(f"[*] No Hillsborough records in database for {target_date} to export.")
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

    print(f"[+] Exported daily Hillsborough CSV to: {csv_path} ({len(rows)} records)")

    cloud_url = upload_file_to_cloud(csv_path, f"hillsborough/{clean_date}/{csv_filename}")
    if cloud_url and cloud_url != csv_path:
        print(f"[+] Synced report to S3: {cloud_url}")

    return csv_filename


def get_daily_summary(target_date: str) -> dict:
    """Generates execution metrics for the daily collection run."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM lis_pendens WHERE recording_date = ? AND pdf_path NOT LIKE '%orange%'", (target_date,))
        total_lps = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM lis_pendens WHERE recording_date = ? AND case_number != '' AND case_number IS NOT NULL AND pdf_path NOT LIKE '%orange%'", (target_date,))
        cases_id = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*), SUM(debt_amount) FROM lis_pendens WHERE recording_date = ? AND debt_amount > 0 AND pdf_path NOT LIKE '%orange%'", (target_date,))
        row = cur.fetchone()
        cases_with_debt = row[0]
        total_debt = row[1] if row[1] else 0.0

    return {
        "target_date": target_date,
        "total_lps_collected": total_lps,
        "cases_identified": cases_id,
        "cases_with_debt": cases_with_debt,
        "total_debt_value": total_debt
    }


async def main():
    parser = argparse.ArgumentParser(description="Florida Court Lis Pendens Multi-County Pipeline")
    parser.add_argument("--date", type=str, help="Target recording date in MM/DD/YYYY format.")
    parser.add_argument("--county", type=str, default="all", choices=["all", "hillsborough", "orange"], help="Target county")
    parser.add_argument("--today", action="store_true", help="Force collection for today.")
    parser.add_argument("--skip-hover", action="store_true", help="Skip Stage 2 (docket search).")
    args = parser.parse_args()

    init_db()

    target_date = calculate_target_date(manual_date=args.date, force_today=args.today)

    print(f"\n=======================================================")
    print(f" FLORIDA COURT RECORDS LIS PENDENS MACHINE")
    print(f" Target Date: {target_date}")
    print(f" Target County: {args.county.upper()}")
    print(f" Started At:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"=======================================================\n")

    # 1. Hillsborough County
    if args.county in ("all", "hillsborough"):
        print(f"\n>>> [1/2] RUNNING HILLSBOROUGH COUNTY PIPELINE <<<")
        try:
            await run_stage1_ori(target_date)
            if not args.skip_hover:
                await run_stage2_hover()
            csv_filename = export_daily_csv(target_date)
            summary = get_daily_summary(target_date)

            print(f"\n=======================================================")
            print(f" HILLSBOROUGH SUMMARY FOR {target_date}")
            print(f"=======================================================")
            print(f"  • Total Lis Pendens Downloaded: {summary['total_lps_collected']}")
            print(f"  • Case Numbers Identified:     {summary['cases_identified']}")
            print(f"  • Cases with Debt Extracted:   {summary['cases_with_debt']}")
            print(f"  • Total Extracted Debt Value:  ${summary['total_debt_value']:,.2f}")
            print(f"=======================================================\n")

            notify_downstream_machine({
                "county": "hillsborough",
                "target_date": target_date,
                "summary": summary,
                "csv_report": f"hillsborough/reports/{csv_filename}"
            })
        except Exception as e:
            print(f"[-] Error in Hillsborough pipeline: {e}")

    # 2. Orange County (Orlando)
    if args.county in ("all", "orange"):
        print(f"\n>>> [2/2] RUNNING ORANGE COUNTY PIPELINE <<<")
        try:
            try:
                from orange_probe import main as probe_orange
                await probe_orange()
            except Exception as pe:
                print(f"[!] Probe warning: {pe}")

            orange_lps = await run_orange_stage1(target_date)
            orange_csv = export_orange_daily_csv(target_date)

            print(f"\n=======================================================")
            print(f" ORANGE COUNTY SUMMARY FOR {target_date}")
            print(f"=======================================================")
            print(f"  • Total Lis Pendens Downloaded: {orange_lps}")
            print(f"  • Orange County Report:        {orange_csv}")
            print(f"=======================================================\n")

            notify_downstream_machine({
                "county": "orange",
                "target_date": target_date,
                "total_lps_collected": orange_lps,
                "status": "COMPLETED",
                "csv_report": f"orange/{target_date.replace('/', '-')}/{orange_csv}"
            })
        except Exception as e:
            print(f"[-] Error in Orange County pipeline: {e}")

    # 3. Live AWS S3 Verification Audit
    verify_s3_contents()


if __name__ == "__main__":
    asyncio.run(main())
