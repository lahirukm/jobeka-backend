from pydantic import BaseModel, Field


# ════════════════════════════════════════════
# FULL TIME MODEL — 10 features
# Trained feature order:
# skill_match, experience_match, salary_match, education_match, language_match,
# trust_score, reliability_score, completed_jobs_count, cancelled_jobs_count, no_show_count
# ════════════════════════════════════════════
class FullTimeMatchRequest(BaseModel):
    skill_match:          float = Field(..., ge=0, le=1,
                                        description="0-1 score, how well skills match")
    experience_match:     float = Field(..., ge=0,
                                        le=1, description="0-1 score, experience fit")
    salary_match:         float = Field(..., ge=0, le=1,
                                        description="0-1 score, salary expectation fit")
    education_match:      float = Field(..., ge=0, le=1,
                                        description="0-1 score, education requirement fit")
    language_match:       float = Field(..., ge=0, le=1,
                                        description="0-1 score, language requirement fit")
    trust_score:          float = Field(..., ge=0, le=1,
                                        description="0-1 score, applicant trust score")
    reliability_score:    float = Field(..., ge=0, le=1,
                                        description="0-1 score, applicant reliability")
    completed_jobs_count: int = Field(..., ge=0,
                                      description="number of jobs completed before")
    cancelled_jobs_count: int = Field(..., ge=0,
                                      description="number of jobs cancelled before")
    no_show_count:        int = Field(..., ge=0,
                                      description="number of no-shows before")


class MatchResponse(BaseModel):
    prediction: int                 # 0, 1, or 2 — raw class predicted by the model
    label: str                      # human-readable label
    # confidence per class, e.g. {"0": 0.1, "1": 0.3, "2": 0.6}
    probabilities: dict


# ════════════════════════════════════════════
# PART TIME MODEL — 5 features
# Trained feature order:
# distance_km, availability_match, skill_match_ratio, trust_score, reliability_score
# ════════════════════════════════════════════
class PartTimeMatchRequest(BaseModel):
    distance_km:         float = Field(
        ..., ge=0, description="distance between applicant and job location, in km")
    availability_match:  float = Field(..., ge=0, le=1,
                                       description="0-1 score, schedule/availability fit")
    skill_match_ratio:   float = Field(..., ge=0, le=1,
                                       description="0-1 ratio, how many required skills match")
    trust_score:         float = Field(..., ge=0, le=1,
                                       description="0-1 score, applicant trust score")
    reliability_score:   float = Field(..., ge=0, le=1,
                                       description="0-1 score, applicant reliability")
