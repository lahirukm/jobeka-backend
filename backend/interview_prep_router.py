"""
JobEka — Interview Prep: the 5 "General" questions answered from the user's OWN profile + CV.

  POST /api/interview-prep/my-answers   (login token)
       {role?, company?, refresh?}
    → {answers: [5 strings], source: "gemini" | "claude" | "builtin", has_cv, name}

Questions (same order as the app):
  0 Tell me about yourself · 1 Strengths · 2 Weakness · 3 Why work here · 4 Five years from now

Answers are saved per user + role, and made again only when the CV changes or the user taps "Make again".
"""
import json
import re
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import database
from cv_router import _ask_ai, _clean_cv, GEMINI_API_KEY, ANTHROPIC_API_KEY, cv_collection, users_collection

router = APIRouter(prefix="/api/interview-prep", tags=["Interview prep"])
cache = database.get_collection("interview_answers")

QUESTIONS = [
    "Tell me about yourself.",
    "What are your greatest strengths?",
    "What is your biggest weakness?",
    "Why do you want to work here?",
    "Where do you see yourself in 5 years?",
]


def _s(v, n=80) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _profile(user: dict, cv: dict) -> dict:
    """Only what the answers need — nothing private like phone, address or birthday."""
    return {
        "name": cv.get("full_name") or user.get("name", ""),
        "headline": cv.get("headline", ""),
        "summary": cv.get("summary") or cv.get("about_me", ""),
        "skills": cv.get("skills", []) or user.get("skills", []) or [],
        "languages": cv.get("languages", []),
        "certifications": cv.get("certifications", []),
        "experience": [{k: e.get(k) for k in ("title", "company", "start", "end", "description", "bullets") if e.get(k)}
                       for e in cv.get("experience", [])][:5],
        "education": cv.get("education", [])[:4],
    }


def _a(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _fallback(p: dict, role: str, company: str) -> list:
    """No AI key / AI failed — simple answers built straight from the CV."""
    name = p["name"] or "I"
    exp = p["experience"][0] if p["experience"] else {}
    edu = p["education"][0] if p["education"] else {}
    skills = ", ".join(p["skills"][:4]) or "learning new things quickly"
    target = role or p["headline"] or "this role"
    about = (f"My name is {name}. " if p["name"] else "") \
        + (f"I'm {_a(p['headline'])}. " if p["headline"] else "") \
        + (f"I worked as {_a(exp.get('title'))} at {exp.get('company')}. " if exp.get("title") and exp.get("company") else "") \
        + (f"I studied {edu.get('qualification')} at {edu.get('institute')}. " if edu.get("qualification") and edu.get("institute") else "") \
        + f"My main skills are {skills}, and I'm now looking for a chance to grow as {_a(target)}."
    return [
        about,
        f"My main strengths are {skills}. " + (f"In my work as {_a(exp.get('title'))}, I used these every day to finish tasks on time." if exp.get("title")
                                               else "I use them to finish my work carefully and on time."),
        "Sometimes I spend too long trying to make my work perfect. I'm improving this by setting small deadlines for each task and asking for feedback early.",
        f"I want to work {('at ' + company) if company else 'here'} because this {target} position matches my skills in {skills}, "
        "and I believe I can learn a lot and add value to the team.",
        f"In five years I see myself as an experienced {target}, with stronger skills and more responsibility, helping the company grow.",
    ]


@router.post("/my-answers")
async def my_answers(body: dict, user_id: str = Depends(get_current_user)):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=401, detail="Please log in again")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=401, detail="Please log in again")
    email = user["email"].lower()
    role, company = _s(body.get("role")), _s(body.get("company"))

    doc = await cv_collection.find_one({"email": email}) or {}
    cv = _clean_cv(doc.get("cv"))
    has_cv = bool(cv.get("headline") or cv.get("skills") or cv.get("experience") or cv.get("education") or cv.get("summary"))
    p = _profile(user, cv)
    cv_at = doc.get("updatedAt")

    key = {"email": email, "role": role.lower(), "company": company.lower()}
    if not body.get("refresh"):
        old = await cache.find_one(key)
        if old and old.get("cv_at") == cv_at and len(old.get("answers", [])) == 5:
            return {"answers": old["answers"], "source": old.get("source", "builtin"), "has_cv": has_cv, "name": p["name"]}

    answers, source = _fallback(p, role, company), "builtin"
    if GEMINI_API_KEY or ANTHROPIC_API_KEY:
        prompt = (
            "You help a job seeker in Sri Lanka prepare for an interview. Write THEIR OWN answers to 5 common interview questions, "
            "using ONLY the facts in their profile below. Speak as them (first person), simple natural English, "
            "60-110 words per answer, ready to say out loud.\n"
            f"Job they are preparing for: {role or 'not given — use their headline / experience'}"
            + (f" at {company}" if company else "") + "\n"
            f"Their profile (JSON): {json.dumps(p, ensure_ascii=False)}\n\n"
            "Questions:\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(QUESTIONS)) + "\n\n"
            "Rules: do NOT invent jobs, companies, degrees, numbers or awards that are not in the profile. "
            "For the weakness, pick a believable small weakness and say how they are improving it. "
            "If the company is not given, say 'this company'. No emojis.\n"
            'Reply as JSON only: {"answers": ["...", "...", "...", "...", "..."]}'
        )
        try:
            text, provider = await _ask_ai(prompt)
            m = re.search(r"\{.*\}", text, re.S)
            got = (json.loads(m.group(0)).get("answers") if m else None) or []
            got = [re.sub(r"\s+", " ", str(a)).strip()[:900] for a in got][:5]
            if len(got) == 5 and all(len(a) > 20 for a in got):
                answers, source = got, provider
        except Exception as e:
            print("Interview prep AI error:", e)

    if source != "builtin" or not (GEMINI_API_KEY or ANTHROPIC_API_KEY):     # don't keep a fallback when the AI just had a hiccup
        await cache.update_one(key, {"$set": {**key, "answers": answers, "source": source, "cv_at": cv_at,
                                              "updatedAt": datetime.utcnow()}}, upsert=True)
    return {"answers": answers, "source": source, "has_cv": has_cv, "name": p["name"]}
