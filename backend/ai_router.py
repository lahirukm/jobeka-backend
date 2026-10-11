from fastapi import APIRouter, HTTPException
from typing import Optional
import pandas as pd
from bson import ObjectId

import ai_match
from database import database, jobs_collection

import ml_loader
from ai_schema import FullTimeMatchRequest, PartTimeMatchRequest, MatchResponse

router = APIRouter(prefix="/api/ai", tags=["AI Models"])

# ── Class label mapping
# ⚠️ EDIT THIS if 0/1/2 mean something different in your model
CLASS_LABELS = {
    0: "Not Suitable",
    1: "Moderate Match",
    2: "Strong Match",
}


def build_response(model, features_df: pd.DataFrame) -> MatchResponse:
    prediction    = int(model.predict(features_df)[0])
    probabilities = model.predict_proba(features_df)[0]

    # model.classes_ tells us which probability column belongs to which class
    prob_dict = {str(cls): round(float(p), 4) for cls, p in zip(model.classes_, probabilities)}

    return MatchResponse(
        prediction=prediction,
        label=CLASS_LABELS.get(prediction, str(prediction)),
        probabilities=prob_dict,
    )


# ════════════════════════════════════════════
# FULL TIME JOB MATCHING
# POST /api/ai/full-time-match
# ════════════════════════════════════════════
@router.post("/full-time-match", response_model=MatchResponse)
async def predict_full_time_match(payload: FullTimeMatchRequest):
    if ml_loader.full_time_model is None:
        raise HTTPException(
            status_code=503,
            detail="full_time_model.pkl not loaded. Add it to ml_models/ and restart the server."
        )
    try:
        # Order MUST match feature_names_in_ exactly:
        # skill_match, experience_match, salary_match, education_match, language_match,
        # trust_score, reliability_score, completed_jobs_count, cancelled_jobs_count, no_show_count
        features = pd.DataFrame([{
            "skill_match":          payload.skill_match,
            "experience_match":     payload.experience_match,
            "salary_match":         payload.salary_match,
            "education_match":      payload.education_match,
            "language_match":       payload.language_match,
            "trust_score":          payload.trust_score,
            "reliability_score":    payload.reliability_score,
            "completed_jobs_count": payload.completed_jobs_count,
            "cancelled_jobs_count": payload.cancelled_jobs_count,
            "no_show_count":        payload.no_show_count,
        }])
        return build_response(ml_loader.full_time_model, features)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {str(e)}")


# ════════════════════════════════════════════
# PART TIME JOB MATCHING
# POST /api/ai/part-time-match
# ════════════════════════════════════════════
@router.post("/part-time-match", response_model=MatchResponse)
async def predict_part_time_match(payload: PartTimeMatchRequest):
    if ml_loader.part_time_model is None:
        raise HTTPException(
            status_code=503,
            detail="part_time_model.pkl not loaded. Add it to ml_models/ and restart the server."
        )
    try:
        # Order MUST match feature_names_in_ exactly:
        # distance_km, availability_match, skill_match_ratio, trust_score, reliability_score
        features = pd.DataFrame([{
            "distance_km":         payload.distance_km,
            "availability_match":  payload.availability_match,
            "skill_match_ratio":   payload.skill_match_ratio,
            "trust_score":         payload.trust_score,
            "reliability_score":   payload.reliability_score,
        }])
        return build_response(ml_loader.part_time_model, features)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {str(e)}")


# ── GET /api/ai/status — check which models are loaded
@router.get("/status")
async def ai_status():
    return {
        "full_time_model_loaded": ml_loader.full_time_model is not None,
        "part_time_model_loaded": ml_loader.part_time_model is not None,
    }


# ════════════════════════════════════════════
# THE MODELS INSIDE THE APP (features built from real data — see ai_match.py)
#   GET /api/ai/applicants/{job_id}?employer_email=     employer: match of every applicant
#   GET /api/ai/job-match/{job_id}?email=&lat=&lng=      seeker: how well do I fit this job?
#   GET /api/ai/part-time-best?email=&lat=&lng=          seeker: nearby part-time jobs ranked for me
# ════════════════════════════════════════════
async def _job(job_id: str):
    if not ObjectId.is_valid(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    job = await jobs_collection.find_one({"_id": ObjectId(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/applicants/{job_id}")
async def applicants_match(job_id: str, employer_email: str):
    job = await _job(job_id)
    if (job.get("employer_email") or "").lower() != employer_email.lower().strip():
        raise HTTPException(status_code=403, detail="Not your job")
    out = {}
    apps = database.get_collection("applications")
    async for a in apps.find({"job_id": job_id, "status": {"$ne": "withdrawn"}}):
        p = await ai_match.person(a["email"])
        r = await ai_match.match(job, p)
        if r:
            out[str(a["_id"])] = r
    return {"available": ml_loader.full_time_model is not None, "matches": out}


@router.get("/job-match/{job_id}")
async def job_match(job_id: str, email: str, lat: Optional[float] = None, lng: Optional[float] = None):
    job = await _job(job_id)
    p = await ai_match.person(email)
    r = await ai_match.match(job, p, lat, lng)
    return r or {"available": False}


@router.get("/part-time-best")
async def part_time_best(email: str, lat: Optional[float] = None, lng: Optional[float] = None, limit: int = 40):
    if ml_loader.part_time_model is None:
        return {"available": False, "jobs": []}
    p = await ai_match.person(email)
    out = []
    q = {"status": "active", "employer_email": {"$ne": p["email"]},
         "$or": [{"type": "Part Time"}, {"type": {"$exists": False}}, {"type": ""}, {"type": None}]}
    async for j in jobs_collection.find(q).sort("createdAt", -1).limit(150):
        r = await ai_match.match(j, p, lat, lng)
        if r:
            out.append({"job_id": str(j["_id"]), "score": r["score"], "label": r["label"],
                        "distance_km": r["features"]["distance_km"], "reasons": r["reasons"]})
    out.sort(key=lambda x: -x["score"])
    return {"available": True, "jobs": out[:max(1, min(limit, 100))]}
