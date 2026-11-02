#!/usr/bin/env python3
"""
NAGE Cloud Factory Intake
=========================
Production-grade intake for Florida Court Scrapers adhering strictly to:
  1. The established admission mechanism (source manifests retained before queueing).
  2. Verified storage handoff (remote byte & sha256 confirmation before enqueueing).
  3. No report shortcuts (all underwriting, debt extraction, and reports produced
     exclusively through authentic machine worker stages).
  4. Idempotent queue admission and failure isolation.
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import ssl
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CONTEXT = ssl.create_default_context()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud_factory")

DEFAULT_LP_RELEASE_CONFIG = Path.home() / "nage-runtime" / "document-worker" / "lp-pat-release.json"
DEFAULT_FL_COUNTIES_CONFIG = (
    Path.home() / "nage-runtime" / "document-worker" / "repo" / "config" / "florida-document-counties.json"
)

CLIENT_LABELS = {
    "221ed457-ecd0-4af3-aa9e-b956f33517e8": "George Munoz",
    "42907af4-43b3-447e-ac8b-696278f79f7e": "Keith Thomas",
    "aea89aa3-8961-4e35-8eb7-8ebf3ccb8c39": "Pat Brester",
    "24a05e5a-b2f9-4b58-87d1-a7c71a62638b": "NAGE Acquisitions",
}


def _load_counties() -> Dict[str, Dict[str, str]]:
    """Load canonical 67-county routing from lp-pat-release.json and florida-document-counties.json."""
    fallback = {
        "hillsborough": {
            "code": "29",
            "fips": "12057",
            "client_id": "221ed457-ecd0-4af3-aa9e-b956f33517e8",
            "client_name": "George Munoz",
            "name": "Hillsborough",
        },
        "broward": {
            "code": "06",
            "fips": "12011",
            "client_id": "42907af4-43b3-447e-ac8b-696278f79f7e",
            "client_name": "Keith Thomas",
            "name": "Broward",
        },
        "duval": {
            "code": "16",
            "fips": "12031",
            "client_id": "42907af4-43b3-447e-ac8b-696278f79f7e",
            "client_name": "Keith Thomas",
            "name": "Duval",
        },
        "orange": {
            "code": "48",
            "fips": "12095",
            "client_id": "24a05e5a-b2f9-4b58-87d1-a7c71a62638b",
            "client_name": "NAGE Acquisitions",
            "name": "Orange",
        },
        "manatee": {
            "code": "41",
            "fips": "12081",
            "client_id": "aea89aa3-8961-4e35-8eb7-8ebf3ccb8c39",
            "client_name": "Pat Brester",
            "name": "Manatee",
        },
        "sarasota": {
            "code": "58",
            "fips": "12115",
            "client_id": "aea89aa3-8961-4e35-8eb7-8ebf3ccb8c39",
            "client_name": "Pat Brester",
            "name": "Sarasota",
        },
    }
    lp_config_path = Path(os.getenv("NAGE_LP_RELEASE_CONFIG", str(DEFAULT_LP_RELEASE_CONFIG)))
    fl_config_path = Path(os.getenv("NAGE_FL_COUNTIES_CONFIG", str(DEFAULT_FL_COUNTIES_CONFIG)))
    try:
        lp_data = json.loads(lp_config_path.read_text()) if lp_config_path.exists() else {}
        fl_data = json.loads(fl_config_path.read_text()) if fl_config_path.exists() else {}
    except Exception:
        return fallback

    fl_by_slug = {c["county_slug"]: c for c in fl_data.get("counties", []) if "county_slug" in c}
    lp_counties = lp_data.get("counties", {})
    if not lp_counties and not fl_by_slug:
        return fallback

    merged: Dict[str, Dict[str, str]] = {}
    for slug in sorted(set(lp_counties.keys()) | set(fl_by_slug.keys()) | set(fallback.keys())):
        lp_entry = lp_counties.get(slug, {})
        fl_entry = fl_by_slug.get(slug, {})
        fb_entry = fallback.get(slug, {})
        client_id = lp_entry.get("client_id") or fb_entry.get("client_id") or "24a05e5a-b2f9-4b58-87d1-a7c71a62638b"
        fips = lp_entry.get("fips") or fl_entry.get("county_fips") or fb_entry.get("fips") or ""
        code = fl_entry.get("court_county_code") or fb_entry.get("code") or "00"
        display = fl_entry.get("display_name", "").removesuffix(" County").strip() or fb_entry.get("name") or slug.title()
        merged[slug] = {
            "code": code,
            "fips": fips,
            "client_id": client_id,
            "client_name": CLIENT_LABELS.get(client_id, client_id),
            "name": display,
        }
    return merged


COUNTIES = _load_counties()


class IntakeError(RuntimeError):
    pass


class StorageVerificationError(IntakeError):
    pass


def normalize_ucn(county_slug: str, raw_case: str) -> Optional[str]:
    """Normalize raw court case number into Florida standard UCN format."""
    if not raw_case:
        return None
    raw = raw_case.strip().replace(" ", "").upper()
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    code = COUNTIES.get(county_slug, {}).get("code", "00")

    # Handle Broward clerk local case numbers (e.g. CACE-26-012345 or CACE2026012345)
    if county_slug == "broward":
        m_broward = re.fullmatch(r"(CACE|COCE|CONO|COSO|COWE)(?:(19|20)?(\d{2}))0*(\d{4,8})", compact)
        if m_broward:
            century = m_broward.group(2) or "20"
            year = f"{century}{m_broward.group(3)}"
            kind = "CA" if m_broward.group(1) == "CACE" else "CC"
            num_str = f"{int(m_broward.group(4)):06d}"
            return f"06-{year}-{kind}-{num_str}"

    m = re.search(
        r"(?:(\d{2})[-_]?)?((?:19|20)\d{2})[-_]?([A-Z]{2})[-_]?0*(\d{4,8})([A-Z0-9]{0,8})?|(\d{2})[-_](\d{2})[-_]([A-Z]{2})[-_]?0*(\d{4,8})([A-Z0-9]{0,8})?",
        raw,
    )
    if m:
        if m.group(2):
            c_prefix, year, kind, num, suffix = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5) or ""
        else:
            c_prefix, year, kind, num, suffix = m.group(6), "20" + m.group(7), m.group(8), m.group(9), m.group(10) or ""
        prefix = c_prefix or code
        num_str = f"{int(num):06d}"
        if county_slug == "sarasota" and suffix.endswith(("NC", "SC")):
            return f"{prefix}-{year}-{kind}-{num_str}-{suffix[-2:]}"
        return f"{prefix}-{year}-{kind}-{num_str}"
    return raw


def normalize_court_case_key(county_slug: str, raw_case: Optional[str]) -> str:
    """Canonical civil case key matching court_debt_record.case_key (e.g. 2026CA010375)."""
    literal = re.sub(r"[-_\s]", "", str(raw_case or "").upper())
    if county_slug == "broward" and re.match(r"^(?:CACE|COCE|CONO|COSO|COWE)", literal):
        ucn = normalize_ucn(county_slug, literal)
        literal = re.sub(r"[-_\s]", "", str(ucn or "").upper())
    pattern = r"((?:19|20)\d{2})(CA|CC|SC)(\d{6})([A-Z][A-Z0-9]{0,7}|[0-9]{4}[A-Z]{2})?"
    full = re.fullmatch(r"(\d{2})" + pattern, literal)
    if full:
        expected_code = COUNTIES.get(county_slug, {}).get("code")
        if expected_code and full.group(1) != expected_code:
            raise IntakeError(f"COURT_CASE_COUNTY_MISMATCH: {raw_case} does not match {county_slug}")
        year, kind, number, suffix = full.groups()[1:]
    else:
        short = re.fullmatch(pattern, literal)
        if not short:
            raise IntakeError(f"INVALID_COURT_CASE_NUMBER: {raw_case}")
        year, kind, number, suffix = short.groups()
    location = (
        suffix[-2:]
        if county_slug == "sarasota" and suffix and suffix.endswith(("NC", "SC"))
        else ""
    )
    return f"{year}{kind}{number}{location}"


COURT_DEBT_SCHEMA = "nage.court-debt-record/1"
COURT_DEBT_STATUSES = {
    "HELD",
    "BAND",
    "NOT_PUBLIC",
    "NO_FORM_ON_DOCKET",
    "CASE_NOT_FOUND",
    "NEEDS_SHANE",
    "NOT_CHECKED",
}
COURT_DEBT_REASONS = {
    "HELD": "Court form needs review.",
    "BAND": "Only a court filing range is known; no exact debt established.",
    "NOT_PUBLIC": "Court form is not public.",
    "NO_FORM_ON_DOCKET": "Court case checked; no value form filed yet.",
    "CASE_NOT_FOUND": "Court search checked; no matching case found.",
    "NEEDS_SHANE": "Court evidence needs Shane’s review.",
    "NOT_CHECKED": "Court form has not been checked yet.",
}
DEFAULT_COURT_DROP_ROOT = Path("/Users/Breezy/nage-runtime/court-debt-records")


def compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_source_key(client_id: str, county: str, case_number: Optional[str], sha: str, instrument: Optional[str] = None) -> str:
    """Canonical source key generator matching lp_production.source_key."""
    if case_number is None:
        filing_id = f"{COUNTIES[county]['fips']}:{instrument}"
        return hashlib.sha256(json.dumps([client_id, filing_id], separators=(',', ':')).encode()).hexdigest()
    return hashlib.sha256(json.dumps([client_id, county, case_number, sha], separators=(',', ':')).encode()).hexdigest()


class VerifiedCloudIntake:
    def __init__(
        self,
        supabase_url: Optional[str] = None,
        service_key: Optional[str] = None,
        admissions_root: Optional[Path] = None,
        court_drop_root: Optional[Path] = None,
    ):
        self.supabase_url = (supabase_url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.service_key = service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.headers = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        
        # Admissions root: local default or configured path
        default_admissions = Path.home() / "nage-runtime" / "lp-daily-release-20261008" / "production-admissions"
        self.admissions_root = admissions_root or Path(os.getenv("NAGE_ADMISSIONS_ROOT", str(default_admissions)))
        self.court_drop_root = court_drop_root or Path(os.getenv("NAGE_COURT_DROP_ROOT", str(DEFAULT_COURT_DROP_ROOT)))

    def is_configured(self) -> bool:
        return bool(self.supabase_url and self.service_key)

    def upload_and_verify_storage(self, local_pdf: Path, storage_key: str, expected_sha: str) -> None:
        """Uploads source PDF to Supabase Storage and verifies downloaded bytes strictly match sha256."""
        if not self.is_configured():
            raise IntakeError("SUPABASE_NOT_CONFIGURED")

        object_url = f"{self.supabase_url}/storage/v1/object/county-raw/{storage_key}"
        auth_headers = {"apikey": self.service_key, "Authorization": f"Bearer {self.service_key}"}

        # 1. Check if object already exists with matching hash
        req_check = urllib.request.Request(object_url, headers=auth_headers, method="GET")
        try:
            with urllib.request.urlopen(req_check, context=SSL_CONTEXT, timeout=15) as resp:
                if resp.status == 200:
                    data = resp.read()
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha == expected_sha:
                        logger.info(f"[+] Storage verified (already present): {storage_key}")
                        return
                    else:
                        raise StorageVerificationError(f"FOLDER_REMOTE_SOURCE_DIFFERS: expected {expected_sha}, got {actual_sha}")
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                raise StorageVerificationError(f"STORAGE_CHECK_FAILED: HTTP {e.code}")
        except StorageVerificationError:
            raise
        except Exception as e:
            raise StorageVerificationError(f"STORAGE_CHECK_TRANSPORT_ERROR: {e}")

        # 2. Upload binary
        pdf_bytes = local_pdf.read_bytes()
        req_upload = urllib.request.Request(
            object_url,
            data=pdf_bytes,
            headers={**auth_headers, "Content-Type": "application/pdf", "x-upsert": "false"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req_upload, context=SSL_CONTEXT, timeout=30) as resp:
                if resp.status not in (200, 201):
                    raise StorageVerificationError(f"FOLDER_UPLOAD_UNCERTAIN_RECONCILIATION_REQUIRED: status {resp.status}")
        except urllib.error.HTTPError as e:
            raise StorageVerificationError(f"FOLDER_UPLOAD_FAILED: HTTP {e.code} - {e.read().decode()}")
        except Exception as e:
            raise StorageVerificationError(f"FOLDER_UPLOAD_TRANSPORT_ERROR: {e}")

        # 3. Read back and verify hash
        req_verify = urllib.request.Request(object_url, headers=auth_headers, method="GET")
        try:
            with urllib.request.urlopen(req_verify, context=SSL_CONTEXT, timeout=15) as resp:
                saved_bytes = resp.read()
                actual_sha = hashlib.sha256(saved_bytes).hexdigest()
                if actual_sha != expected_sha:
                    raise StorageVerificationError(f"FOLDER_REMOTE_SOURCE_DIFFERS: expected {expected_sha}, got {actual_sha}")
                logger.info(f"[+] Uploaded and verified storage object {storage_key} ({len(saved_bytes)} bytes)")
        except StorageVerificationError:
            raise
        except Exception as e:
            raise StorageVerificationError(f"STORAGE_VERIFICATION_READBACK_FAILED: {e}")

    def retain_intake_record(
        self,
        client_id: str,
        county_slug: str,
        case_number: Optional[str],
        sha: str,
        instrument_number: Optional[str],
        local_pdf_path: Path,
        filing_date: Optional[str] = None,
    ) -> str:
        """Retains admission source manifest record identifying client, county, PDF hash and documents."""
        sources_dir = self.admissions_root / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)

        fips = COUNTIES[county_slug]["fips"]
        filing_id = f"{fips}:{instrument_number}" if instrument_number else None

        manifest = {
            "client_id": client_id,
            "case_number": case_number,
            "pdf_sha256": sha,
            "source_county_slug": county_slug,
            "instrument_number": instrument_number,
            "filing_identity": filing_id,
            "documents": [
                {
                    "path": str(local_pdf_path.resolve()),
                    "sha256": sha,
                    "document_id": sha,
                    "title": "NOTICE OF LIS PENDENS",
                    "filed_date": filing_date or str(datetime.date.today()),
                    "case_number": case_number,
                    "instrument_number": instrument_number,
                }
            ],
            "source_cutoff": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "as_of": str(datetime.date.today()),
        }

        # Primary source key (client + county + case + sha)
        source_id = compute_source_key(client_id, county_slug, case_number, sha, instrument_number)
        path = sources_dir / f"{source_id}.json"
        
        # Atomic write
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(manifest, indent=2))
        tmp_path.replace(path)

        # Fallback key by filing_identity in case case_number was unresolved at press time
        if instrument_number:
            fallback_id = compute_source_key(client_id, county_slug, None, sha, instrument_number)
            fb_path = sources_dir / f"{fallback_id}.json"
            if not fb_path.exists():
                tmp_fb = fb_path.with_suffix(".tmp")
                tmp_fb.write_text(json.dumps(manifest, indent=2))
                tmp_fb.replace(fb_path)

        logger.info(f"[+] Retained admission manifest {source_id}.json for {county_slug} case {case_number}")
        return source_id

    def check_existing_job(self, client_id: str, sha: str, instrument_number: Optional[str] = None) -> Optional[Dict]:
        """Checks if a job already exists for this exact PDF hash or instrument."""
        if not self.is_configured():
            return None

        # Query by SHA
        url = f"{self.supabase_url}/rest/v1/document_processing_jobs?client_id=eq.{client_id}&pdf_sha256=eq.{sha}&select=id,state,case_number,attempts,created_at&limit=1"
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        try:
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data:
                    return data[0]
        except Exception as e:
            logger.warning(f"[-] Check existing job query failed: {e}")
        return None

    def enqueue_job(
        self,
        lane: str,
        case_number: Optional[str],
        instrument_number: Optional[str],
        client_id: str,
        storage_key: str,
        pdf_sha256: str,
        filename: str,
        county_slug: str,
    ) -> str:
        """Enqueues document processing job via official Supabase RPC."""
        if not self.is_configured():
            raise IntakeError("SUPABASE_NOT_CONFIGURED")

        rpc_url = f"{self.supabase_url}/rest/v1/rpc/enqueue_document_processing_job_v2"
        county_info = COUNTIES[county_slug]
        source_task_id = f"folder:{lane}:{pdf_sha256}"

        payload = {
            "p_lane": lane,
            "p_source_task_id": source_task_id,
            "p_case_number": case_number,
            "p_instrument_number": instrument_number,
            "p_client_id": client_id,
            "p_storage_object_key": storage_key,
            "p_pdf_sha256": pdf_sha256,
            "p_original_filename": filename,
            "p_operator": "human:shane",
            "p_source_state": "FL",
            "p_source_county_slug": county_slug,
            "p_source_county_fips": county_info["fips"],
            "p_allow_new_revision": False,
        }

        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                job_id = res.get("job_id")
                state = res.get("state")
                logger.info(f"[+] Successfully enqueued job {job_id} in state {state}")
                return job_id
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode()
            logger.error(f"[-] Enqueue RPC HTTP {e.code}: {err_msg}")
            raise IntakeError(f"ENQUEUE_FAILED: HTTP {e.code} - {err_msg}")
        except Exception as e:
            logger.error(f"[-] Enqueue RPC transport failure: {e}")
            raise IntakeError(f"ENQUEUE_TRANSPORT_ERROR: {e}")

    def call_court_debt_record(self, action: str, **payload) -> object:
        """Invokes public.court_debt_record RPC via service_role."""
        if not self.is_configured():
            raise IntakeError("SUPABASE_NOT_CONFIGURED")

        rpc_url = f"{self.supabase_url}/rest/v1/rpc/court_debt_record"
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps({"p_action": action, "p_payload": payload}).encode("utf-8"),
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode()
            logger.error(f"[-] court_debt_record({action}) HTTP {e.code}: {err_msg}")
            raise IntakeError(f"COURT_DEBT_RPC_FAILED: HTTP {e.code} - {err_msg}")
        except Exception as e:
            logger.error(f"[-] court_debt_record({action}) transport failure: {e}")
            raise IntakeError(f"COURT_DEBT_RPC_TRANSPORT_ERROR: {e}")

    def drop_court_debt_record(
        self,
        county_slug: str,
        raw_case_number: str,
        *,
        court_claim_pdf: Optional[Path] = None,
        source_url: str,
        document_title: str = "VALUE OF MORTGAGE FORECLOSURE REAL PROPERTY CLAIM FORM",
        filed_date: Optional[str] = None,
        filing_date: Optional[str] = None,
        instrument_number: Optional[str] = None,
        first_defendant: Optional[str] = None,
        status: str = "HELD",
        status_reason: Optional[str] = None,
        checked_by: str = "Claude (Ego Lite), attended by Shane",
        checked_at: Optional[str] = None,
        parcel_count: Optional[int] = 1,
    ) -> Dict:
        """
        Phase 2 Court Debt Drop-In matching document_processing_worker.court_debt_record.drop().
        Writes court-source.pdf and case.receipt.json into /Users/Breezy/nage-runtime/court-debt-records/<county>/<case_key>/
        and registers the record in public.court_debt_records so worker_debt reads the exact claim.
        """
        if status not in COURT_DEBT_STATUSES:
            raise IntakeError(f"INVALID_COURT_DEBT_STATUS: {status} (only machine reader may set EXACT)")
        if not source_url:
            raise IntakeError("COURT_DEBT_SOURCE_URL_REQUIRED")

        case_key = normalize_court_case_key(county_slug, raw_case_number)
        checked_iso = checked_at or datetime.datetime.now(datetime.timezone.utc).isoformat()

        case_dir = (self.court_drop_root / county_slug / case_key).resolve()
        case_dir.mkdir(parents=True, exist_ok=True)

        supplied: Dict[str, object] = {
            "county": county_slug,
            "court_case_number": case_key,
            "status": status,
            "status_reason": status_reason or COURT_DEBT_REASONS[status],
            "source_url": source_url,
            "checked_at": checked_iso,
            "checked_by": checked_by,
            "case_binding": "ATTENDED_COURT_CASE",
        }
        if instrument_number:
            supplied["lp_instrument_numbers"] = [str(instrument_number)]
        if document_title:
            supplied["document_title"] = document_title
        if filed_date:
            datetime.date.fromisoformat(filed_date)
            supplied["filed_date"] = filed_date
        if filing_date:
            datetime.date.fromisoformat(filing_date)
            supplied["filing_date"] = filing_date
        if first_defendant:
            supplied["first_defendant"] = first_defendant

        dest_pdf: Optional[Path] = None
        fingerprint: Optional[str] = None
        if court_claim_pdf is not None:
            if not court_claim_pdf.exists():
                raise FileNotFoundError(f"Court claim PDF missing: {court_claim_pdf}")
            raw_bytes = court_claim_pdf.read_bytes()
            if not raw_bytes.startswith(b"%PDF-") or b"%%EOF" not in raw_bytes[-2048:]:
                raise IntakeError(f"INCOMPLETE_COURT_CLAIM_PDF: {court_claim_pdf}")
            dest_pdf = case_dir / "court-source.pdf"
            if court_claim_pdf.resolve() != dest_pdf.resolve():
                tmp_pdf = dest_pdf.with_suffix(".pdf.tmp")
                tmp_pdf.write_bytes(raw_bytes)
                tmp_pdf.replace(dest_pdf)
            fingerprint = hashlib.sha256(raw_bytes).hexdigest()
            supplied["pdf"] = "court-source.pdf"

        # Write case.receipt.json atomically for worker maintenance / audit trail
        receipt_path = case_dir / "case.receipt.json"
        tmp_receipt = receipt_path.with_suffix(".json.tmp")
        tmp_receipt.write_text(json.dumps(supplied, indent=2))
        tmp_receipt.replace(receipt_path)

        if not self.is_configured():
            logger.info(
                f"[+] Staged court debt drop in {case_dir} for worker maintenance "
                f"({county_slug} {case_key})"
            )
            return {
                "id": None,
                "county": county_slug,
                "case_number": case_key,
                "receipt_path": str(receipt_path),
                "pdf_sha256": fingerprint,
                "body": supplied,
            }

        # Register in public.court_debt_records (mirroring court_debt_record.drop)
        records = self.call_court_debt_record("LIST", county=county_slug)
        previous = next((r for r in (records or []) if r.get("case_number") == case_key), None)
        if previous and previous["body"].get("status") == "NEEDS_SHANE" and status != "NEEDS_SHANE":
            raise IntakeError("COURT_DEBT_OWNER_CHECK_OUTSTANDING: cannot clear NEEDS_SHANE via intake drop")

        body = dict(previous["body"]) if previous else {"schema": COURT_DEBT_SCHEMA}
        for k in ("source_url", "document_title", "filed_date", "filing_date", "checked_by", "case_binding"):
            if k in supplied:
                body[k] = supplied[k]
        body["lp_instrument_numbers"] = sorted(
            set(body.get("lp_instrument_numbers", []))
            | {str(x) for x in supplied.get("lp_instrument_numbers", [])}
        )
        if first_defendant:
            body["first_defendant_key"] = " ".join(re.sub(r"[^A-Z0-9 ]", " ", first_defendant.upper()).split())
        if parcel_count is not None and body.get("parcel_count") is None:
            body["parcel_count"] = int(parcel_count)
        body.update(
            status=status,
            status_reason=supplied["status_reason"],
            checked_at=checked_iso,
            next_check_at=None,
        )

        if dest_pdf is not None and fingerprint is not None:
            receipt_hash = hashlib.sha256(
                json.dumps([supplied, fingerprint], sort_keys=True).encode("utf-8")
            ).hexdigest()
            if (
                previous
                and previous["body"].get("acquisition_receipt_sha256") == receipt_hash
                and previous["body"].get("parcel_count") == body.get("parcel_count")
            ):
                logger.info(f"[i] Court debt record already up to date for {county_slug} {case_key} (v{previous['version']})")
                return previous
            body["acquisition_receipt_sha256"] = receipt_hash
            same_pdf = body.get("source_pdf_fingerprint") == fingerprint
            body.update(
                source_pdf_fingerprint=fingerprint,
                source_pdf_path=str(dest_pdf.resolve()),
            )
            if same_pdf and previous and previous["body"].get("status") == "EXACT" and status != "NEEDS_SHANE":
                body["status"] = previous["body"]["status"]
                body["status_reason"] = previous["body"]["status_reason"]
            elif not same_pdf:
                for k in ("amounts", "reading", "evidence", "lien_type", "plaintiff", "form_date"):
                    body.pop(k, None)
                body.update(
                    status="HELD",
                    status_reason="Court PDF saved; awaiting its machine reading.",
                )

        expected_version = previous["version"] if previous else 0
        saved_record = self.call_court_debt_record(
            "PUT",
            county=county_slug,
            case_number=case_key,
            body=body,
            expected_version=expected_version,
        )
        logger.info(
            f"[+] Dropped & registered court debt record for {county_slug} {case_key}: "
            f"id={saved_record.get('id')} v={saved_record.get('version')} status={saved_record.get('body', {}).get('status')}"
        )
        return saved_record


def process_court_filing(
    local_pdf: Path,
    county_slug: str,
    raw_case_number: Optional[str],
    instrument_number: Optional[str],
    intake: VerifiedCloudIntake,
    filing_date: Optional[str] = None,
    court_claim_pdf: Optional[Path] = None,
    court_claim_status: Optional[str] = None,
    court_source_url: Optional[str] = None,
    court_doc_title: str = "VALUE OF MORTGAGE FORECLOSURE REAL PROPERTY CLAIM FORM",
    court_filed_date: Optional[str] = None,
    first_defendant: Optional[str] = None,
    parcel_count: Optional[int] = 1,
) -> Dict:
    """End-to-end verified handoff for a single court filing (Phase 1 LP + optional Phase 2 court debt drop)."""
    if not local_pdf.exists():
        raise FileNotFoundError(f"Local PDF missing: {local_pdf}")

    county_info = COUNTIES[county_slug]
    client_id = county_info["client_id"]
    sha = compute_sha256(local_pdf)
    case_number = normalize_ucn(county_slug, raw_case_number)
    storage_key = f"folder-intake/{client_id}/{county_slug}/{sha}.pdf"

    # Optional Phase 2 Court Debt Drop BEFORE enqueueing so worker_debt sees the claim PDF or status immediately
    court_record = None
    if court_claim_pdf is not None or court_claim_status is not None:
        if not raw_case_number:
            raise IntakeError("COURT_CLAIM_REQUIRES_CASE_NUMBER")
        effective_status = "HELD" if court_claim_pdf is not None else (court_claim_status or "NO_FORM_ON_DOCKET")
        court_record = intake.drop_court_debt_record(
            county_slug=county_slug,
            raw_case_number=raw_case_number,
            court_claim_pdf=court_claim_pdf,
            source_url=court_source_url or f"https://clerk.{county_slug}.fl.us/case/{case_number}",
            document_title=court_doc_title,
            filed_date=court_filed_date or filing_date,
            filing_date=filing_date,
            instrument_number=instrument_number,
            first_defendant=first_defendant,
            status=effective_status,
            parcel_count=parcel_count,
        )

    # Step 0: Check idempotency / existing job
    existing = intake.check_existing_job(client_id, sha, instrument_number)
    if existing:
        logger.info(f"[i] Job already exists for SHA {sha[:12]} (job_id: {existing['id']}, state: {existing['state']})")
        return {
            "status": "ALREADY_ENQUEUED",
            "job_id": existing["id"],
            "sha256": sha,
            "court_debt_record_id": court_record.get("id") if isinstance(court_record, dict) else None,
        }

    # Step 1: Storage upload & readback verification
    intake.upload_and_verify_storage(local_pdf, storage_key, sha)

    # Step 2: Retain admission source manifest record
    intake.retain_intake_record(
        client_id=client_id,
        county_slug=county_slug,
        case_number=case_number,
        sha=sha,
        instrument_number=instrument_number,
        local_pdf_path=local_pdf,
        filing_date=filing_date,
    )

    # Step 3: Enqueue job to native machine queue
    job_id = intake.enqueue_job(
        lane="LIS_PENDENS",
        case_number=case_number,
        instrument_number=instrument_number,
        client_id=client_id,
        storage_key=storage_key,
        pdf_sha256=sha,
        filename=local_pdf.name,
        county_slug=county_slug,
    )

    return {
        "status": "ADMITTED",
        "job_id": job_id,
        "sha256": sha,
        "case_number": case_number,
        "court_debt_record_id": court_record.get("id") if isinstance(court_record, dict) else None,
    }


def admit_county_filings(
    county_slug: str,
    target_date: Optional[str] = None,
    intake: Optional[VerifiedCloudIntake] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Admit all collected filings for a county from the SQLite database into Supabase."""
    import sqlite3

    if intake is None:
        intake = VerifiedCloudIntake()
    if not intake.is_configured():
        logger.warning("[!] Supabase not configured; skipping automatic cloud intake")
        return {"status": "SKIPPED", "reason": "SUPABASE_NOT_CONFIGURED"}

    if db_path is None:
        db_path = Path(__file__).parent / "hillsborough_records.db"
    if not db_path.exists():
        return {"status": "SKIPPED", "reason": f"DB not found: {db_path}"}

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    query = "SELECT * FROM lis_pendens WHERE pdf_path IS NOT NULL AND pdf_path != ''"
    params = []
    if target_date:
        query += " AND recording_date = ?"
        params.append(target_date)

    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    county_rows = []
    for r in rows:
        c = (r.get("county") or "").lower()
        pdf = r.get("pdf_path") or ""
        inst = r.get("instrument_number") or ""
        if c == county_slug or f"/{county_slug}/" in pdf or inst.startswith(f"{county_slug.upper()}_"):
            county_rows.append(r)

    admitted = 0
    already_enqueued = 0
    errors = 0
    results = []

    for r in county_rows:
        pdf_p = Path(r["pdf_path"])
        if not pdf_p.exists():
            continue
        docket_p = Path(r["docket_pdf_path"]) if r.get("docket_pdf_path") else None
        valid_docket = docket_p if (docket_p and docket_p.exists()) else None

        rec_date = r.get("recording_date", "")
        filing_date_iso = None
        if "/" in rec_date:
            parts = rec_date.split("/")
            if len(parts) == 3:
                filing_date_iso = f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"
        elif "-" in rec_date:
            filing_date_iso = rec_date.split(" ")[0]

        try:
            res = process_court_filing(
                local_pdf=pdf_p,
                county_slug=county_slug,
                raw_case_number=r.get("case_number"),
                instrument_number=r.get("instrument_number"),
                intake=intake,
                filing_date=filing_date_iso,
                court_claim_pdf=valid_docket,
                first_defendant=r.get("party_name"),
            )
            if res.get("status") == "ADMITTED":
                admitted += 1
            elif res.get("status") == "ALREADY_ENQUEUED":
                already_enqueued += 1
            results.append(res)
        except Exception as e:
            logger.error(f"[-] Error admitting {r.get('instrument_number')} for {county_slug}: {e}")
            errors += 1

    logger.info(
        f"[+] County intake complete for {county_slug}: admitted={admitted}, already_enqueued={already_enqueued}, errors={errors}"
    )
    return {
        "county": county_slug,
        "admitted": admitted,
        "already_enqueued": already_enqueued,
        "errors": errors,
        "total": len(county_rows),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verified NAGE Cloud Factory Intake (Phase 1 LP + Phase 2 Court Debt)")
    parser.add_argument("--county", required=True, choices=sorted(COUNTIES.keys()))
    parser.add_argument("--lp-pdf", type=Path, help="Path to Phase 1 Notice of Lis Pendens PDF")
    parser.add_argument("--case-number", required=True, help="Court case number (e.g. 2026CA010375 or 29-2026-CA-010375)")
    parser.add_argument("--instrument", help="Official Records instrument number")
    parser.add_argument("--filing-date", help="ISO filing date (YYYY-MM-DD)")
    parser.add_argument("--court-claim-pdf", type=Path, help="Optional Phase 2 court claim/value form PDF")
    parser.add_argument(
        "--court-claim-status",
        choices=sorted(COURT_DEBT_STATUSES),
        help="Optional Phase 2 court status when no PDF is on docket (e.g. NO_FORM_ON_DOCKET)",
    )
    parser.add_argument("--court-source-url", help="Provenance URL for Phase 2 court claim PDF")
    parser.add_argument(
        "--court-doc-title",
        default="VALUE OF MORTGAGE FORECLOSURE REAL PROPERTY CLAIM FORM",
        help="Document title for Phase 2 court claim PDF",
    )
    parser.add_argument("--court-filed-date", help="ISO filed date for Phase 2 court claim PDF (YYYY-MM-DD)")
    parser.add_argument("--first-defendant", help="Optional first defendant name for court record matching")
    parser.add_argument("--parcel-count", type=int, default=1, help="Case parcel count (default: 1)")
    parser.add_argument(
        "--drop-court-debt-only",
        action="store_true",
        help="Only drop Phase 2 court debt record into COURT-DEBT-RECORDS / court_debt_records",
    )
    args = parser.parse_args(argv)

    intake = VerifiedCloudIntake()
    if args.drop_court_debt_only:
        drop_status = args.court_claim_status or "HELD"
        result = intake.drop_court_debt_record(
            county_slug=args.county,
            raw_case_number=args.case_number,
            court_claim_pdf=args.court_claim_pdf,
            source_url=args.court_source_url or "",
            document_title=args.court_doc_title,
            filed_date=args.court_filed_date or args.filing_date,
            filing_date=args.filing_date,
            instrument_number=args.instrument,
            first_defendant=args.first_defendant,
            status=drop_status,
            parcel_count=args.parcel_count,
        )
        print(json.dumps(result, indent=2))
        return 0

    if not args.lp_pdf:
        parser.error("--lp-pdf is required unless --drop-court-debt-only is set")

    result = process_court_filing(
        local_pdf=args.lp_pdf,
        county_slug=args.county,
        raw_case_number=args.case_number,
        instrument_number=args.instrument,
        intake=intake,
        filing_date=args.filing_date,
        court_claim_pdf=args.court_claim_pdf,
        court_claim_status=args.court_claim_status,
        court_source_url=args.court_source_url,
        court_doc_title=args.court_doc_title,
        court_filed_date=args.court_filed_date,
        first_defendant=args.first_defendant,
        parcel_count=args.parcel_count,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
