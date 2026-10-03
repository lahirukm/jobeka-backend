from fastapi import APIRouter, HTTPException, Header
from database import database
from datetime import datetime

router = APIRouter(prefix="/api/admin", tags=["Admin"])

users_collection = database.get_collection("users")
jobs_collection  = database.get_collection("jobs")

# Simple admin key check (change this to a strong secret!)
import os
ADMIN_KEY = os.getenv("ADMIN_KEY", "jobeka_admin_2025")  # set ADMIN_KEY in .env

def check_admin(x_admin_key: str = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")


# ── GET /api/admin/stats
@router.get("/stats")
async def get_stats(x_admin_key: str = Header(None)):
    check_admin(x_admin_key)
    total_users       = await users_collection.count_documents({})
    job_seekers       = await users_collection.count_documents({"role": "job_seeker"})
    employers         = await users_collection.count_documents({"role": "employer"})
    service_providers = await users_collection.count_documents({"role": "service_provider"})
    total_jobs        = await jobs_collection.count_documents({})
    active_jobs       = await jobs_collection.count_documents({"status": "active"})
    applied_jobs      = await jobs_collection.count_documents({"status": "applied"})
    closed_jobs       = await jobs_collection.count_documents({"status": "closed"})
    return {
        "total_users":       total_users,
        "job_seekers":       job_seekers,
        "employers":         employers,
        "service_providers": service_providers,
        "total_jobs":        total_jobs,
        "active_jobs":       active_jobs,
        "applied_jobs":      applied_jobs,
        "closed_jobs":       closed_jobs,
    }


# ── GET /api/admin/users
@router.get("/users")
async def get_all_users(x_admin_key: str = Header(None)):
    check_admin(x_admin_key)
    users = []
    async for user in users_collection.find().sort("createdAt", -1):
        user["_id"] = str(user["_id"])
        user.pop("password", None)
        if "createdAt" in user:
            user["createdAt"] = user["createdAt"].isoformat()
        users.append(user)
    return users


# ── GET /api/admin/jobs
@router.get("/jobs")
async def get_all_jobs(x_admin_key: str = Header(None)):
    check_admin(x_admin_key)
    jobs = []
    async for job in jobs_collection.find().sort("createdAt", -1):
        job["_id"] = str(job["_id"])
        job.pop("arrival", None)  # hide OTP data
        if "createdAt" in job:
            job["createdAt"] = job["createdAt"].isoformat()
        jobs.append(job)
    return jobs


# ── DELETE /api/admin/users/{id}
@router.delete("/users/{user_id}")
async def delete_user(user_id: str, x_admin_key: str = Header(None)):
    check_admin(x_admin_key)
    from bson import ObjectId
    await users_collection.delete_one({"_id": ObjectId(user_id)})
    return {"message": "User deleted"}


# ── DELETE /api/admin/jobs/{id}
@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, x_admin_key: str = Header(None)):
    check_admin(x_admin_key)
    from bson import ObjectId
    await jobs_collection.delete_one({"_id": ObjectId(job_id)})
    return {"message": "Job deleted"}
