"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load local .env (real environment variables take priority)
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(ms: int) -> int:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return (ms // 1000) * 1000


def ms_to_dt_utc(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC timezone-aware datetime."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert MongoDB BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(val: Union[float, Decimal], decimals: int = 2) -> float:
    """R8: Reported numbers 2 decimals half-up (rates: 4 decimals half-up)."""
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30). A record's date is the
    IST calendar date of its punch_in. Overnight shifts (shift_end <= shift_start):
    a punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    trunc_ms = truncate_to_seconds(punch_in_ms)
    dt_utc = ms_to_dt_utc(trunc_ms)
    dt_ist = dt_utc.astimezone(IST)

    if shift_end <= shift_start:
        punch_time_str = dt_ist.strftime("%H:%M")
        if punch_time_str < shift_end:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: int, attendance_date_str: str, shift_start: str) -> int:
    """
    R2: Late grace period 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start.
    """
    trunc_ms = truncate_to_seconds(punch_in_ms)
    punch_in_ist = ms_to_dt_utc(trunc_ms).astimezone(IST)

    sh, sm = map(int, shift_start.split(":"))
    d_obj = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime.combine(d_obj, time(sh, sm), tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_ms: int, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out, counted only if
    that is at least 30; otherwise 0. Works across midnight for overnight shifts.
    """
    trunc_ms = truncate_to_seconds(punch_out_ms)
    punch_out_ist = ms_to_dt_utc(trunc_ms).astimezone(IST)

    eh, em = map(int, shift_end.split(":"))
    d_obj = date.fromisoformat(attendance_date_str)
    if shift_end <= shift_start:
        end_date = d_obj + timedelta(days=1)
    else:
        end_date = d_obj
    shift_end_dt = datetime.combine(end_date, time(eh, em), tzinfo=IST)

    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    ot_min = int(diff_sec // 60)
    return ot_min if ot_min >= 30 else 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: Work hours = (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_sec = (punch_out_ms // 1000) - (punch_in_ms // 1000)
    return float((Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day is true when the rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document into OpenAPI AttendanceRecord response."""
    history_formatted = []
    for h in doc.get("history", []):
        at_val = dt_to_ms(h.get("at")) if isinstance(h.get("at"), datetime) else h.get("at")
        changes_formatted = {}
        for k, v in h.get("changes", {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_v = dt_to_ms(from_v) if isinstance(from_v, datetime) else from_v
                to_v = dt_to_ms(to_v) if isinstance(to_v, datetime) else to_v
            changes_formatted[k] = {"from": from_v, "to": to_v}
        history_formatted.append({
            "at": at_val,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_formatted,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_formatted,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document into OpenAPI Employee response."""
    created_at = doc.get("created_at")
    created_at_ms = dt_to_ms(created_at) if isinstance(created_at, datetime) else created_at
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Lifespan: Database Index Creation at Startup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes exist idempotently
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unavailable: {exc}",
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Employee with this emp_code already exists",
        )

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    punched_at_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    trunc_ms = truncate_to_seconds(punched_at_ms)
    punch_in_dt = ms_to_dt_utc(trunc_ms)

    attendance_date = get_attendance_date(trunc_ms, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(trunc_ms, attendance_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    punched_at_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    trunc_ms = truncate_to_seconds(punched_at_ms)
    punch_out_dt = ms_to_dt_utc(trunc_ms)

    # Find employee's most recent record whose punch_in is <= punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )

    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in found",
        )

    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    candidate_in_dt = candidate["punch_in"]
    if candidate_in_dt.tzinfo is None:
        candidate_in_dt = candidate_in_dt.replace(tzinfo=timezone.utc)

    candidate_in_ms = int(candidate_in_dt.timestamp() * 1000)
    diff_sec = (trunc_ms // 1000) - (candidate_in_ms // 1000)

    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(candidate_in_ms, trunc_ms)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime_minutes(trunc_ms, candidate["date"], emp["shift_start"], emp["shift_end"])

    # Concurrency safe: only one simultaneous punch-out updates punch_out from None
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must not be greater than date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # Determine target status
    target_status = body.status if body.status is not None else rec.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Supplying punch_in or punch_out with ABSENT or LEAVE is not allowed",
            )
        final_punch_in = None
        final_punch_out = None
    else:  # Presence status (PRESENT, WFH, ON_DUTY)
        if body.punch_in is not None:
            trunc_in_ms = truncate_to_seconds(body.punch_in)
            final_punch_in = ms_to_dt_utc(trunc_in_ms)
        else:
            final_punch_in = rec.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )

        if body.punch_out is not None:
            trunc_out_ms = truncate_to_seconds(body.punch_out)
            final_punch_out = ms_to_dt_utc(trunc_out_ms)
        else:
            final_punch_out = rec.get("punch_out")

    # Normalize to timezone-aware UTC
    if final_punch_in is not None and final_punch_in.tzinfo is None:
        final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)
    if final_punch_out is not None and final_punch_out.tzinfo is None:
        final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

    # Validate punch_in stays on record's attendance date
    if final_punch_in is not None:
        in_ms = int(final_punch_in.timestamp() * 1000)
        computed_date = get_attendance_date(in_ms, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

    # Validate punch_out relative to punch_in
    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_out requires a punch_in",
            )
        diff_sec = (final_punch_out - final_punch_in).total_seconds()
        if diff_sec <= 0 or diff_sec > 86400:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        in_ms = int(final_punch_in.timestamp() * 1000)
        final_late_minutes = compute_late_minutes(in_ms, date, emp["shift_start"])
        if final_punch_out is not None:
            out_ms = int(final_punch_out.timestamp() * 1000)
            final_work_hours = compute_work_hours(in_ms, out_ms)
            final_half_day = compute_half_day(final_work_hours)
            final_overtime_minutes = compute_overtime_minutes(out_ms, date, emp["shift_start"], emp["shift_end"])
        else:
            final_work_hours = None
            final_half_day = False
            final_overtime_minutes = 0

    # Build changes audit trail
    changes: Dict[str, Dict[str, Any]] = {}

    def record_change(key: str, old_val: Any, new_val: Any):
        if key in ("punch_in", "punch_out"):
            if dt_to_ms(old_val) != dt_to_ms(new_val):
                changes[key] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                changes[key] = {"from": old_val, "to": new_val}

    record_change("status", rec.get("status"), target_status)
    record_change("punch_in", rec.get("punch_in"), final_punch_in)
    record_change("punch_out", rec.get("punch_out"), final_punch_out)
    record_change("work_hours", rec.get("work_hours"), final_work_hours)
    record_change("late_minutes", rec.get("late_minutes", 0), final_late_minutes)
    record_change("overtime_minutes", rec.get("overtime_minutes", 0), final_overtime_minutes)
    record_change("half_day", rec.get("half_day", False), final_half_day)

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request changed nothing",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking on history array length ensures concurrent regularizations don't lose audit trail
    current_history_len = len(rec.get("history", []))
    res = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": current_history_len}},
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent edit conflict; please retry",
        )

    return format_attendance_doc(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, mo = map(int, month.split("-"))
    last_day_num = calendar.monthrange(year, mo)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{last_day_num:02d}"

    # Calculate working_days (Mon-Fri) from max(first_day_of_month, joined_on) to last_day_of_month
    working_days = 0
    if emp["joined_on"] <= last_day_str:
        start_d = date.fromisoformat(max(first_day_str, emp["joined_on"]))
        end_d = date.fromisoformat(last_day_str)
        cur = start_d
        while cur <= end_d:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day_str, "$lte": last_day_str},
            }
        },
        {
            "$set": {
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, mo = map(int, month.split("-"))
    last_day_num = calendar.monthrange(year, mo)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{last_day_num:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first_day_str, "$lte": last_day_str}}}
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "log_day_of_week": {
                    "$cond": [
                        {"$ne": ["$logs.date", None]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                }
            }
        },
        {
            "$set": {
                "is_mon_fri": {"$in": ["$log_day_of_week", [2, 3, 4, 5, 6]]},
                "is_present_status": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_mon_fri", True]},
                                    {"$eq": ["$is_present_status", True]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_present_status", True]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$set": {
                "department": "$_id",
                "headcount": {"$size": "$employees_set"},
                "raw_avg_work_hours": {"$avg": "$work_hours_list"},
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$ne": ["$raw_avg_work_hours", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$raw_avg_work_hours", 100]},
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, mo = map(int, month.split("-"))
    last_day_num = calendar.monthrange(year, mo)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{last_day_num:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first_day_str, "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    # Verify department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown department: {department}",
        )

    if to < from_:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must not be earlier than 'from' date",
        )

    d_from = date.fromisoformat(from_)
    d_to = date.fromisoformat(to)
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    from_dt = datetime.fromisoformat(from_).replace(tzinfo=timezone.utc)
    to_dt_bound = datetime.fromisoformat(to).replace(tzinfo=timezone.utc) + timedelta(days=1)

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_, "$lte": to},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bound],
                },
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "raw_moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$raw_moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$raw_moving_avg_7d", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
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
                "moving_avg_7d": 1,
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")
        q: Dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, mo = map(int, month.split("-"))
        last_day_num = calendar.monthrange(year, mo)[1]
        first_day_str = f"{month}-01"
        last_day_str = f"{month}-{last_day_num:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day_str, "$lte": last_day_str},
                }
            },
            {
                "$set": {
                    "day_of_week": {
                        "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, mo = map(int, month.split("-"))
        last_day_num = calendar.monthrange(year, mo)[1]
        first_day_str = f"{month}-01"
        last_day_str = f"{month}-{last_day_num:02d}"

        match_stage: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": first_day_str, "$lte": last_day_str}}}
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$set": {
                    "log_day_of_week": {
                        "$cond": [
                            {"$ne": ["$logs.date", None]},
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                            None,
                        ]
                    }
                }
            },
            {
                "$set": {
                    "is_mon_fri": {"$in": ["$log_day_of_week", [2, 3, 4, 5, 6]]},
                    "is_present_status": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "employees_set": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$eq": ["$is_mon_fri", True]},
                                        {"$eq": ["$is_present_status", True]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "work_hours_list": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$eq": ["$is_present_status", True]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                "$$REMOVE",
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                    },
                    "on_duty_count": {
                        "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                    },
                }
            },
            {
                "$set": {
                    "department": "$_id",
                    "headcount": {"$size": "$employees_set"},
                    "raw_avg_work_hours": {"$avg": "$work_hours_list"},
                }
            },
            {"$match": {"headcount": {"$gt": 0}}},
            {"$sort": {"department": 1}},
            {
                "$project": {
                    "_id": 0,
                    "department": 1,
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$ne": ["$raw_avg_work_hours", None]},
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {"$multiply": ["$raw_avg_work_hours", 100]},
                                                0.5,
                                            ]
                                        }
                                    },
                                    100,
                                ]
                            },
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
        ]
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, mo = map(int, month.split("-"))
        last_day_num = calendar.monthrange(year, mo)[1]
        first_day_str = f"{month}-01"
        last_day_str = f"{month}-{last_day_num:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day_str, "$lte": last_day_str},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])

        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from and to are required for department_trend")
        if to < from_:
            raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date")

        from_dt = datetime.fromisoformat(from_).replace(tzinfo=timezone.utc)
        to_dt_bound = datetime.fromisoformat(to).replace(tzinfo=timezone.utc) + timedelta(days=1)

        dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_, "$lte": to},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date_str": "$_id",
                    "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt_bound],
                    },
                }
            },
            {"$sort": {"date_dt": 1}},
            {
                "$set": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "is_working_day": {
                        "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                    },
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$project": {"_id": 0, "joined_on": 1}},
                    ],
                    "as": "dept_emps",
                }
            },
            {
                "$set": {
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": "$dept_emps",
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]},
                            }
                        }
                    }
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {"$divide": ["$present_count", "$headcount"]},
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date_dt": 1},
                    "output": {
                        "raw_moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]},
                        }
                    },
                }
            },
            {
                "$set": {
                    "moving_avg_7d": {
                        "$cond": [
                            {"$ne": ["$raw_moving_avg_7d", None]},
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {"$multiply": ["$raw_moving_avg_7d", 10000]},
                                                0.5,
                                            ]
                                        }
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
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
                    "moving_avg_7d": 1,
                }
            },
        ]

        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(status_code=422, detail="Invalid endpoint")
