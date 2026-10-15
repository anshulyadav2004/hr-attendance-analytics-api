# REVIEW.md

List every defect found in the starter `app/main.py`:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `MongoClient` initialization | Initialized without `tz_aware=True`, causing naive datetime returns from PyMongo. | Timezone misalignment between UTC BSON datetimes and IST calculations. | Passed `tz_aware=True` to `MongoClient` and normalized boundaries. |
| 2 | `health()` endpoint | Returns `200` unconditionally without verifying MongoDB connectivity. | `/health` returns `200` even when MongoDB is offline. | Added a `db.command("ping")` check returning `503` if MongoDB fails. |
| 3 | `create_employee()` | Lacked validation for matching `shift_start` and `shift_end`; returned python datetime for `created_at`. | Successful creation with identical shift times; schema validation failure on `created_at`. | Added check for `shift_start == shift_end` (422) and serialized `created_at` to epoch milliseconds. |
| 4 | `list_employees()` | Incorrect `skip` formula (`page * page_size`), untracked department counts, and missing sorting. | Skipping first page incorrectly, inflated totals, and unsorted employee listings. | Fixed `skip = (page - 1) * page_size`, applied department filter to counting, and added `.sort("emp_code", 1)`. |
| 5 | `punch_in()` | Race-condition prone; ignored overnight shifts and truncation; returned raw datetimes and injected an unauthorized `id` field. | Parallel requests creating duplicates; overnight shift date misalignments; validation errors on response. | Added unique compound index on `(emp_code, date)`, implemented R1 date resolution and time truncation, and removed `id`. |
| 6 | `list_attendance()` | Loaded entire collection into memory and sorted in Python; injected `id`. | Severe performance degradation on large datasets and schema violations. | Replaced in-memory lists with cursor `.sort()`, `.skip()`, and `.limit()`. |
| 7 | `compute_late_minutes()` | Checked `minutes > 10` instead of strict `> 10:00` offset rule from `shift_start`. | Incorrect late minutes calculation for employees arriving right at grace boundaries. | Rewrote to strictly evaluate whole minutes elapsed past `shift_start` when exceeding 10 minutes. |
| 8 | `compute_overtime()` | Missing IST timezone context and failed to enforce the 30-minute threshold rule (R3). | Overtime recorded for durations under 30 minutes. | Implemented proper IST shift-end comparison enforcing the $\ge 30$ min threshold. |

Also examined and decided **not** a defect:
* **Environment loading (`load_dotenv`)**: Correctly handles local development while respecting real environment variables.
* **FastAPI setup**: The base application framework layout is clean and appropriate.