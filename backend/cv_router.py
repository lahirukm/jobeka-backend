"""
JobEka — CV builder.

  GET  /api/cv?email=                     saved CV (or {})
  PUT  /api/cv                            save CV  {email, cv, template}
  POST /api/cv/ai                         AI writes / improves the CV from the user's details
  POST /api/cv/render                     HTML preview of a CV in a template
  GET  /api/applications/{aid}/cv?employer_email=   employer views the CV sent with an application

AI uses Google Gemini (free tier) when GEMINI_API_KEY is set, or Claude when ANTHROPIC_API_KEY
is set; otherwise a built-in writer creates the summary and bullet points so the feature still works.
"""
import os, re, json, html
from datetime import datetime

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from database import database, jobs_collection

router = APIRouter(prefix="/api", tags=["CV builder"])
cv_collection           = database.get_collection("cv_profiles")
applications_collection = database.get_collection("applications")
users_collection        = database.get_collection("users")

# AI providers (first one with a key is used):
#   1. Google Gemini  — free tier key from Google AI Studio  → GEMINI_API_KEY
#   2. Anthropic Claude (paid)                              → ANTHROPIC_API_KEY
#   3. none → built-in writer
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = os.getenv("CV_GEMINI_MODEL", "gemini-2.5-flash")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL          = os.getenv("CV_AI_MODEL", "claude-haiku-4-5-20251001")
TEMPLATES         = ("modern", "classic", "creative")


# ─────────────────────────── helpers
def _clean_list(v, n=20):
    return [str(x).strip()[:80] for x in (v or []) if str(x).strip()][:n]

def _clean_cv(cv: dict) -> dict:
    cv = cv or {}
    def items(key, fields, n=10):
        out = []
        for it in (cv.get(key) or [])[:n]:
            if not isinstance(it, dict):
                continue
            row = {f: str(it.get(f) or "").strip()[:300] for f in fields}
            if key == "experience":
                row["bullets"] = [str(b).strip()[:220] for b in (it.get("bullets") or []) if str(b).strip()][:6]
            if any(row.get(f) for f in fields):
                out.append(row)
        return out
    return {
        "full_name": str(cv.get("full_name") or "").strip()[:80],
        "headline":  str(cv.get("headline") or "").strip()[:100],
        "email":     str(cv.get("email") or "").strip()[:100],
        "phone":     str(cv.get("phone") or "").strip()[:30],
        "address":   str(cv.get("address") or "").strip()[:150],
        "birthday":  str(cv.get("birthday") or "").strip()[:20],
        "summary":   str(cv.get("summary") or "").strip()[:1200],
        "about_me":  str(cv.get("about_me") or "").strip()[:1500],     # raw notes for the AI
        "skills":    _clean_list(cv.get("skills"), 25),
        "languages": _clean_list(cv.get("languages"), 10),
        "certifications": _clean_list(cv.get("certifications"), 10),
        "experience": items("experience", ("title", "company", "start", "end", "description")),
        "education":  items("education", ("qualification", "institute", "year", "result")),
    }

async def _job_brief(job_id):
    if not job_id or not ObjectId.is_valid(job_id):
        return None
    j = await jobs_collection.find_one({"_id": ObjectId(job_id)})
    if not j:
        return None
    return {"title": j.get("title", ""), "category": j.get("category", ""), "skills": j.get("required_skills") or [],
            "description": (j.get("description") or "")[:600], "requirements": (j.get("requirements") or "")[:600]}


# ─────────────────────────── save / load
@router.get("/cv")
async def get_cv(email: str):
    d = await cv_collection.find_one({"email": email.lower()})
    if not d:
        return {}
    return {"cv": d.get("cv", {}), "template": d.get("template", "modern"), "updatedAt": d.get("updatedAt").isoformat() if d.get("updatedAt") else None}

