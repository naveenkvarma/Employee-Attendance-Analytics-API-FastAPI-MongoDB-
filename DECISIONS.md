# DECISIONS.md

1. **Indexes.**
   - `employees: {"emp_code": 1}` (unique): guarantees business code uniqueness and backs O(1) employee lookups.
   - `attendance_logs: {"emp_code": 1, "date": 1}` (unique): enforces the single-record-per-day constraint and serves race-safe punch-ins, regularizations, and monthly summaries.
   - `attendance_logs: {"date": -1, "emp_code": 1}`: powers fast paginated listing on 100k+ records with index-backed multi-key sorting.
   - `attendance_logs: {"date": 1, "late_minutes": 1}`: eliminates collection scans for leaderboard filtering.
   - `employees: {"joined_on": 1, "department": 1}` and `{"department": 1, "joined_on": 1}`: optimizes department filtering and headcount range scans.
   - *Rejected*: An index on `attendance_logs.history` because history is an append-only audit trail never queried by keys; also rejected a single-field index on `date` since `(date, emp_code)` covers prefix queries.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt an `insert_one`. The database-level unique compound index on `(emp_code, date)` acts as the race boundary: one insert commits (returning 201), while the other throws a MongoDB `DuplicateKeyError` (code 11000). The exception is caught and returns 409, guaranteeing zero duplicate records without racy check-then-insert logic.

3. **Ties.**
   The leaderboard uses standard competition ranking (`$setWindowFields` with `$rank: {}`), so tied employees share a rank and subsequent ranks are skipped (e.g., 1, 2, 2, 4). The `limit` filter is applied on `rank <= limit`. Any employee tied at or below the cutoff rank is returned, even if total items exceed `limit`. Ties are ordered by `emp_code` ascending.

4. **Headcount.**
   The department summary aggregation starts from `employees` (filtering `joined_on <= last_day_of_month`), then performs a `$lookup` into `attendance_logs` and unwinds with `preserveNullAndEmptyArrays: True`. Headcount is computed as `$size` of an `$addToSet: "$emp_code"`, ensuring employees with zero logs are accurately counted.

5. **One thing you would change for 100x data.**
   Maintain daily and monthly pre-aggregated rollups via background change streams. Computing moving averages, densified date gaps, and multi-collection lookups across tens of millions of raw documents on every request will saturate database CPU; incremental rollups turn heavy aggregations into instant O(1) index queries.
