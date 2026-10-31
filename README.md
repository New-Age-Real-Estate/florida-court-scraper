# Hillsborough County Lis Pendens & Court Docket Scraper

Automated pipeline powered by **Bright Data Scraping Browser** and **Playwright (Python)** to extract daily Lis Pendens (LP) filings and associated court dockets & debt amounts from Hillsborough County, Florida.

---

## Architecture Overview

1. **Stage 1 (`stage1_ori.py`)**:
   - Navigates to [Hillsborough Official Records Search (ORI)](https://publicaccess.hillsclerk.com/oripublicaccess/).
   - Selects Document Type `(LP) LIS PENDENS`.
   - Filters by Recording Date (default: yesterday).
   - Iterates through the results table, opens each document popup, and downloads the PDF formatted as:
     `Hillsborough_LP_[recording-date]_[instrument-number].pdf`.
   - Automatically parses Page 1 of the PDF using `pdfplumber` to extract the `Case No.`.
   - Saves records into a local SQLite database (`hillsborough_records.db`) with deduplication.

2. **Stage 2 (`stage2_hover.py`)**:
   - Takes extracted Case Numbers from Stage 1.
   - Navigates to [Hillsborough HOVER Court Search](https://hover.hillsclerk.com/html/case/caseSearch.html).
   - Looks up case details and extracts the **Claim / Debt Amount**.
   - Downloads the initial Complaint / Lis Pendens court docket filing.
   - Updates the database and generates a daily CSV export.

---

## Quick Setup

### 1. Install Dependencies
```bash
cd /Users/Breezy/.gemini/antigravity/scratch/hillsborough-court-scraper
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Bright Data Credentials
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Open `.env` and fill in your Bright Data Scraping Browser WebSocket URL:
```env
BRIGHT_DATA_WS_ENDPOINT=wss://brd-customer-<YOUR_CUSTOMER_ID>-zone-<YOUR_ZONE>:<YOUR_PASSWORD>@brd.superproxy.io:9222
```

### 3. Test Connection
Run the connection test script to confirm Bright Data can reach the portal:
```bash
python3 test_connection.py
```
This will take a diagnostic screenshot and save it to `./debug_screenshots/test_hillsclerk.png`.

---

## Running the Pipeline

### Run Full Daily Collection (Yesterday's filings):
```bash
python3 run_daily.py
```

### Run for a Specific Date:
```bash
python3 run_daily.py --date 10/25/2024
```

### Run for Today's Filings:
```bash
python3 run_daily.py --today
```

### Run Only Stage 1 (Official Records without HOVER docket search):
```bash
python3 run_daily.py --skip-hover
```

---

## Automated Daily Scheduling (Cron)

To run this automatically every morning at 6:00 AM (Monday through Friday):

1. Open your user crontab:
   ```bash
   crontab -e
   ```
2. Add the following line (adjusting paths as needed):
   ```cron
   0 6 * * 1-5 cd /Users/Breezy/.gemini/antigravity/scratch/hillsborough-court-scraper && /usr/bin/python3 run_daily.py >> scraper.log 2>&1
   ```

---

## Output Data Structure

* **Downloaded Lis Pendens PDFs:** `./downloads/lis_pendens/Hillsborough_LP_[date]_[inst#].pdf`
* **Downloaded Court Docket PDFs:** `./downloads/court_dockets/HOVER_Docket_[case#]_Complaint.pdf`
* **SQLite Database:** `hillsborough_records.db`
* **Daily Export CSV:** `hillsborough_report_[date].csv`
