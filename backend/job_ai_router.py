"""
JobEka — "✨ Write with AI" on the Post new job form.

The employer types the Job title + Category (+ anything else already filled in),
taps the button, and Gemini writes the Job description and the Requirements.

  POST /api/job-ai/write   (login token)
       {title, category, type, salary?, working_hours?, location?,
        required_skills?, experience_years?, education_level?, languages?}
    → {description, requirements, ai: "gemini" | "claude" | "builtin"}
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from cv_router import _ask_ai, GEMINI_API_KEY, ANTHROPIC_API_KEY     # same Gemini helper as the CV builder

router = APIRouter(prefix="/api/job-ai", tags=["Jobs — AI writer"])


def _s(v, n=120) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]

def _list(v) -> list:
    return [_s(x, 40) for x in (v or []) if _s(x, 40)][:12] if isinstance(v, list) else []

def _fallback(d: dict) -> dict:
    """No AI key / AI failed — still gives a usable starting text."""
    part = d["type"].lower() == "part time"
    desc = (f"We are looking for a reliable {d['title']} to join us"
            + (f" in {d['location']}" if d["location"] else "") + ". "
            + ("This is a short part-time job — please arrive on time and finish the work carefully."
               if part else f"You will work in our {d['category']} team and help us grow."))
    req = [f"Experience as a {d['title']} is an advantage"]
    if d["experience_years"]:
        req[0] = f"At least {d['experience_years']} year(s) of experience as a {d['title']}"
    if d["skills"]:
        req.append("Skills: " + ", ".join(d["skills"]))
    if d["education_level"]:
        req.append(f"Education: {d['education_level']} or above")
    if d["languages"]:
        req.append("Languages: " + ", ".join(d["languages"]))
    req.append("Honest, punctual and able to work with others")
    return {"description": desc, "requirements": "\n".join("• " + r for r in req)}


@router.post("/write")
async def ai_write(body: dict, user_id: str = Depends(get_current_user)):
    d = {"title": _s(body.get("title"), 80), "category": _s(body.get("category"), 60),
         "type": _s(body.get("type"), 30) or "Full Time", "salary": _s(body.get("salary"), 60),
         "working_hours": _s(body.get("working_hours"), 60), "location": _s(body.get("location"), 80),
         "skills": _list(body.get("required_skills")), "experience_years": _s(body.get("experience_years"), 5),
         "education_level": _s(body.get("education_level"), 60), "languages": _list(body.get("languages"))}
    if len(d["title"]) < 3:
        raise HTTPException(status_code=400, detail="Type the job title first")
    if not (GEMINI_API_KEY or ANTHROPIC_API_KEY):
        return {**_fallback(d), "ai": "builtin"}

    part = d["type"].lower() == "part time"
    known = {k: v for k, v in d.items() if v}
    prompt = (
        "You write job posts for JobEka, a Sri Lankan jobs app. Use simple, clear English that ordinary job seekers understand.\n"
        f"Job details from the employer (JSON): {json.dumps(known, ensure_ascii=False)}\n\n"
        + ("This is a PART-TIME / one-day job (e.g. cleaning, delivery, helper). Keep it short and practical.\n"
           "description: 2-3 sentences — what the worker will do, and anything to bring or know.\n"
           "requirements: 3-4 short bullet points.\n"
           if part else
           "This is a FULL-TIME / contract job.\n"
           "description: 3-4 sentences — about the role and the main day-to-day responsibilities.\n"
           "requirements: 5-6 short bullet points — skills, experience, education, languages, personal qualities.\n")
        + "Use the skills, experience, education and languages given above in the requirements when they exist; "
          "otherwise choose the ones that are normal for this job title in Sri Lanka.\n"
          "Do NOT invent the company name, salary, address, phone number or benefits. No emojis, no hashtags.\n"
          "Each bullet point starts with \"• \" and bullets are separated by \\n.\n"
          'Reply as JSON only: {"description": "...", "requirements": "• ...\\n• ..."}'
    )
    try:
        text, provider = await _ask_ai(prompt)
        m = re.search(r"\{.*\}", text, re.S)
        j = json.loads(m.group(0)) if m else {}
        desc = re.sub(r"[ \t]+", " ", str(j.get("description") or "")).strip()[:1200]
        req = j.get("requirements") or ""
        if isinstance(req, list):
            req = "\n".join(str(x) for x in req)
        lines = [re.sub(r"^\s*(?:[•\-\*]+|\d+[\.\)])\s*", "", ln).strip() for ln in str(req).splitlines()]
        req = "\n".join("• " + ln for ln in lines if ln)[:1200]
        if len(desc) < 20 or not req:
            raise ValueError("empty answer")
        return {"description": desc, "requirements": req, "ai": provider}
    except Exception as e:
        print("Job AI writer error:", e)
        return {**_fallback(d), "ai": "builtin"}
