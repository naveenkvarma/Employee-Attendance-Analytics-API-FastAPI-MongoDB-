# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, manual regularization audit trails, and attendance analytics built with MongoDB aggregation pipelines.

## Requirements & Stack
- **Python**: 3.11+
- **MongoDB**: 6.0 or newer (tested with MongoDB 7.0/8.0)
- **Dependencies**: `fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`

## Setup & Running

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables**:
   Set `MONGO_URI` and `MONGO_DB` in your environment or in a local `.env` file (real environment variables take priority):
   - `MONGO_URI`: `mongodb://localhost:27017` (default)
   - `MONGO_DB`: `attendance_db` (default)

3. **Seed Sample Data (Optional)**:
   ```bash
   python sample_seed.py
   ```

4. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive API documentation will be available at `http://localhost:8000/docs`.

## Completed Deliverables
- [app/main.py](file:///c:/Users/VARMA/Downloads/candidate_kit/app/main.py): Complete implementation of all endpoints and business rules (R1 - R10) inside a single file. Indexes are initialized idempotently on startup.
- [REVIEW.md](file:///c:/Users/VARMA/Downloads/candidate_kit/REVIEW.md): Detailed analysis and fixes for 18 defects identified in the starter code, along with justifications for non-defects.
- [DECISIONS.md](file:///c:/Users/VARMA/Downloads/candidate_kit/DECISIONS.md): Architectural decisions covering index design, punch-in race concurrency, leaderboard tie-breaking, headcount calculation with zero logs, and 100x scalability.
- [requirements.txt](file:///c:/Users/VARMA/Downloads/candidate_kit/requirements.txt): Pinned/minimum dependency specifications.

## Key Features & Business Rules Implemented
- **R1 Attendance Date & Overnight Shifts**: Converted to IST; overnight shifts (`shift_end <= shift_start`) assign punch-ins prior to `shift_end` to the previous calendar day. Instants are truncated to whole seconds.
- **R2 Late Grace Period**: Strict 10-minute grace window (>600s after shift start); late minutes calculated as floor minutes elapsed since shift start.
- **R3 Overtime**: Minimum 30-minute threshold past shift end (works across midnight for overnight shifts).
- **R4 Work Hours**: Computed elapsed hours rounded to 2 decimal places half-up using `Decimal` arithmetic.
- **R5 Half Day**: Flagged when rounded work hours < 4.50 (counts as 0.5 present day).
- **R6 Presence & R7 Working Days**: Mon-Fri working day counting (accounting for mid-month joiners from `joined_on`); weekend logs add to late/overtime totals without inflating present days.
- **R8 Rounding**: Strict round-half-up formatting (2 decimals for reported values, 4 decimals for rates).
- **R9 Headcount & Gap-Filling**: Cross-collection department summaries count employees with zero logs; department trends use `$densify` for gap filling across all calendar days and compute 7-day moving averages.
- **Concurrency & Audit Trail**: Database unique indexes prevent concurrent punch-in races; optimistic locking prevents lost history entries on concurrent regularizations.
- **Explain Endpoint**: `GET /admin/explain/{endpoint}` validates index-backed queries (`IXSCAN`) and guarantees zero collection scans (`COLLSCAN`).
