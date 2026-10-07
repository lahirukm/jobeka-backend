"""
JobEka — applications for full-time / contract / internship jobs.

Different from part-time jobs:
  • many people can apply to the same job (the job stays open)
  • no tracking, no arrival code, no payment through the app
  • the employer reviews applicants: Shortlist → Hire, or Reject

Seeker:   POST   /api/jobs/{job_id}/applications        apply
          GET    /api/applications/mine?email=          my applications (+ job info)
          DELETE /api/applications/{aid}?email=         withdraw (while still "applied")
Employer: GET    /api/jobs/{job_id}/applications?employer_email=
          PATCH  /api/applications/{aid}                {employer_email, status}
"""
import re
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException

from database import database, jobs_collection

router = APIRouter(prefix="/api", tags=["Full-time applications"])
applications_collection = database.get_collection("applications")
users_collection        = database.get_collection("users")

STATUSES = ("applied", "shortlisted", "rejected", "hired")


def _oid(v):
    try:
        return ObjectId(v)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid id")

def is_part_time(job) -> bool:
    return (job.get("type") or "Part Time").strip().lower() == "part time"

def _out(a):
    d = dict(a)
    d["_id"] = str(d["_id"])
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ─────────────────────────── seeker applies
@router.post("/jobs/{job_id}/applications")
async def apply(job_id: str, body: dict):
    job = await jobs_collection.find_one({"_id": _oid(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if is_part_time(job):
        raise HTTPException(status_code=400, detail="Part-time jobs use the map apply flow")
    if job.get("status", "active") != "active":
        raise HTTPException(status_code=400, detail="This vacancy is closed")
    email = (body.get("email") or "").lower()
    user = await users_collection.find_one({"email": email}) if email else None
    if not user:
        raise HTTPException(status_code=403, detail="Please log in to apply")
    if email == (job.get("employer_email") or "").lower():
        raise HTTPException(status_code=400, detail="You can't apply to your own job")
    if await applications_collection.find_one({"job_id": job_id, "email": email, "status": {"$ne": "withdrawn"}}):
        raise HTTPException(status_code=400, detail="You have already applied to this job")

    now = datetime.utcnow()
    doc = {
        "job_id": job_id, "job_title": job.get("title", ""), "job_type": job.get("type", ""),
        "company": job.get("employer_name", ""), "employer_email": (job.get("employer_email") or "").lower(),
        "email": email,
        "name":  (body.get("name") or user.get("name", "")).strip()[:80],
        "phone": (body.get("phone") or user.get("phone", "")).strip()[:20],
        "message": (body.get("message") or "").strip()[:1000],
        "status": "applied", "createdAt": now, "updatedAt": now,
    }
    res = await applications_collection.insert_one(doc)
    await jobs_collection.update_one({"_id": job["_id"]}, {"$inc": {"applicants": 1}})
    doc["_id"] = res.inserted_id
    print(f"📨 Application: {email} → {job.get('title')}")
    return _out(doc)


@router.get("/applications/mine")
async def my_applications(email: str):
    out = []
    async for a in applications_collection.find({"email": email.lower(), "status": {"$ne": "withdrawn"}}).sort("createdAt", -1):
        d = _out(a)
        job = await jobs_collection.find_one({"_id": ObjectId(a["job_id"])}) if ObjectId.is_valid(a["job_id"]) else None
        if job:
            d["job"] = {"_id": str(job["_id"]), "title": job.get("title"), "location": job.get("location"),
                        "salary": job.get("salary"), "type": job.get("type"), "category": job.get("category"),
                        "status": job.get("status", "active")}
        out.append(d)
    return out


@router.get("/jobs/{job_id}/my-application")
async def my_application_for_job(job_id: str, email: str):
    a = await applications_collection.find_one({"job_id": job_id, "email": email.lower(), "status": {"$ne": "withdrawn"}})
    return _out(a) if a else {}


@router.delete("/applications/{aid}")
async def withdraw(aid: str, email: str):
    a = await applications_collection.find_one({"_id": _oid(aid)})
    if not a or a["email"] != email.lower():
        raise HTTPException(status_code=404, detail="Application not found")
    if a["status"] != "applied":
        raise HTTPException(status_code=400, detail="The employer has already reviewed this application")
    await applications_collection.update_one({"_id": a["_id"]}, {"$set": {"status": "withdrawn", "updatedAt": datetime.utcnow()}})
    await jobs_collection.update_one({"_id": ObjectId(a["job_id"]), "applicants": {"$gt": 0}}, {"$inc": {"applicants": -1}})
    return {"status": "withdrawn"}


# ─────────────────────────── employer reviews
@router.get("/jobs/{job_id}/applications")
async def job_applications(job_id: str, employer_email: str):
    job = await jobs_collection.find_one({"_id": _oid(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if (job.get("employer_email") or "").lower() != employer_email.lower():
        raise HTTPException(status_code=403, detail="Not your job")
    order = {"hired": 0, "shortlisted": 1, "applied": 2, "rejected": 3}
    items = [_out(a) async for a in applications_collection.find({"job_id": job_id, "status": {"$ne": "withdrawn"}})]
    items.sort(key=lambda a: (order.get(a["status"], 9), a["createdAt"]))
    return items


@router.patch("/applications/{aid}")
async def set_status(aid: str, body: dict):
    a = await applications_collection.find_one({"_id": _oid(aid)})
    if not a:
        raise HTTPException(status_code=404, detail="Application not found")
    if a["employer_email"] != (body.get("employer_email") or "").lower():
        raise HTTPException(status_code=403, detail="Not your job")
    status = body.get("status")
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    await applications_collection.update_one({"_id": a["_id"]}, {"$set": {"status": status, "updatedAt": datetime.utcnow()}})
    return {"status": status}
