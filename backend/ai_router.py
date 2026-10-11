import csv
import io
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response
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
            await ai_match.log(job, a["email"], r, final=ai_match.is_final_app(a))
    return {"available": ml_loader.full_time_model is not None, "matches": out}


@router.get("/job-match/{job_id}")
async def job_match(job_id: str, email: str, lat: Optional[float] = None, lng: Optional[float] = None):
    job = await _job(job_id)
    p = await ai_match.person(email)
    r = await ai_match.match(job, p, lat, lng)
    if r:
        if ai_match.is_part_time(job):
            final = job.get("status") in ("applied", "closed")
        else:
            a = await database.get_collection("applications").find_one({"job_id": job_id, "email": p["email"]})
            final = bool(a and ai_match.is_final_app(a))
        await ai_match.log(job, p["email"], r, final=final)
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


# ════════════════════════════════════════════
# RETRAINING DATA (admin dashboard → "AI models")
#   GET /api/ai/training/stats                    how many predictions have a real outcome, live accuracy
#   GET /api/ai/training.csv?model=full_time       CSV for Colab (features + label) → retrain → new .pkl
# ════════════════════════════════════════════
from admin_router import check_admin

async def _rows(model: str):
    rows = []
    async for r in ai_match.training_collection.find({"model": model}).sort("createdAt", 1):
        label, why = await ai_match.outcome(r)
        rows.append((r, label, why))
    return rows

@router.get("/training/stats", dependencies=[Depends(check_admin)])
async def training_stats():
    out = {"models": {"full_time": ml_loader.full_time_model is not None, "part_time": ml_loader.part_time_model is not None}}
    for m in ("full_time", "part_time"):
        rows = await _rows(m)
        done = [(r, l) for r, l, _ in rows if l is not None]
        right = sum(1 for r, l in done if r.get("predicted") == l)
        out[m] = {
            "predictions": len(rows), "with_outcome": len(done), "waiting": len(rows) - len(done),
            "labels": {str(k): sum(1 for _, l in done if l == k) for k in (0, 1, 2)},
            "accuracy": round(right / len(done), 3) if done else None,
            "recent": [{"job": r.get("job_title", ""), "email": r["email"], "score": r.get("score"),
                        "predicted": CLASS_LABELS.get(r.get("predicted"), "?"), "outcome": why,
                        "label": None if l is None else CLASS_LABELS.get(l), "correct": None if l is None else r.get("predicted") == l}
                       for r, l, why in rows[-12:][::-1]],
        }
    return out


@router.get("/training.csv", dependencies=[Depends(check_admin)])
async def training_csv(model: str = "full_time", include_pending: bool = False):
    if model not in ("full_time", "part_time"):
        raise HTTPException(status_code=400, detail="model must be full_time or part_time")
    cols = ai_match.FT_COLS if model == "full_time" else ai_match.PT_COLS
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols + ["label", "outcome", "ai_predicted", "ai_score", "gated", "job_title", "created"])
    for r, label, why in await _rows(model):
        if label is None and not include_pending:
            continue
        f = r.get("features") or {}
        w.writerow([f.get(c, "") for c in cols] + ["" if label is None else label, why, r.get("predicted"), r.get("score"),
                                                     int(bool(r.get("gated"))), r.get("job_title", ""), r.get("createdAt", "")])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="jobeka_{model}_training.csv"'})
