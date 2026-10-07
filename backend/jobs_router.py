import re
from fastapi import APIRouter, HTTPException
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime

from database import jobs_collection
from job_schema import JobCreate, JobUpdate

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


def job_to_dict(job) -> dict:
    job["_id"] = str(job["_id"])
    # never send the OTP hash to any client (a 4-digit OTP hash could be brute-forced)
    if isinstance(job.get("arrival"), dict):
        job["arrival"] = {k: v for k, v in job["arrival"].items() if k != "otp_hash"}
    return job


# ── POST /api/jobs/ — create job (save employer_email)
@router.post("/")
async def create_job(job: JobCreate):
    job_dict              = job.model_dump()
    job_dict["status"]    = "active"
    job_dict["applicants"]= 0
    job_dict["createdAt"] = datetime.utcnow()

    result  = await jobs_collection.insert_one(job_dict)
    new_job = await jobs_collection.find_one({"_id": result.inserted_id})
    print(f"✅ Job saved: {new_job['title']} by {new_job.get('employer_email','unknown')}")
    return job_to_dict(new_job)


# ── GET /api/jobs/ — get ALL jobs for a specific employer (filtered by email)
@router.get("/")
async def get_jobs(employer_email: str = None):
    jobs = []
    query = {}
    if employer_email:
        query["employer_email"] = employer_email
    async for job in jobs_collection.find(query).sort("createdAt", -1):
        jobs.append(job_to_dict(job))
    return jobs


# ── GET /api/jobs/stats — real numbers for the home screen
@router.get("/stats")
async def job_stats():
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    active_full_time = await jobs_collection.count_documents({"status": "active", "type": "Full Time"})
    active_part_time = await jobs_collection.count_documents({"status": "active", "type": "Part Time"})
    active_intern    = await jobs_collection.count_documents({"status": "active", "type": "Internship"})
    active_total     = await jobs_collection.count_documents({"status": "active"})
    posted_today     = await jobs_collection.count_documents({"createdAt": {"$gte": today}})
    completed_today  = await jobs_collection.count_documents({"status": "closed", "completedAt": {"$gte": today}})
    employers        = await jobs_collection.distinct("employer_email", {"status": "active", "employer_email": {"$nin": [None, ""]}})
    return {
        "active_total":     active_total,
        "active_part_time": active_part_time,
        "active_full_time": active_full_time,
        "active_internship": active_intern,
        "posted_today":     posted_today,
        "completed_today":  completed_today,
        "active_employers": len(employers),
    }


# ── GET /api/jobs/my-applications?email= — jobs this job seeker applied for
@router.get("/my-applications")
async def my_applications(email: str):
    jobs = []
    async for job in jobs_collection.find({"appliedBy.email": email.lower()}).sort("createdAt", -1):
        jobs.append(job_to_dict(job))
    return jobs


# ── GET /api/jobs/part-time — get only Part Time active jobs with coordinates (for map)
@router.get("/part-time")
async def get_part_time_jobs():
    """
    Returns all active Part Time jobs that have lat/lng set.
    Used by the customer-side map screen.
    """
    jobs = []
    query = {
        "type":      "Part Time",
        "status":    "active",
        "latitude":  {"$ne": None},
        "longitude": {"$ne": None},
    }
    async for job in jobs_collection.find(query).sort("createdAt", -1):
        jobs.append(job_to_dict(job))
    return jobs


# ── GET /api/jobs/all-active — all active jobs (for full-time list etc.)
@router.get("/all-active")
async def get_all_active_jobs():
    jobs = []
    async for job in jobs_collection.find({"status": "active"}).sort("createdAt", -1):
        jobs.append(job_to_dict(job))
    return jobs


# ── GET /api/jobs/{id}
# ── GET /api/jobs/options?field=type|category&q= — values employers typed before (grows the dropdowns)
@router.get("/options")
async def job_options(field: str = "category", q: str = ""):
    if field not in ("type", "category", "education_level", "required_skills", "languages"):
        raise HTTPException(status_code=400, detail="Unknown field")
    values = await jobs_collection.distinct(field)
    seen, out = set(), []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        k = v.strip().lower()
        if k in seen or (q and q.lower() not in k):
            continue
        seen.add(k); out.append(v.strip())
    return sorted(out)[:30]


