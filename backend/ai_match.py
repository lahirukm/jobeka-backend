"""
JobEka — turns real app data into the inputs of the two trained RandomForest models.

Full-time model (10 features)            where the value comes from
  skill_match          0-1   job.required_skills  vs  CV skills (+ profile skills, headline, experience text)
  experience_match     0-1   years in the CV experience list  vs  job.experience_years
  salary_match         0-1   CV expected salary vs job salary (neutral 0.8 when the CV has none)
  education_match      0-1   highest CV qualification  vs  job.education_level
  language_match       0-1   CV languages  vs  job.languages
  trust_score          0-1   profile completeness, account age, finished jobs
  reliability_score    0-1   finished / (finished + cancelled + no-shows), smoothed for new users
  completed_jobs_count       part-time jobs finished + full-time jobs hired
  cancelled_jobs_count       applications withdrawn + part-time jobs given up
  no_show_count              interviews marked "didn't come" by employers

Part-time model (5 features)
  distance_km                GPS distance worker → job
  availability_match   0-1   1.0 when the worker has no other part-time job in progress
  skill_match_ratio    0-1   same skill matching as above (job skills or job title/category)
  trust_score, reliability_score   as above
"""
import math
import re
from datetime import datetime

import pandas as pd

import ml_loader
from database import database, jobs_collection

users_collection        = database.get_collection("users")
cv_collection           = database.get_collection("cv_profiles")
applications_collection = database.get_collection("applications")
events_collection       = database.get_collection("ai_events")     # optional: part-time cancellations

LABELS = {0: "Not Suitable", 1: "Moderate Match", 2: "Strong Match"}
EDU = ["no formal", "o/l", "a/l", "certificate", "diploma", "bachelor", "master", "phd"]
EDU_WORDS = [
    (7, ("phd", "doctorate", "d.phil")),
    (6, ("master", "msc", "m.sc", "mba", "ma ", "mphil")),
    (5, ("bachelor", "bsc", "b.sc", "ba ", "b.a", "beng", "b.eng", "degree", "bit", "bcom", "llb")),
    (4, ("diploma", "hnd", "hndit", "nvq 5", "nvq 6")),
    (3, ("certificate", "cert", "nvq")),
    (2, ("a/l", "a.l", "advanced level", "gce a")),
    (1, ("o/l", "o.l", "ordinary level", "gce o")),
]
STOP = {"and", "the", "for", "with", "job", "work", "staff", "assistant", "part", "time", "full", "a", "of", "in", "to"}


# ─────────────────────────── small helpers
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9+#./ ]", " ", str(s or "").lower()).strip()

def _words(s) -> set:
    return {w for w in _norm(s).split() if len(w) > 2 and w not in STOP}

def _has(text: str, skill: str) -> bool:
    k = _norm(skill)
    if not k:
        return False
    if k in text:
        return True
    parts = [w for w in k.split() if len(w) > 2]
    return bool(parts) and all(p in text for p in parts)

def _edu_level(text: str) -> int:
    t = f" {_norm(text)} "
    for lvl, keys in EDU_WORDS:
        if any(k in t for k in keys):
            return lvl
    return 0

def _job_edu(level: str) -> int:
    t = _norm(level)
    if not t or "no formal" in t:
        return 0
    for i, k in enumerate(EDU):
        if k in t:
            return i
    return _edu_level(t)

def _years_in(text: str):
    m = re.search(r"(19|20)\d{2}", str(text or ""))
    return int(m.group(0)) if m else None

def _cv_years(experience: list) -> float:
    total, now = 0.0, datetime.utcnow().year
    for e in experience or []:
        s = _years_in(e.get("start"))
        end_txt = str(e.get("end") or "")
        f = now if (not end_txt or re.search(r"present|now|current|today", end_txt, re.I)) else _years_in(end_txt)
        if s and f and f >= s:
            total += max(0.5, f - s)
        elif s or f:
            total += 1
    return min(total, 40)

def _money(s):
    m = re.search(r"\d[\d,]*(\.\d+)?", str(s or ""))
    try:
        v = float(m.group(0).replace(",", "")) if m else None
    except ValueError:
        return None
    if v and re.search(r"\d\s*k\b", str(s), re.I):
        v *= 1000
    return v

