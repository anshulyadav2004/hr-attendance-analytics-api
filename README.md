# Employee Attendance API

FastAPI service for employee records, attendance punch events, regularizations and monthly/daily analytics. The API
contract and business rules are in [openapi.yaml](./openapi.yaml); MongoDB document shapes are in [DATA_MODEL.md](./DATA_MODEL.md).

## Run locally

Requires Python 3.11+ and MongoDB 6.0+. Install dependencies, start MongoDB, then run:

```sh
python -m pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in the environment before starting the service. Defaults are `mongodb://localhost:27017`
and `attendance_db`. The sample seed script replaces the two sample collections, so use it only with a disposable local
database.

`GET /health` checks MongoDB readiness. The service creates its required indexes on startup.