# ── GET /api/jobs/search?q=&type=&category= — job seekers search vacancies ("IT", "accountant", "react")
@router.get("/search")
async def search_jobs(q: str = "", type: str = "", category: str = "", exclude_part_time: bool = True):
    query = {"status": "active"}
    if type:
        query["type"] = re.compile(f"^{re.escape(type)}$", re.I)
    elif exclude_part_time:
        query["type"] = {"$not": re.compile("^part time$", re.I)}
    if category:
        query["category"] = re.compile(f"^{re.escape(category)}$", re.I)
    words = [w for w in re.split(r"\s+", q.strip().lower()) if w]
    out = []
    async for job in jobs_collection.find(query).sort("createdAt", -1):
        text = " ".join([job.get("title", ""), job.get("category", ""), job.get("type", ""), job.get("location", ""),
                         job.get("description", ""), job.get("requirements", ""), job.get("employer_name", ""),
                         " ".join(job.get("required_skills") or [])]).lower()
        # short words like "IT", "HR", "UI" must match a whole word (so "it" doesn't hit "city")
        if all((re.search(rf"\b{re.escape(w)}\b", text) if len(w) <= 3 else w in text) for w in words):
            out.append(job_to_dict(job))
    return out


@router.get("/{job_id}")
async def get_job(job_id: str):
    try:
        oid = ObjectId(job_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid job id")
    job = await jobs_collection.find_one({"_id": oid})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_dict(job)


# ── PUT /api/jobs/{id}
@router.put("/{job_id}")
async def update_job(job_id: str, job: JobUpdate):
    try:
        oid = ObjectId(job_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid job id")

    update_data = {k: v for k, v in job.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = await jobs_collection.update_one({"_id": oid}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Job not found")

    updated = await jobs_collection.find_one({"_id": oid})
    print(f"✅ Job updated: {updated['title']}")
    return job_to_dict(updated)


# ── POST /api/jobs/{id}/apply — apply → "applied" (hidden from map, job in progress)
@router.post("/{job_id}/apply")
async def apply_job(job_id: str, body: dict):
    try:
        oid = ObjectId(job_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid job id")

    job = await jobs_collection.find_one({"_id": oid})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if (job.get("type") or "Part Time").strip().lower() != "part time":
        raise HTTPException(status_code=400, detail="This is not a part-time job. Please update the app to apply.")

    if job.get("status") in ("applied", "closed"):
        raise HTTPException(status_code=400, detail="This job is already taken")

    await jobs_collection.update_one(
        {"_id": oid},
        {"$set": {
            "status":     "applied",   # ← hidden from map, job in progress
            "applicants": (job.get("applicants") or 0) + 1,
            "appliedBy":  {
                "name":    body.get("name",    ""),
                "phone":   body.get("phone",   ""),
                "email":   (body.get("email") or "").lower(),
                "message": body.get("message", ""),
            },
        }}
    )
    updated = await jobs_collection.find_one({"_id": oid})
    print(f"✅ Applied: {updated['title']} by {body.get('name')}")
    return job_to_dict(updated)


# ── POST /api/jobs/{id}/cancel — customer cancels → restore to "active"
@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    try:
        oid = ObjectId(job_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid job id")

    job = await jobs_collection.find_one({"_id": oid})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Only restore if currently "applied" (in progress but not completed)
    if job.get("status") == "applied":
        await jobs_collection.update_one(
            {"_id": oid},
            {"$set": {
                "status":    "active",
                "appliedBy": None,
            }}
        )
        print(f"✅ Job restored to active: {job['title']}")
    return {"message": "Job restored to active"}


# ── POST /api/jobs/{id}/complete — job done → "closed" permanently
@router.post("/{job_id}/complete")
async def complete_job(job_id: str):
    try:
        oid = ObjectId(job_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid job id")

    job = await jobs_collection.find_one({"_id": oid})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    await jobs_collection.update_one(
        {"_id": oid},
        {"$set": {"status": "closed", "completedAt": datetime.utcnow()}}
    )
    print(f"✅ Job completed & closed: {job['title']}")
    return {"message": "Job completed successfully"}


# ── DELETE /api/jobs/{id}
@router.delete("/{job_id}")
async def delete_job(job_id: str):
    try:
        oid = ObjectId(job_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid job id")

    result = await jobs_collection.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Job not found")

    print(f"🗑️ Job deleted: {job_id}")
    return {"message": "Job deleted"}
