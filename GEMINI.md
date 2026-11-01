# Florida Court Records Scraper & Lead Ingestion Machine

This directory contains the automated multi-county Florida court scrapers and verified intake bridge for NAGE.

## County Coverage & Architecture
1. **Hillsborough (29):** `stage1_ori.py` (Official Records Lis Pendens), `stage2_hover.py` (HOVER Court Docket & Debt Form).
2. **Broward (06):** `broward_stage1.py` (AcclaimWeb Lis Pendens), `broward_stage2.py` (Broward Clerk eClerk Case Search & Complaint PDF).
3. **Orange (48):** `orange_stage1.py` (Official Records), `orange_stage2_myeclerk.py` (myEClerk Docket).
4. **Duval (16):** `duval_stage1.py` (Official Records), `duval_stage2.py` (CORE Court Docket).
5. **Sarasota (58):** `sarasota_stage1.py` (Official Records), `sarasota_stage2.py` (ClerkNet Docket).
6. **Manatee (41):** `manatee_stage1.py` (Official Records), `manatee_stage2.py` (MCCCC Hub Docket).

## Orchestration
- **Daily Runner:** `python run_daily.py --county <name> --today` (or `--date MM/DD/YYYY`).
- **Verified Intake Bridge:** `python cloud_factory_intake.py` admits filings directly into Supabase `court_debt_records` and `document_processing_jobs`.
- **Worker Runtime:** Active locally in background at `~/nage-runtime/document-worker/repo`.
- **S3 Data Lake:** `s3://florida-court-records-machine/`.

## Non-Negotiable Invariants
- **R1:** Zero wrong parcels (deterministic resolution against FDOR rolls).
- **R10:** Court debt is strictly court-stated from official filings. Never inject third-party amounts into intake receipts.
- **Approval Boundary:** Pipeline stops at `READY_FOR_APPROVAL` for Shane's review. Never auto-publish.