def km(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371
    d1, d2 = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(d1 / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d2 / 2) ** 2
    return round(r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


# ─────────────────────────── who is this person (CV + history)
async def person(email: str) -> dict:
    email = (email or "").lower().strip()
    user = await users_collection.find_one({"email": email}) or {}
    cv = ((await cv_collection.find_one({"email": email})) or {}).get("cv") or {}

    skills = list(cv.get("skills") or []) + [s.strip() for s in str(user.get("skills") or "").split(",") if s.strip()]
    text = " ".join([" ".join(skills), cv.get("headline", ""), cv.get("summary", ""),
                     " ".join(f"{e.get('title','')} {e.get('description','')} {' '.join(e.get('bullets') or [])}" for e in cv.get("experience") or []),
                     " ".join(cv.get("certifications") or [])])
    edu_text = " ".join(f"{e.get('qualification','')}" for e in cv.get("education") or [])

    # history
    pt_done = await jobs_collection.count_documents({"appliedBy.email": email, "status": "closed"})
    pt_busy = await jobs_collection.count_documents({"appliedBy.email": email, "status": "applied"})
    ft_hired = await applications_collection.count_documents({"email": email, "status": "hired"})
    withdrawn = await applications_collection.count_documents({"email": email, "status": "withdrawn"})
    pt_cancel = await events_collection.count_documents({"email": email, "type": "cancel"})
    no_show = await applications_collection.count_documents({"email": email, "interview.outcome": "no_show"})
    completed, cancelled = pt_done + ft_hired, withdrawn + pt_cancel

    # reliability: smoothed so a brand-new user starts around 0.8, not 0 or 1
    reliability = (completed + 4) / (completed + cancelled + 2 * no_show + 5)

    # trust: verified phone, profile completeness, account age, track record
    filled = sum(bool(x) for x in (cv.get("full_name"), cv.get("summary"), cv.get("skills"),
                                    cv.get("experience"), cv.get("education"), user.get("avatar_v")))
    created = user.get("createdAt") or datetime.utcnow()
    age_days = max(0, (datetime.utcnow() - created).days) if isinstance(created, datetime) else 0
    trust = (0.25 + 0.15 * bool(user.get("phone")) + 0.25 * filled / 6
             + 0.15 * min(1, age_days / 90) + 0.20 * min(1, completed / 10) - 0.08 * no_show)

    return {
        "email": email, "name": user.get("name") or cv.get("full_name") or "",
        "skills": skills, "text": _norm(text), "languages": [_norm(l) for l in cv.get("languages") or []],
        "edu": _edu_level(edu_text), "years": _cv_years(cv.get("experience")),
        "expected_salary": _money(cv.get("expected_salary")),
        "has_cv": bool(cv), "busy": pt_busy > 0,
        "completed": completed, "cancelled": cancelled, "no_show": no_show,
        "trust": round(max(0.0, min(1.0, trust)), 3), "reliability": round(max(0.0, min(1.0, reliability)), 3),
    }


def _skills(job: dict, p: dict):
    req = [s for s in job.get("required_skills") or [] if str(s).strip()]
    if req:
        have = [s for s in req if _has(p["text"], s)]
        return len(have) / len(req), have, [s for s in req if s not in have]
    # no skill list → compare the job's title / category words with the person's CV
    words = _words(f"{job.get('title','')} {job.get('category','')}")
    if not words or not p["text"]:
        return 0.5, [], []
    hit = [w for w in words if w in p["text"]]
    return min(1.0, 0.3 + 0.7 * len(hit) / len(words)), hit, []


# ─────────────────────────── features + prediction
def full_time_features(job: dict, p: dict):
    skill, have, missing = _skills(job, p)
    need_years = float(job.get("experience_years") or 0)
    exp = 1.0 if need_years <= 0 else min(1.0, p["years"] / need_years)
    pay = _money(job.get("salary"))
    sal = 0.8 if not (pay and p["expected_salary"]) else (1.0 if pay >= p["expected_salary"] else max(0.0, pay / p["expected_salary"]))
    need_edu = _job_edu(job.get("education_level"))
    edu = 1.0 if need_edu == 0 else (1.0 if p["edu"] >= need_edu else max(0.0, 1 - 0.3 * (need_edu - p["edu"])))
    langs = [_norm(l) for l in job.get("languages") or [] if str(l).strip()]
    lang = 1.0 if not langs else (0.6 if not p["languages"] else sum(1 for l in langs if l in p["languages"]) / len(langs))
    f = {
        "skill_match": round(skill, 3), "experience_match": round(exp, 3), "salary_match": round(sal, 3),
        "education_match": round(edu, 3), "language_match": round(lang, 3),
        "trust_score": p["trust"], "reliability_score": p["reliability"],
        "completed_jobs_count": p["completed"], "cancelled_jobs_count": p["cancelled"], "no_show_count": p["no_show"],
    }
    reasons = []
    req = job.get("required_skills") or []
    if req:
        reasons.append(("good" if skill >= 0.67 else "warn" if skill >= 0.34 else "bad",
                        f"Has {len(have)} of {len(req)} skills" + (f" · missing {', '.join(missing[:3])}" if missing else "")))
    if need_years > 0:
        reasons.append(("good" if exp >= 1 else "warn", f"{p['years']:g} yrs experience (needs {need_years:g})"))
    if need_edu:
        reasons.append(("good" if edu >= 1 else "warn", "Education matches" if edu >= 1 else f"Needs {job.get('education_level')}"))
    if langs:
        reasons.append(("good" if lang >= 1 else "warn", "Speaks the required languages" if lang >= 1 else f"Languages: needs {', '.join(job.get('languages') or [])}"))
    if p["no_show"]:
        reasons.append(("bad", f"Missed {p['no_show']} interview{'s' if p['no_show'] > 1 else ''} before"))
    elif p["completed"]:
        reasons.append(("good", f"{p['completed']} job{'s' if p['completed'] > 1 else ''} done on JobEka"))
    if not p["has_cv"]:
        reasons.append(("warn", "No JobEka CV yet — fewer details to match"))
    return f, reasons


def part_time_features(job: dict, p: dict, lat=None, lng=None):
    skill, have, _ = _skills(job, p)
    d = km(lat, lng, job.get("latitude"), job.get("longitude"))
    f = {
        "distance_km": d if d is not None else 10.0,
        "availability_match": 0.3 if p["busy"] else 1.0,
        "skill_match_ratio": round(skill, 3),
        "trust_score": p["trust"], "reliability_score": p["reliability"],
    }
    reasons = []
    if d is not None:
        reasons.append(("good" if d <= 3 else "warn" if d <= 10 else "bad", f"{d:g} km away"))
    reasons.append(("good" if skill >= 0.6 else "warn", "Your skills fit this job" if skill >= 0.6 else "Few matching skills in your CV"))
    if p["busy"]:
        reasons.append(("warn", "You already have a job in progress"))
    if p["completed"]:
        reasons.append(("good", f"{p['completed']} job{'s' if p['completed'] > 1 else ''} done on JobEka"))
    return f, reasons


def predict(model, features: dict) -> dict:
    """→ {score 0-100, label, prediction, probabilities}. score = 100·P(strong) + 50·P(moderate)."""
    df = pd.DataFrame([features])
    cols = getattr(model, "feature_names_in_", None)
    if cols is not None:
        df = df[list(cols)]
    probs = {int(c): float(p) for c, p in zip(model.classes_, model.predict_proba(df)[0])}
    pred = max(probs, key=probs.get)
    score = round(100 * probs.get(2, 0) + 50 * probs.get(1, 0))
    return {"score": score, "prediction": pred, "label": LABELS.get(pred, str(pred)),
            "probabilities": {str(k): round(v, 4) for k, v in probs.items()}}


def is_part_time(job) -> bool:
    return (job.get("type") or "Part Time").strip().lower() == "part time"


async def match(job: dict, p: dict, lat=None, lng=None):
    pt = is_part_time(job)
    model = ml_loader.part_time_model if pt else ml_loader.full_time_model
    if model is None:
        return None
    f, reasons = part_time_features(job, p, lat, lng) if pt else full_time_features(job, p)
    out = predict(model, f)
    out["model"] = "part_time" if pt else "full_time"
    out["features"] = f
    out["reasons"] = [{"tone": t, "text": x} for t, x in reasons]
    return out