@router.put("/cv")
async def save_cv(body: dict):
    email = (body.get("email") or "").lower()
    if not await users_collection.find_one({"email": email}):
        raise HTTPException(status_code=403, detail="Please log in again")
    cv = _clean_cv(body.get("cv"))
    if not cv["full_name"]:
        raise HTTPException(status_code=400, detail="Add your full name")
    template = body.get("template") if body.get("template") in TEMPLATES else "modern"
    await cv_collection.update_one({"email": email}, {"$set": {"email": email, "cv": cv, "template": template, "updatedAt": datetime.utcnow()}}, upsert=True)
    return {"ok": True, "cv": cv, "template": template}


# ─────────────────────────── AI writer
PROMPT = """You are a professional CV writer for job seekers in Sri Lanka.
Using ONLY the facts in the candidate's details below, write a polished CV in clear, simple professional English.
Rules:
- Never invent employers, dates, degrees, certificates or numbers that are not in the details.
- If the candidate has little or no work experience, focus the summary on education, skills and attitude.
- "summary": 3–4 sentences, first person implied (no "I"), tailored to the target job if one is given.
- For each experience entry, write 2–4 short achievement-style bullet points based on what the candidate wrote.
- "skills": clean up and order the candidate's skills; you may add at most 3 skills that are clearly implied by their details.
- "headline": a short professional title (e.g. "Junior Software Developer").
Return ONLY valid JSON with this shape and nothing else:
{"headline": "", "summary": "", "skills": [], "experience": [{"title": "", "company": "", "start": "", "end": "", "bullets": []}]}

TARGET JOB: %s

CANDIDATE DETAILS (JSON):
%s"""

def _fallback(cv, job):
    """Simple built-in writer used when no AI key is configured or the AI call fails."""
    skills = cv["skills"]
    edu = cv["education"][0] if cv["education"] else None
    exp = cv["experience"]
    title = cv["headline"] or (exp[0]["title"] if exp and exp[0].get("title") else (job["title"] if job else "Job Seeker"))
    years = f"hands-on experience as {exp[0]['title']}" if exp and exp[0].get("title") else "a strong willingness to learn"
    parts = [f"Motivated {title.lower()} with {years}."]
    if skills:
        parts.append(f"Skilled in {', '.join(skills[:5])}.")
    if edu and edu.get("qualification"):
        parts.append(f"Holds {edu['qualification']}{' from ' + edu['institute'] if edu.get('institute') else ''}.")
    parts.append(f"Looking to contribute to {('a ' + job['title'] + ' role') if job else 'a growing team'} with reliability, teamwork and attention to detail.")
    new_exp = []
    for e in exp:
        bullets = e.get("bullets") or [s.strip()[0].upper() + s.strip()[1:] for s in re.split(r"[.\n;]+", e.get("description", "")) if len(s.strip()) > 3][:4]
        new_exp.append({**e, "bullets": bullets})
    return {"headline": title, "summary": " ".join(parts), "skills": skills, "experience": new_exp, "ai": "builtin"}

async def _ask_ai(prompt: str):
    """Send the prompt to Gemini (free) or Claude and return (text, provider)."""
    async with httpx.AsyncClient(timeout=40) as client:
        if GEMINI_API_KEY:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY, "content-type": "application/json"},
                json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}})
            r.raise_for_status()
            parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts), "gemini"
        r = await client.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"), "claude"


@router.post("/cv/ai")
async def ai_generate(body: dict):
    email = (body.get("email") or "").lower()
    if not await users_collection.find_one({"email": email}):
        raise HTTPException(status_code=403, detail="Please log in again")
    cv = _clean_cv(body.get("cv"))
    job = await _job_brief(body.get("job_id"))
    if not (GEMINI_API_KEY or ANTHROPIC_API_KEY):
        return {**_fallback(cv, job)}
    details = {k: cv[k] for k in ("full_name", "headline", "about_me", "skills", "languages", "certifications", "experience", "education")}
    prompt = PROMPT % (json.dumps(job) if job else "none", json.dumps(details, ensure_ascii=False))
    try:
        text, provider = await _ask_ai(prompt)
        m = re.search(r"\{.*\}", text, re.S)
        out = json.loads(m.group(0)) if m else {}
        # keep the user's own facts (company, dates) — only take the AI's wording
        exp = []
        for i, e in enumerate(cv["experience"]):
            ai_e = (out.get("experience") or [])[i] if i < len(out.get("experience") or []) else {}
            exp.append({**e, "bullets": [str(b)[:220] for b in (ai_e.get("bullets") or [])][:5] or e.get("bullets", [])})
        return {"headline": str(out.get("headline") or cv["headline"])[:100],
                "summary": str(out.get("summary") or "")[:1200],
                "skills": _clean_list(out.get("skills") or cv["skills"], 25),
                "experience": exp, "ai": provider}
    except Exception as e:
        print("CV AI error:", e)
        return {**_fallback(cv, job)}


