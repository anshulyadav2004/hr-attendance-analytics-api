"""
Employee Attendance & Analytics API - Completed Solution
Stack: Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup Index Creation (Idempotent)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def configure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R5, R8)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def round_half_up(val: float, decimals: int = 2) -> float:
    d = Decimal(str(val))
    quant_str = "0." + "0" * decimals if decimals > 0 else "1"
    return float(d.quantize(Decimal(quant_str), rounding=ROUND_HALF_UP))


def resolve_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    # R1: IST calendar date of punch_in, handling overnight shifts (shift_end <= shift_start)
    dt_ist = punch_in_utc.astimezone(IST)
    if shift_end_str <= shift_start_str:
        sh, sm = map(int, shift_start_str.split(":"))
        eh, em = map(int, shift_end_str.split(":"))
        # If punch-in IST time is earlier than shift_end, it belongs to the previous day's shift
        time_minutes = dt_ist.hour * 60 + dt_ist.minute
        end_minutes = eh * 60 + em
        if time_minutes < end_minutes:
            dt_ist = dt_ist - timedelta(days=1)
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start_str: str, att_date_str: str) -> int:
    # R2: Grace period 10 minutes. Late if strictly > 10:00 after shift_start.
    sh, sm = map(int, shift_start_str.split(":"))
    att_date = datetime.fromisoformat(att_date_str)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, sh, sm, tzinfo=IST)
    
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    diff_minutes = int(diff_seconds // 60)
    
    if diff_minutes > 10:
        return diff_minutes
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up
    secs = (punch_out - punch_in).total_seconds()
    return round_half_up(secs / 3600, 2)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end_str: str, shift_start_str: str, att_date_str: str) -> int:
    # R3: whole minutes (floor) from shift_end to punch_out, >= 30 else 0
    eh, em = map(int, shift_end_str.split(":"))
    att_date = datetime.fromisoformat(att_date_str)
    shift_end_dt = datetime(att_date.year, att_date.month, att_date.day, eh, em, tzinfo=IST)
    
    if shift_end_str <= shift_start_str:
        shift_end_dt += timedelta(days=1)
        
    diff_seconds = (punch_out_utc.astimezone(IST) - shift_end_dt).total_seconds()
    diff_minutes = int(diff_seconds // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    # R5: half_day = true when rounded work_hours < 4.50
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Pydantic Models & Validation
# --------------------------------------------------------------------------- #
class EpochMillisField(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or v < 100000000000 or v > 4102444800000:
                raise HTTPException(422, "Invalid epoch milliseconds timestamp")
        return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_ts(cls, v):
        if v is not None and (not isinstance(v, int) or v < 100000000000 or v > 4102444800000):
            raise HTTPException(422, "Invalid punched_at epoch ms")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ["PRESENT", "WFH", "ON_DUTY"]:
            raise HTTPException(422, "Invalid status for punch-in")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_ts(cls, v):
        if v is not None and (not isinstance(v, int) or v < 100000000000 or v > 4102444800000):
            raise HTTPException(422, "Invalid punched_at epoch ms")
        return v


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Response Serializer Helper
# --------------------------------------------------------------------------- #
def serialize_log(doc: dict) -> dict:
    if not doc:
        return doc
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_epoch_ms(doc.get("punch_out"))
    if "history" in doc:
        for h in doc["history"]:
            h["at"] = dt_to_epoch_ms(h.get("at"))
            if "changes" in h:
                for field, change in h["changes"].items():
                    if field in ["punch_in", "punch_out"]:
                        if isinstance(change.get("from"), datetime):
                            change["from"] = dt_to_epoch_ms(change["from"])
                        if isinstance(change.get("to"), datetime):
                            change["to"] = dt_to_epoch_ms(change["to"])
    return doc


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unreachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start cannot equal shift_end")
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    
    now_dt = datetime.now(timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = now_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
        
    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch_ms(now_dt)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "Invalid pagination parameters")
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for it in items:
        it["created_at"] = dt_to_epoch_ms(it.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    raw_ts = body.punched_at if body.punched_at else int(datetime.now(timezone.utc).timestamp() * 1000)
    ts = truncate_to_seconds(epoch_ms_to_dt(raw_ts))
    
    att_date = resolve_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    
    # Check duplicate or race insertion via unique compound index (emp_code, date)
    existing = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date})
    if existing:
        raise HTTPException(409, "Already punched in for this date")
        
    late_mins = compute_late_minutes(ts, emp["shift_start"], att_date)
    
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
        
    return serialize_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    raw_ts = body.punched_at if body.punched_at else int(datetime.now(timezone.utc).timestamp() * 1000)
    out_ts = truncate_to_seconds(epoch_ms_to_dt(raw_ts))
    
    # Find most recent open record for employee where punch_in <= out_ts and punch_out is null
    # Must account for overnight shifts (up to 24h span)
    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_out": None,
        "punch_in": {"$lte": out_ts}
    }, sort=[("punch_in", DESCENDING)])
    
    if not record:
        raise HTTPException(404, "No active punch-in found")
        
    p_in = record["punch_in"]
    diff_secs = (out_ts - p_in).total_seconds()
    if diff_secs <= 0 or diff_secs > 86400:
        raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")
        
    work_hrs = compute_work_hours(p_in, out_ts)
    overtime = compute_overtime_minutes(out_ts, emp["shift_end"], emp["shift_start"], record["date"])
    half_d = compute_half_day(work_hrs)
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_ts,
                "work_hours": work_hrs,
                "overtime_minutes": overtime,
                "half_day": half_d
            }
        },
        return_document=True
    )
    
    if not res:
        raise HTTPException(409, "Record already punched out")
        
    return serialize_log(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "Invalid pagination parameters")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")
    if status:
        q["status"] = status
        
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
    cursor.skip((page - 1) * page_size).limit(page_size)
    
    items = [serialize_log(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for this date")
        
    # Validation rules
    new_status = body.status if body.status is not None else record["status"]
    
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times for ABSENT or LEAVE status")
        new_punch_in = None
        new_punch_out = None
    else:
        if body.punch_in is not None:
            new_punch_in = truncate_to_seconds(epoch_ms_to_dt(body.punch_in))
            # R1 check: punch_in must stay on record's attendance date
            resolved_d = resolve_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
            if resolved_d != date:
                raise HTTPException(422, "punch_in must belong to the record's attendance date")
        else:
            new_punch_in = record.get("punch_in")
            
        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")
            
        if body.punch_out is not None:
            new_punch_out = truncate_to_seconds(epoch_ms_to_dt(body.punch_out))
        else:
            new_punch_out = record.get("punch_out")
            
        if new_punch_out is not None:
            diff_secs = (new_punch_out - new_punch_in).total_seconds()
            if diff_secs <= 0 or diff_secs > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields
    if new_status in ["ABSENT", "LEAVE"]:
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out, emp["shift_end"], emp["shift_start"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check if anything actually changed
    changes = {}
    field_comparisons = {
        "status": (record.get("status"), new_status),
        "punch_in": (record.get("punch_in"), new_punch_in),
        "punch_out": (record.get("punch_out"), new_punch_out),
        "work_hours": (record.get("work_hours"), new_work_hours),
        "late_minutes": (record.get("late_minutes", 0), new_late_minutes),
        "overtime_minutes": (record.get("overtime_minutes", 0), new_overtime_minutes),
        "half_day": (record.get("half_day", False), new_half_day),
    }

    for k, (old_val, new_val) in field_comparisons.items():
        if old_val != new_val:
            changes[k] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=True
    )

    return serialize_log(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    try:
        y, m = map(int, month.split("-"))
    except ValueError:
        raise HTTPException(422, "Invalid month format, use YYYY-MM")

    start_date = f"{month}-01"
    if m == 12:
        end_date = f"{y+1}-01-01"
    else:
        end_date = f"{y}-{m+1:02d}-01"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lt": end_date}
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0
                        ]
                    }
                }
            }
        }
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    data = res[0] if res else {
        "leave_days": 0, "late_count": 0, "total_late_minutes": 0,
        "total_overtime_minutes": 0, "present_days": 0.0
    }

    # Compute working days (Mon-Fri from joined_on)
    joined = emp["joined_on"]
    cur = datetime.fromisoformat(start_date)
    last = datetime.fromisoformat(end_date) - timedelta(days=1)
    
    working_days = 0
    while cur <= last:
        # Check if cur >= joined_on
        if cur.date().isoformat() >= joined:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
        cur += timedelta(days=1)

    present_days = data["present_days"]
    att_pct = round_half_up((present_days / working_days) * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": att_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    try:
        y, m = map(int, month.split("-"))
    except ValueError:
        raise HTTPException(422, "Invalid month format, use YYYY-MM")

    start_date = f"{month}-01"
    if m == 12:
        end_date = f"{y+1}-01-01"
    else:
        end_date = f"{y}-{m+1:02d}-01"

    last_day_of_month = (datetime.fromisoformat(end_date) - timedelta(days=1)).date().isoformat()

    emp_match = {"joined_on": {"$lte": last_day_of_month}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ecode"]},
                            "date": {"$gte": start_date, "$lt": end_date}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "_id": 0
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]

    results = list(db.employees.aggregate(pipeline))
    for r in results:
        if r["avg_work_hours"] is not None:
            r["avg_work_hours"] = round_half_up(r["avg_work_hours"], 2)

    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    try:
        y, m = map(int, month.split("-"))
    except ValueError:
        raise HTTPException(422, "Invalid month format")

    start_date = f"{month}-01"
    if m == 12:
        end_date = f"{y+1}-01-01"
    else:
        end_date = f"{y}-{m+1:02d}-01"

    emp_match = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ecode"]},
                            "date": {"$gte": start_date, "$lt": end_date},
                            "late_minutes": {"$gt": 0}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "late_count": {"$size": "$logs"}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING},
                "output": {
                    "rank": {"$denseRank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0}}
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(..., alias="to")):
    # Verify department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    try:
        start_dt = datetime.fromisoformat(from_)
        end_dt = datetime.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, "Invalid date format, use YYYY-MM-DD")

    if end_dt < start_dt:
        raise HTTPException(422, "to date cannot be earlier than from date")
    if (end_dt - start_dt).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    pipeline = [
        {
            "$match": {
                "department": department,
                "joined_on": {"$lte": to}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ecode"]},
                            "date": {"$gte": from_, "$lte": to}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "active_emps": {"$addToSet": "$emp_code"}
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_, to]
                }
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$curr_date"]}
                        }
                    },
                    {"$count": "hc"}
                ],
                "as": "hc_info"
            }
        },
        {
            "$project": {
                "date": "$_id",
                "is_working_day": {
                    # Day of week in python: 0=Mon, 5=Sat, 6=Sun. In Mongo $dayOfWeek: 1=Sun, 2=Mon...6=Fri, 7=Sat
                    "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, [2, 3, 4, 5, 6]]
                },
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc_info.hc", 0]}, 0]
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4
                            ]
                        },
                        {
                            "$cond": [
                                {"$not": ["$is_working_day"]},
                                None,
                                0.0
                            ]
                        }
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"range": [-6, "current"], "unit": "day"}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None
                    ]
                }
            }
        }
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explain_res = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        y, m = map(int, month.split("-"))
        start_date = f"{month}-01"
        end_date = f"{y+1}-01-01" if m == 12 else f"{y}-{m+1:02d}-01"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lt": end_date}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}}
        ]
        explain_res = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        y, m = map(int, month.split("-"))
        start_date = f"{month}-01"
        end_date = f"{y+1}-01-01" if m == 12 else f"{y}-{m+1:02d}-01"
        last_day = (datetime.fromisoformat(end_date) - timedelta(days=1)).date().isoformat()
        emp_match = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$group": {"_id": "$department", "hc": {"$sum": 1}}}
        ]
        explain_res = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        y, m = map(int, month.split("-"))
        start_date = f"{month}-01"
        end_date = f"{y+1}-01-01" if m == 12 else f"{y}-{m+1:02d}-01"
        emp_match = {}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$project": {"emp_code": 1}}
        ]
        explain_res = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to required")
        pipeline = [
            {"$match": {"department": department, "joined_on": {"$lte": to}}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$densify": {"field": "joined_on", "range": {"step": 1, "unit": "day", "bounds": [from_, to]}}}
        ]
        try:
            explain_res = db.employees.aggregate(pipeline, explain=True)
        except Exception as e:
            explain_res = {"note": str(e)}
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    else:
        raise HTTPException(422, "Unknown explain endpoint")