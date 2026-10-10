"""
JobEka — applications for full-time / contract / internship jobs.

Different from part-time jobs:
  • many people can apply to the same job (the job stays open)
  • no tracking, no arrival code, no payment through the app
  • the employer reviews applicants: Shortlist → Hire, or Reject

Seeker:   POST   /api/jobs/{job_id}/applications        apply with the JobEka CV (built in the app)
          POST   /api/jobs/{job_id}/applications/file   apply with own CV file (multipart: file, email, name, phone, message)
          GET    /api/applications/{aid}/cv-file?email= | ?employer_email=   open the uploaded CV
          GET    /api/applications/mine?email=          my applications (+ job info)
          DELETE /api/applications/{aid}?email=         withdraw (while still "applied")
Employer: GET    /api/jobs/{job_id}/applications?employer_email=
          PATCH  /api/applications/{aid}                {employer_email, status}
"""
import re
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId
from bson import Binary
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from database import database, jobs_collection

router = APIRouter(prefix="/api", tags=["Full-time applications"])
applications_collection = database.get_collection("applications")
users_collection        = database.get_collection("users")
cv_collection           = database.get_collection("cv_profiles")
cv_files_collection     = database.get_collection("cv_files")       # CVs uploaded from the phone (PDF / Word / photo)

CV_MAX   = 5 * 1024 * 1024
CV_TYPES = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "image/jpeg": "jpg", "image/png": "png",
}

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
async def _check_and_build(job_id: str, body: dict, attach_saved_cv: bool):
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
    saved_cv = await cv_collection.find_one({"email": email}) if attach_saved_cv else None   # the CV made in the CV builder
    doc = {
        "cv": (saved_cv or {}).get("cv"), "cv_template": (saved_cv or {}).get("template", "modern"),
        "cv_source": "jobeka" if attach_saved_cv else "file",
        "job_id": job_id, "job_title": job.get("title", ""), "job_type": job.get("type", ""),
        "company": job.get("employer_name", ""), "employer_email": (job.get("employer_email") or "").lower(),
        "email": email,
        "name":  (body.get("name") or user.get("name", "")).strip()[:80],
        "phone": (user.get("phone") or body.get("phone") or "").strip()[:20],     # registered (verified) number
        "message": (body.get("message") or "").strip()[:1000],
        "status": "applied", "createdAt": now, "updatedAt": now,
    }
    return job, doc

async def _save(job, doc):
    res = await applications_collection.insert_one(doc)
    await jobs_collection.update_one({"_id": job["_id"]}, {"$inc": {"applicants": 1}})
    doc["_id"] = res.inserted_id
    print(f"📨 Application ({doc['cv_source']}): {doc['email']} → {job.get('title')}")
    return doc

@router.post("/jobs/{job_id}/applications")
async def apply(job_id: str, body: dict):
    job, doc = await _check_and_build(job_id, body, attach_saved_cv=True)
    return _out(await _save(job, doc))


@router.post("/jobs/{job_id}/applications/file")
async def apply_with_file(job_id: str, file: UploadFile = File(...), email: str = Form(...), name: str = Form(""),
                          phone: str = Form(""), message: str = Form("")):
    ctype = (file.content_type or "").lower()
    if ctype not in CV_TYPES:
        raise HTTPException(status_code=400, detail="Upload your CV as PDF, Word (.doc/.docx) or a photo (JPG/PNG)")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty")
    if len(data) > CV_MAX:
        raise HTTPException(status_code=400, detail="CV file is larger than 5 MB")
    job, doc = await _check_and_build(job_id, {"email": email, "name": name, "phone": phone, "message": message}, attach_saved_cv=False)
    fname = (file.filename or f"cv.{CV_TYPES[ctype]}").replace("/", "_")[-80:]
    doc["cv_file"] = {"name": fname, "type": ctype, "size": len(data)}
    doc = await _save(job, doc)
    await cv_files_collection.insert_one({"application_id": str(doc["_id"]), "email": doc["email"], "name": fname,
                                         "content_type": ctype, "data": Binary(data), "createdAt": doc["createdAt"]})
    return _out(doc)


@router.get("/applications/{aid}/cv-file")
async def open_cv_file(aid: str, email: str = "", employer_email: str = ""):
    a = await applications_collection.find_one({"_id": _oid(aid)})
    if not a:
        raise HTTPException(status_code=404, detail="Application not found")
    allowed = (email and email.lower() == a["email"]) or (employer_email and employer_email.lower() == a["employer_email"])
    if not allowed:
        raise HTTPException(status_code=403, detail="Not allowed")
    f = await cv_files_collection.find_one({"application_id": aid})
    if not f:
        raise HTTPException(status_code=404, detail="No CV file for this application")
    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", f["name"])
    return Response(content=bytes(f["data"]), media_type=f["content_type"],
                    headers={"Content-Disposition": f'inline; filename="{safe}"', "Cache-Control": "private, max-age=300"})


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
    items = []
    async for a in applications_collection.find({"job_id": job_id, "status": {"$ne": "withdrawn"}}):
        d = _out(a)
        d["has_cv"] = bool(a.get("cv"))
        d["has_cv_file"] = bool(a.get("cv_file"))
        d["headline"] = (a.get("cv") or {}).get("headline", "")
        d["skills"] = (a.get("cv") or {}).get("skills", [])[:6]
        d.pop("cv", None)                                   # full CV is opened separately
        items.append(d)
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
