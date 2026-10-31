import os
import sys
import argparse
import asyncio
import csv
from datetime import datetime, timedelta

from stage1_ori import run_stage1_ori
from stage2_hover import run_stage2_hover
from database import init_db, get_daily_summary, get_connection
from storage import upload_file_to_cloud, notify_downstream_machine

def calculate_target_date(manual_date: str = None, force_today: bool = False) -> str:
    """
    Intelligently determines the target recording date:
    - If manual_date provided: uses that date.
    - If force_today: uses today's date.
    - If run after 5:30 PM on a weekday: collects today (business day ended).
    - If run on Monday morning: collects Friday (3 days ago).
    - If run on Sunday: collects Friday (2 days ago).
    - If run on Saturday: collects Friday (1 day ago).
    - Otherwise (Tue-Fri morning): collects yesterday.
    """
    if manual_date:
        return manual_date

    now = datetime.now()

    if force_today or now.hour >= 17 and now.minute >= 30 and now.weekday() < 5:
        # Evening weekday run: collect today's completed day
        return now.strftime("%m/%d/%Y")

    weekday = now.weekday()  # Monday is 0, Sunday is 6

    if weekday == 0:
        # Monday morning: previous business day is Friday (3 days ago)
        target = now - timedelta(days=3)
    elif weekday == 6:
        # Sunday: previous business day is Friday (2 days ago)
        target = now - timedelta(days=2)
    elif weekday == 5:
        # Saturday: previous business day is Friday (1 day ago)
        target = now - timedelta(days=1)
    else:
        # Tue-Fri morning: previous day
        target = now - timedelta(days=1)

    return target.strftime("%m/%d/%Y")


def export_daily_csv(target_date: str):
    """Exports all records for the given date to a CSV file."""
    clean_date = target_date.replace("/", "-")
    csv_filename = f"hillsborough_report_{clean_date}.csv"
    csv_path = os.path.join(os.path.dirname(__file__), csv_filename)

    with get_connection() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                instrument_number,
                recording_date,
                party_name,
                case_number,
                debt_amount,
                hover_status,
                pdf_path,
                docket_pdf_path
            FROM lis_pendens
            WHERE recording_date = ?
            ORDER BY instrument_number DESC
        """, (target_date,))
        rows = cur.fetchall()

    if not rows:
        print(f"[*] No records in database for {target_date} to export.")
        return

    fieldnames = [
        "instrument_number",
        "recording_date",
        "party_name",
        "case_number",
        "debt_amount",
        "hover_status",
        "pdf_path",
        "docket_pdf_path"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[+] Exported daily CSV report to: {csv_path}")

    # Cloud Upload if configured
    cloud_url = upload_file_to_cloud(csv_path, f"hillsborough/reports/{csv_filename}")
    if cloud_url and cloud_url != csv_path:
        print(f"[+] Synced report to S3: {cloud_url}")


async def main():
    parser = argparse.ArgumentParser(description="Hillsborough County Daily LP & Court Docket Scraper")
    parser.add_argument("--date", type=str, help="Target recording date in MM/DD/YYYY format.")
    parser.add_argument("--today", action="store_true", help="Force collection for today instead of previous business day.")
    parser.add_argument("--skip-hover", action="store_true", help="Only run Stage 1 (ORI LP collection).")
    args = parser.parse_args()

    init_db()

    target_date = calculate_target_date(manual_date=args.date, force_today=args.today)

    print(f"\n=======================================================")
    print(f" Hillsborough County Lis Pendens Pipeline")
    print(f" Target Date: {target_date}")
    print(f" Run Mode:    {'Full (Stage 1 + Stage 2)' if not args.skip_hover else 'Stage 1 Only'}")
    print(f" Started At:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"=======================================================\n")

    # Run Stage 1: Official Records
    await run_stage1_ori(target_date)

    # Run Stage 2: HOVER Court Docket & Debt Amount
    if not args.skip_hover:
        await run_stage2_hover()

    # Export & Summary
    export_daily_csv(target_date)
    summary = get_daily_summary(target_date)

    print(f"\n=======================================================")
    print(f" COLLECTION SUMMARY FOR {target_date}")
    print(f"=======================================================")
    print(f"  • Total Lis Pendens Downloaded: {summary['total_lps_collected']}")
    print(f"  • Case Numbers Identified:     {summary['cases_identified']}")
    print(f"  • Cases with Debt Extracted:   {summary['cases_with_debt']}")
    print(f"  • Total Extracted Debt Value:  ${summary['total_debt_value']:,.2f}")
    print(f"=======================================================\n")

    # Notify downstream pipeline / machine
    notify_downstream_machine({
        "county": "hillsborough",
        "target_date": target_date,
        "summary": summary,
        "csv_report": f"hillsborough/reports/{csv_filename}"
    })


if __name__ == "__main__":
    asyncio.run(main())
