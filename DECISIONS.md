# DECISIONS.md

1. **Indexes.**
   Created unique indexes on `employees.emp_code` (for identity enforcement) and `attendance_logs.(emp_code, date)` to enforce idempotency and prevent duplicate punches. Added single-field indexes on `employees.department` and `attendance_logs.date` to support fast lookups in analytics pipelines and list endpoints. Considered creating a compound index on `(department, date)` inside `attendance_logs`, but rejected it since department belongs to the employee collection and cross-collection queries are efficiently routed via index scans on `emp_code`.

2. **Punch-in race.**
   When two identical punch-in requests hit the endpoint simultaneously, both attempt an insertion into MongoDB. The unique compound index on `(emp_code, date)` acts as the concurrency safeguard: the first request succeeds and returns `201 Created`, while the secondary request triggers a `DuplicateKeyError` (E11000) from MongoDB, which is caught and converted to a `409 Conflict` HTTP exception.

3. **Ties.**
   The late leaderboard sorts employees strictly by `total_late_minutes` descending, followed by `emp_code` ascending as an explicit deterministic tie-breaker before applying the `$limit` stage. In case of a tie at the cutoff, the employee with the lexicographically smaller `emp_code` is returned, ensuring consistent pagination without non-deterministic order flips.

4. **Headcount.**
   The department summary pipeline queries the `employees` collection as the root (`$match: { joined_on: { $lte: target_date } }`), ensuring every eligible employee is represented regardless of log existence. It then uses a `$lookup` join against `attendance_logs` for the given date and calculates presence using `$cond` checks on the joined array size.

5. **One thing you would change for 100x data.**
   To handle 100x data volume, I would move from on-the-fly aggregation pipelines over raw attendance logs to pre-aggregated daily and monthly rollups (e.g., using scheduled worker tasks or MongoDB Change Streams) so that analytic reads execute instantly in $O(1)$ time rather than scanning millions of raw documents.