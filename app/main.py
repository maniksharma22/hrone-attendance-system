import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient

from zoneinfo import ZoneInfo

from bson import json_util
import json

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def configure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index("date")

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out, shift_end, date_str):
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    end_time_obj = datetime.strptime(shift_end, "%H:%M").time()
    dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    end = datetime.combine(dt, end_time_obj, tzinfo=ZoneInfo("Asia/Kolkata"))
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class AttendanceUpdateIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    half_day: Optional[bool] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()

    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    
    log = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d, "punch_out": None})
    if not log:
        raise HTTPException(404, "no active punch-in found for today")
        
    emp = db.employees.find_one({"emp_code": body.emp_code})
    
    punch_in_time = log["punch_in"]
    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(ts, emp["shift_end"], d)
    
    db.attendance_logs.update_one(
        {"_id": log["_id"]},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
            }
        }
    )
    
    updated_log = db.attendance_logs.find_one({"_id": log["_id"]})
    updated_log["id"] = str(updated_log.pop("_id"))
    return updated_log


@app.patch("/attendance/{emp_code}/{date}")
def update_attendance(emp_code: str, date: str, body: AttendanceUpdateIn):
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "attendance log not found")
        
    update_data = {}
    if body.status is not None:
        update_data["status"] = body.status
    if body.half_day is not None:
        update_data["half_day"] = body.half_day
        
    ts_in = log["punch_in"]
    if body.punch_in is not None:
        ts_in = datetime.fromtimestamp(body.punch_in / 1000)
        update_data["punch_in"] = ts_in
        emp = db.employees.find_one({"emp_code": emp_code})
        if emp:
            update_data["late_minutes"] = compute_late_minutes(ts_in, emp["shift_start"])
            
    ts_out = log.get("punch_out")
    if body.punch_out is not None:
        ts_out = datetime.fromtimestamp(body.punch_out / 1000)
        update_data["punch_out"] = ts_out
        
    if ts_in and ts_out:
        update_data["work_hours"] = compute_work_hours(ts_in, ts_out)
        emp = db.employees.find_one({"emp_code": emp_code})
        if emp:
            update_data["overtime_minutes"] = compute_overtime(ts_out, emp["shift_end"], date)
            
    if update_data:
        db.attendance_logs.update_one({"_id": log["_id"]}, {"$set": update_data})
        
    updated_log = db.attendance_logs.find_one({"_id": log["_id"]})
    updated_log["id"] = str(updated_log.pop("_id"))
    return updated_log


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
        
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": None,
                "total_days": {"$sum": 1},
                "present_days": {"$sum": {"$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]}},
                "absent_days": {"$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}},
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        }
    ]
    
    result = list(db.attendance_logs.aggregate(pipeline))
    if not result:
        res_data = {
            "emp_code": emp_code,
            "month": month,
            "total_days": 0,
            "present_days": 0,
            "absent_days": 0,
            "total_work_hours": 0.0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }
    else:
        r = result[0]
        res_data = {
            "emp_code": emp_code,
            "month": month,
            "total_days": r["total_days"],
            "present_days": r["present_days"],
            "absent_days": r["absent_days"],
            "total_work_hours": round(r["total_work_hours"], 2),
            "total_late_minutes": r["total_late_minutes"],
            "total_overtime_minutes": r["total_overtime_minutes"],
        }
    return res_data


@app.get("/analytics/departments/summary")
def departments_summary(date: Optional[str] = None):
    target_date = date or datetime.now(IST).date().isoformat()
    
    pipeline = [
        {"$match": {"joined_on": {"$lte": target_date}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$eq": ["$date", target_date]},
                                    {"$eq": ["$status", "PRESENT"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "attendance"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "total_employees": {"$sum": 1},
                "present_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$size": "$attendance"}, 0]}, 1, 0]
                    }
                }
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "total_employees": 1,
                "present_count": 1,
                "date": {"$literal": target_date}
            }
        }
    ]
    
    summary = list(db.employees.aggregate(pipeline))
    return {"summary": summary}

    
@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Optional[str] = None, limit: int = 10):
    match_query = {}
    if month:
        match_query["date"] = {"$regex": f"^{month}"}
        
    pipeline = [
        {"$match": match_query},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$limit": limit},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee_info"
            }
        },
        {"$unwind": {"path": "$employee_info", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": {"$ifNull": ["$employee_info.name", "Unknown"]},
                "total_late_minutes": 1
            }
        }
    ]
    
    leaderboard = list(db.attendance_logs.aggregate(pipeline))
    return {"leaderboard": leaderboard}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str):
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", date_from]},
                                    {"$lte": ["$date", date_to]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "date": "$logs.date",
                "status": "$logs.status",
                "work_hours": {"$ifNull": ["$logs.work_hours", 0.0]}
            }
        },
        {"$match": {"date": {"$ne": None}}},
        {
            "$group": {
                "_id": "$date",
                "present": {
                    "$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}
                },
                "absent": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}
                },
                "total_work_hours": {"$sum": "$work_hours"}
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present": 1,
                "absent": 1,
                "total_work_hours": {"$round": ["$total_work_hours", 2]}
            }
        }
    ]
    
    trend = list(db.employees.aggregate(pipeline))
    return {"department": department, "trend": trend}


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, date: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 10):
    def serialize_explain(res):
        return json.loads(json_util.dumps(res))

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
        cursor = db.attendance_logs.find(q).sort("date", -1)
        return serialize_explain(cursor.explain("executionStats"))
        
    elif endpoint == "employee_monthly":
        target_emp = emp_code or "EMP0001"
        target_month = month or "2026-10"
        pipeline = [
            {"$match": {"emp_code": target_emp, "date": {"$regex": f"^{target_month}"}}},
            {
                "$group": {
                    "_id": None,
                    "total_days": {"$sum": 1},
                    "present_days": {"$sum": {"$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]}},
                    "absent_days": {"$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}},
                    "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            }
        ]
        res = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return serialize_explain(res)
        
    elif endpoint == "department_summary":
        target_date = date or "2026-10-10"
        pipeline = [
            {"$match": {"joined_on": {"$lte": target_date}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$eq": ["$date", target_date]},
                                        {"$eq": ["$status", "PRESENT"]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "attendance"
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "total_employees": {"$sum": 1},
                    "present_count": {
                        "$sum": {
                            "$cond": [{"$gt": [{"$size": "$attendance"}, 0]}, 1, 0]
                        }
                    }
                }
            }
        ]
        res = db.command("aggregate", "employees", pipeline=pipeline, explain=True)
        return serialize_explain(res)
        
    elif endpoint == "late_leaderboard":
        match_query = {}
        if month:
            match_query["date"] = {"$regex": f"^{month}"}
        pipeline = [
            {"$match": match_query},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}
                }
            },
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$limit": limit}
        ]
        res = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return serialize_explain(res)
        
    elif endpoint == "department_trend":
        dept = department or "Engineering"
        df = date_from or "2026-10-01"
        dt = date_to or "2026-10-10"
        pipeline = [
            {"$match": {"department": dept}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", df]},
                                        {"$lte": ["$date", dt]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "date": "$logs.date",
                    "status": "$logs.status",
                    "work_hours": {"$ifNull": ["$logs.work_hours", 0.0]}
                }
            }
        ]
        res = db.command("aggregate", "employees", pipeline=pipeline, explain=True)
        return serialize_explain(res)
        
    else:
        raise HTTPException(404, "Invalid endpoint for explanation")
   