# ─────────────────────────── templates
def _e(v):
    return html.escape(str(v or ""))

def render_cv(cv: dict, template: str = "modern") -> str:
    cv = _clean_cv(cv)
    t = template if template in TEMPLATES else "modern"
    accent = {"modern": "#2563EB", "classic": "#1F2937", "creative": "#F97316"}[t]
    contact = " · ".join(_e(x) for x in (cv["phone"], cv["email"], cv["address"]) if x)
    def exp_html():
        out = ""
        for e in cv["experience"]:
            dates = " – ".join(x for x in (_e(e.get("start")), _e(e.get("end"))) if x)
            bullets = "".join(f"<li>{_e(b)}</li>" for b in e.get("bullets") or [])
            desc = f"<p>{_e(e.get('description'))}</p>" if e.get("description") and not bullets else ""
            out += f"""<div class="item"><div class="row"><b>{_e(e.get('title'))}</b><span>{dates}</span></div>
                       <div class="sub">{_e(e.get('company'))}</div>{desc}<ul>{bullets}</ul></div>"""
        return out
    def edu_html():
        return "".join(f"""<div class="item"><div class="row"><b>{_e(e.get('qualification'))}</b><span>{_e(e.get('year'))}</span></div>
                           <div class="sub">{_e(e.get('institute'))}{(' · ' + _e(e.get('result'))) if e.get('result') else ''}</div></div>"""
                       for e in cv["education"])
    chips = "".join(f"<span class='chip'>{_e(s)}</span>" for s in cv["skills"])
    langs = ", ".join(_e(l) for l in cv["languages"])
    certs = "".join(f"<li>{_e(c)}</li>" for c in cv["certifications"])
    sections = ""
    if cv["summary"]:      sections += f"<h2>Profile</h2><p class='summary'>{_e(cv['summary'])}</p>"
    if cv["experience"]:   sections += f"<h2>Experience</h2>{exp_html()}"
    if cv["education"]:    sections += f"<h2>Education</h2>{edu_html()}"
    side = ""   # each side section is its own block so the Classic grid keeps title + content together
    if cv["skills"]:         side += f"<div class='blk'><h3>Skills</h3><div class='chips'>{chips}</div></div>"
    if langs:                side += f"<div class='blk'><h3>Languages</h3><p>{langs}</p></div>"
    if cv["certifications"]: side += f"<div class='blk'><h3>Certificates</h3><ul>{certs}</ul></div>"
    if cv["birthday"]:       side += f"<div class='blk'><h3>Date of birth</h3><p>{_e(cv['birthday'])}</p></div>"

    base_css = f"""*{{box-sizing:border-box;margin:0;padding:0}} body{{font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:#1F2937;background:#fff;font-size:13px;line-height:1.5}}
      h2{{font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:{accent};margin:18px 0 8px;padding-bottom:4px;border-bottom:2px solid {accent}22}}
      h3{{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;margin:14px 0 6px}} .item{{margin-bottom:10px}}
      .row{{display:flex;justify-content:space-between;gap:8px}} .row span{{color:#6B7280;font-size:12px;white-space:nowrap}}
      .sub{{color:#4B5563;font-size:12.5px}} ul{{margin:4px 0 0 18px}} li{{margin:2px 0}} .summary{{color:#374151}}
      .chips{{display:flex;flex-wrap:wrap;gap:5px}} .chip{{padding:3px 9px;border-radius:12px;font-size:11.5px}}
      @media print {{ body{{-webkit-print-color-adjust:exact;print-color-adjust:exact}} }}"""
    if t == "modern":
        css = base_css + f""" .wrap{{display:flex;min-height:100vh}} .side{{width:34%;background:#0B1B3F;color:#E2E8F0;padding:26px 18px}}
          .side h3{{color:#93C5FD}} .side .chip{{background:#1E3A8A;color:#DBEAFE}} .main{{flex:1;padding:26px 24px}}
          .name{{font-size:26px;font-weight:800;color:#0B1B3F}} .head{{color:{accent};font-weight:700;margin-top:2px}} .contact{{color:#6B7280;font-size:12px;margin-top:6px}}
          .avatar{{width:64px;height:64px;border-radius:50%;background:{accent};color:#fff;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:800;margin-bottom:10px}}"""
        initials = "".join(w[0] for w in cv["full_name"].split()[:2]).upper()
        body = f"""<div class='wrap'><div class='side'><div class='avatar'>{_e(initials)}</div>{side}</div>
          <div class='main'><div class='name'>{_e(cv['full_name'])}</div><div class='head'>{_e(cv['headline'])}</div>
          <div class='contact'>{contact}</div>{sections}</div></div>"""
    elif t == "classic":
        css = base_css + """ body{font-family:Georgia,'Times New Roman',serif} .page{padding:32px 36px;max-width:820px;margin:auto}
          .top{text-align:center;border-bottom:1.5px solid #111;padding-bottom:12px} .name{font-size:28px;letter-spacing:1px}
          .head{font-style:italic;color:#374151} .contact{font-size:12px;color:#4B5563;margin-top:4px} .chip{background:#F3F4F6;color:#111}
          .side{display:grid;grid-template-columns:repeat(2,1fr);gap:4px 28px;margin-top:6px;border-top:1px solid #E5E7EB;padding-top:4px}
          .side h3{font-size:12px;letter-spacing:1.5px;color:#111}"""
        body = f"""<div class='page'><div class='top'><div class='name'>{_e(cv['full_name'])}</div><div class='head'>{_e(cv['headline'])}</div>
          <div class='contact'>{contact}</div></div>{sections}<div class='side'>{side}</div></div>"""
    else:  # creative
        css = base_css + f""" .hero{{background:linear-gradient(135deg,#F97316,#FB923C 60%,#FDBA74);color:#fff;padding:28px 26px;border-radius:0 0 28px 0}}
          .name{{font-size:28px;font-weight:900;letter-spacing:-.5px}} .head{{font-weight:700;opacity:.95}} .contact{{font-size:12px;margin-top:8px;opacity:.95}}
          .cols{{display:flex;gap:22px;padding:6px 26px 26px}} .main{{flex:1.6}} .side{{flex:1}} .chip{{background:#FFEDD5;color:#9A3412}}
          .item{{border-left:3px solid #FDBA74;padding-left:10px}} h3{{color:#EA580C}}"""
        body = f"""<div class='hero'><div class='name'>{_e(cv['full_name'])}</div><div class='head'>{_e(cv['headline'])}</div>
          <div class='contact'>{contact}</div></div><div class='cols'><div class='main'>{sections}</div><div class='side'>{side}</div></div>"""
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
      <title>{_e(cv['full_name'])} – CV</title><style>{css}</style></head><body>{body}</body></html>"""

@router.post("/cv/render", response_class=HTMLResponse)
async def render(body: dict):
    return render_cv(body.get("cv") or {}, body.get("template") or "modern")

@router.get("/applications/{aid}/cv", response_class=HTMLResponse)
async def application_cv(aid: str, employer_email: str):
    try:
        a = await applications_collection.find_one({"_id": ObjectId(aid)})
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid id")
    if not a:
        raise HTTPException(status_code=404, detail="Application not found")
    if a.get("employer_email") != employer_email.lower():
        raise HTTPException(status_code=403, detail="Not your job")
    if not a.get("cv"):
        raise HTTPException(status_code=404, detail="This applicant did not attach a CV")
    return render_cv(a["cv"], a.get("cv_template", "modern"))
