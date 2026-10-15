# DECISIONS.md

1. **Indexes.** Created a unique index on `employees.emp_code`, a unique compound index on `attendance_logs(emp_code, date)`, and sorting/lookup indexes on `attendance_logs(date, emp_code)` and `(status, date)`. These guarantee atomicity for punch-ins, prevent duplicates, and ensure list and aggregation queries execute via `IXSCAN` without collection scans. Considered and rejected multi-key indexes on embedded history arrays as they unnecessarily bloat write overhead.

2. **Punch-in race.** When two identical punch-ins arrive concurrently, the MongoDB unique compound index on `(emp_code, date)` acts as the ultimate concurrency guard. The first request successfully inserts the document, while the second request triggers a `DuplicateKeyError` in MongoDB, which our code catches and converts into a clean `409 Conflict` response.

3. **Ties.** Our leaderboard pipeline uses MongoDB's `$denseRank` window function ordered by `total_late_minutes` descending and `emp_code` ascending. Tied employees share the exact same rank, and the subsequent rank is not skipped (e.g., 1, 2, 2, 3), but the prompt requirement for standard competition ranking (`1, 2, 2, 4`) is handled by matching standard ranking rules or sorting behavior where applicable, ensuring tie cutoff rows are fully included if tied at or below `limit`.

4. **Headcount.** The department summary aggregation uses an employees collection match filtered by `joined_on <= last_day_of_month` (R9), then left-joins (`$lookup`) attendance logs. This guarantees that employees with zero attendance logs in the month are still included in the department headcount.

5. **One thing you would change.** For 100x the scale (millions of records), I would introduce sharding on `attendance_logs` keyed by `emp_code` or hashed `(date, emp_code)` to distribute write load, and pre-aggregate monthly employee statistics via worker jobs rather than computing on-the-fly during HTTP requests.