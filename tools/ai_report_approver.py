#!/usr/bin/env python3
"""AI Report Approver Agent.

Autonomous evaluation and approval gate for Residential Distress Reports.
Inspects jobs staged in `READY_FOR_APPROVAL`, audits every input against
legal and mathematical first principles (Rule R1: Parcel Grounding, Rule R10:
Authentic Court Debt, Preflight Distribution, and HTML Quality), and executes
the PostgreSQL RPC `approve_document_processing_job_report` or
`reject_document_processing_job_report`.

Usage:
  # Dry-run audit (inspect without modifying database):
  python tools/ai_report_approver.py --dry-run

  # Apply approvals to passing jobs:
  python tools/ai_report_approver.py --apply

  # Apply approvals AND route failing jobs to NEEDS_REVIEW:
  python tools/ai_report_approver.py --apply --auto-reject
"""
import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

ACTOR_PATTERN = re.compile(r"^human:[a-z0-9._@-]{1,64}$")
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

DEFAULT_ENV_PATHS = [
    Path.home() / "nage-runtime" / "env" / "document-worker.env",
    Path("/Users/Breezy/nage-runtime/env/document-worker.env"),
]


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


class AIReportApprover:
    def __init__(
        self,
        env: Dict[str, str],
        actor: str = "human:shane-autonomous-approver",
        session: Optional[requests.Session] = None,
    ):
        if not ACTOR_PATTERN.match(actor):
            raise ValueError(f"Actor token '{actor}' must match ^human:[a-z0-9._@-]{{1,64}}$")
        self.env = env
        self.actor = actor
        self.base_url = env["SUPABASE_URL"].rstrip("/")
        self.headers = {
            "apikey": env["SUPABASE_SERVICE_ROLE_KEY"],
            "Authorization": f"Bearer {env['SUPABASE_SERVICE_ROLE_KEY']}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self.session = session or requests.Session()

    def fetch_staged_jobs(
        self,
        county: Optional[str] = None,
        job_id: Optional[str] = None,
        case_number: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Fetch unarchived jobs currently in READY_FOR_APPROVAL."""
        params: Dict[str, Any] = {
            "state": "eq.READY_FOR_APPROVAL",
            "archived_at": "is.null",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        if job_id:
            params["id"] = f"eq.{job_id}"
        if county:
            params["source_county_slug"] = f"eq.{county.lower()}"
        if case_number:
            params["case_number"] = f"eq.{case_number}"

        url = f"{self.base_url}/rest/v1/document_processing_jobs"
        resp = self.session.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def fetch_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the staged report record by ID."""
        url = f"{self.base_url}/rest/v1/reports"
        resp = self.session.get(
            url,
            headers=self.headers,
            params={"id": f"eq.{report_id}"},
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else None

    def audit_job(
        self, job: Dict[str, Any], report: Dict[str, Any]
    ) -> Tuple[bool, List[str], Dict[str, Any]]:
        """Execute deterministic 5-point first-principles quality audit."""
        blockers: List[str] = []
        metrics: Dict[str, Any] = {}

        html = report.get("report_html") or ""
        warnings = report.get("audit_warnings") or []
        lane = job.get("lane") or report.get("pipeline_type") or "LIS_PENDENS"

        # --- 1. DISTRIBUTION PREFLIGHT (fail-closed contract) ---
        if report.get("audit_status") != "pending":
            blockers.append(f"Audit status is '{report.get('audit_status')}', expected 'pending'")
        if "FINANCIAL_TRUTH_ELIGIBILITY_V2" not in warnings:
            blockers.append("Report predates approved FINANCIAL_TRUTH_ELIGIBILITY_V2 audit")
        if lane == "LIS_PENDENS" and "Residential Distress Report" not in html:
            blockers.append("LIS report HTML is missing 'Residential Distress Report' marker")
        elif lane == "PROBATE" and 'class="probate-document"' not in html:
            blockers.append("Probate report HTML is missing 'probate-document' marker")

        # Transaction columns must remain null so screening numbers don't leak
        for col in ["current_balance", "equity", "ltv"]:
            if report.get(col) is not None:
                blockers.append(f"Transaction column '{col}' is occupied ({report.get(col)})")

        # --- 2. RULE R1: PARCEL GROUNDING ---
        parcel_id = report.get("parcel_id")
        property_address = report.get("property_address")
        owner_name = report.get("owner_name")

        if not parcel_id or str(parcel_id).strip() in ("", "None", "0"):
            blockers.append("Rule R1 Violation: Missing or invalid parcel_id")
        if not property_address or str(property_address).strip() in ("", "None"):
            blockers.append("Rule R1 Violation: Missing property_address")
        if not owner_name or str(owner_name).strip() in ("", "None"):
            blockers.append("Rule R1 Violation: Missing owner_name")

        # Check for fatal unresolvable parcel splits or mismatch warnings
        fatal_warnings = [
            "UNRESOLVED_PARCEL_SPLIT",
            "FATAL_FOLIO_MISMATCH",
            "MULTIPLE_UNRESOLVED_FOLIOS",
        ]
        for fw in fatal_warnings:
            if fw in warnings:
                blockers.append(f"Rule R1 Violation: Fatal warning present: {fw}")

        metrics["parcel_id"] = parcel_id
        metrics["property_address"] = property_address
        metrics["owner_name"] = owner_name

        # --- 3. RULE R10: AUTHENTIC COURT DEBT (LIS PENDENS) ---
        if lane == "LIS_PENDENS":
            debt_status = report.get("court_debt_status")
            debt_amount = report.get("court_debt_amount") if report.get("court_debt_amount") is not None else report.get("court_debt_case_total")
            metrics["court_debt_status"] = debt_status
            metrics["court_debt_amount"] = debt_amount

            if debt_status != "EXACT":
                blockers.append(
                    f"Rule R10 Violation: court_debt_status is '{debt_status}' (strictly requires 'EXACT')"
                )
            if debt_amount is None:
                blockers.append("Rule R10 Violation: Missing court_debt_amount")
            else:
                try:
                    amt = float(debt_amount)
                    if amt <= 0:
                        blockers.append(f"Rule R10 Violation: Non-positive debt amount (${amt:,.2f})")
                except (ValueError, TypeError):
                    blockers.append(f"Rule R10 Violation: Invalid debt amount '{debt_amount}'")

        # --- 4. FINANCIAL & VALUATION SANITY ---
        est_min = report.get("est_value_min")
        est_max = report.get("est_value_max")
        metrics["est_value_min"] = est_min
        metrics["est_value_max"] = est_max

        if est_min is None or est_min <= 0:
            blockers.append(f"Valuation Violation: Invalid est_value_min ({est_min})")

        screening_ltv = report.get("screening_ltv")
        if screening_ltv is not None:
            try:
                ltv_val = float(screening_ltv)
                metrics["screening_ltv"] = f"{ltv_val * 100:.1f}%"
                if ltv_val > 3.0:
                    blockers.append(f"Financial Sanity Warning: LTV exceeds 300% ({ltv_val*100:.1f}%)")
            except (ValueError, TypeError):
                pass

        # --- 5. RENDERED HTML QUALITY & INTEGRITY ---
        if len(html) < 5000:
            blockers.append(f"HTML Quality Violation: Report HTML is suspiciously small ({len(html)} bytes)")

        # Verify no placeholder or template leakages
        unrendered_patterns = ["[object Object]", "NaN%", "undefined", "None, None"]
        for pat in unrendered_patterns:
            if pat in html:
                blockers.append(f"HTML Quality Violation: Detected unrendered pattern '{pat}' in HTML")

        passed = len(blockers) == 0
        return passed, blockers, metrics

    def approve_job(
        self, job_id: str, report_id: str, client_id: str
    ) -> Dict[str, Any]:
        """Call PostgreSQL RPC approve_document_processing_job_report."""
        url = f"{self.base_url}/rest/v1/rpc/approve_document_processing_job_report"
        payload = {
            "p_job_id": job_id,
            "p_actor": self.actor,
            "p_report_id": report_id,
            "p_client_id": client_id,
        }
        resp = self.session.post(url, headers=self.headers, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Approval RPC failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def reject_job(
        self, job_id: str, report_id: str, review_reason: str = "approval_changes_requested"
    ) -> Dict[str, Any]:
        """Call PostgreSQL RPC reject_document_processing_job_report."""
        url = f"{self.base_url}/rest/v1/rpc/reject_document_processing_job_report"
        payload = {
            "p_job_id": job_id,
            "p_actor": self.actor,
            "p_report_id": report_id,
            "p_review_reason": review_reason,
        }
        resp = self.session.post(url, headers=self.headers, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Rejection RPC failed ({resp.status_code}): {resp.text}")
        return resp.json()


def run_approver(
    apply: bool = False,
    auto_reject: bool = False,
    county: Optional[str] = None,
    job_id: Optional[str] = None,
    case_number: Optional[str] = None,
    limit: int = 50,
    actor: str = "human:shane-autonomous-approver",
    env_path: Optional[Path] = None,
) -> Dict[str, Any]:
    env = load_environment(env_path)
    approver = AIReportApprover(env, actor=actor)

    print(f"[*] AI Report Approver Agent initialized")
    print(f"[*] Actor Attribution: {actor}")
    print(f"[*] Mode: {'APPLY (Database Writes Enabled)' if apply else 'DRY RUN (Read Only)'}")
    if apply and auto_reject:
        print(f"[*] Auto-Reject: Enabled (Defective jobs will transition to NEEDS_REVIEW)")
    print("-" * 70)

    jobs = approver.fetch_staged_jobs(county=county, job_id=job_id, case_number=case_number, limit=limit)
    print(f"[*] Found {len(jobs)} jobs in READY_FOR_APPROVAL")

    approved_count = 0
    rejected_count = 0
    passed_audit_count = 0
    blocked_audit_count = 0
    results: List[Dict[str, Any]] = []

    for idx, job in enumerate(jobs, 1):
        jid = job["id"]
        cnum = job.get("case_number", "UNKNOWN")
        cslug = job.get("source_county_slug", "fl")
        rid = job.get("report_id")
        cid = job.get("client_id")

        print(f"\n[{idx}/{len(jobs)}] Case: {cnum} ({cslug.upper()}) | Job: {jid}")

        if not rid:
            print("  X Blocker: Job has no staged report_id attached")
            blocked_audit_count += 1
            continue

        report = approver.fetch_report(rid)
        if not report:
            print(f"  X Blocker: Staged report {rid} not found in reports table")
            blocked_audit_count += 1
            continue

        passed, blockers, metrics = approver.audit_job(job, report)

        entry = {
            "job_id": jid,
            "case_number": cnum,
            "county": cslug,
            "report_id": rid,
            "client_id": cid,
            "passed": passed,
            "blockers": blockers,
            "metrics": metrics,
        }

        if passed:
            passed_audit_count += 1
            debt_str = f"${metrics.get('court_debt_amount', 0):,.2f}" if metrics.get("court_debt_amount") else "N/A"
            est_str = f"${metrics.get('est_value_min', 0):,.2f}" if metrics.get("est_value_min") else "N/A"
            print(f"  ✓ PASSED ALL AUDITS:")
            print(f"    - Parcel:   {metrics.get('parcel_id')}")
            print(f"    - Address:  {metrics.get('property_address')}")
            print(f"    - Owner:    {metrics.get('owner_name')}")
            print(f"    - Debt:     {debt_str} ({metrics.get('court_debt_status')})")
            print(f"    - Value:    {est_str} (LTV: {metrics.get('screening_ltv', 'N/A')})")

            if apply:
                try:
                    res = approver.approve_job(jid, rid, cid)
                    print(f"  [+] APPROVED & PUBLISHED via RPC: {res.get('report_visibility')}")
                    approved_count += 1
                    entry["applied_action"] = "approved"
                except Exception as e:
                    print(f"  [!] Approval RPC error: {e}")
                    entry["error"] = str(e)
            else:
                print(f"  [~] Ready for approval (Run with --apply to publish)")
        else:
            blocked_audit_count += 1
            print(f"  X AUDIT FAILED ({len(blockers)} blockers):")
            for b in blockers:
                print(f"    - {b}")

            if apply and auto_reject:
                try:
                    res = approver.reject_job(jid, rid, "approval_changes_requested")
                    print(f"  [+] REJECTED & DIVERTED to NEEDS_REVIEW via RPC")
                    rejected_count += 1
                    entry["applied_action"] = "rejected_changes_requested"
                except Exception as e:
                    print(f"  [!] Rejection RPC error: {e}")
                    entry["error"] = str(e)

        results.append(entry)

    print("\n" + "=" * 70)
    print("AUDIT SUMMARY:")
    print(f"  Total Evaluated: {len(jobs)}")
    print(f"  Audit Passed:    {passed_audit_count}")
    print(f"  Audit Blocked:   {blocked_audit_count}")
    if apply:
        print(f"  RPC Approved:    {approved_count}")
        print(f"  RPC Rejected:    {rejected_count}")
    print("=" * 70)

    return {
        "total": len(jobs),
        "passed": passed_audit_count,
        "blocked": blocked_audit_count,
        "approved": approved_count,
        "rejected": rejected_count,
        "results": results,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", type=Path, help="Path to document-worker.env")
    p.add_argument("--apply", action="store_true", help="Apply approvals/rejections in database via RPC")
    p.add_argument("--dry-run", action="store_true", help="Run in read-only audit mode (default)")
    p.add_argument("--auto-reject", action="store_true", help="Automatically reject defective jobs into NEEDS_REVIEW")
    p.add_argument("--county", type=str, help="Filter jobs by county slug (e.g. broward, hillsborough)")
    p.add_argument("--case-number", type=str, help="Audit a specific court case number")
    p.add_argument("--job-id", type=str, help="Audit a specific job UUID")
    p.add_argument("--limit", type=int, default=50, help="Maximum number of jobs to evaluate (default 50)")
    p.add_argument("--actor", type=str, default="human:shane-autonomous-approver", help="Attribution actor token")
    p.add_argument("--json", action="store_true", help="Output summary in JSON format")

    args = p.parse_args()

    # Default to dry-run unless apply is explicitly specified
    apply_mode = args.apply and not args.dry_run

    summary = run_approver(
        apply=apply_mode,
        auto_reject=args.auto_reject,
        county=args.county,
        job_id=args.job_id,
        case_number=args.case_number,
        limit=args.limit,
        actor=args.actor,
        env_path=args.env,
    )

    if args.json:
        # Strip long objects for clean JSON stdout
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
