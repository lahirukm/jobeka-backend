from fastapi import APIRouter, HTTPException
import pandas as pd

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
    prediction = int(model.predict(features_df)[0])
    probabilities = model.predict_proba(features_df)[0]

    # model.classes_ tells us which probability column belongs to which class
    prob_dict = {str(cls): round(float(p), 4)
                 for cls, p in zip(model.classes_, probabilities)}

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
        raise HTTPException(
            status_code=500, detail=f"Prediction failed: {str(e)}")


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
        raise HTTPException(
            status_code=500, detail=f"Prediction failed: {str(e)}")


# ── GET /api/ai/status — check which models are loaded
@router.get("/status")
async def ai_status():
    return {
        "full_time_model_loaded": ml_loader.full_time_model is not None,
        "part_time_model_loaded": ml_loader.part_time_model is not None,
    }
