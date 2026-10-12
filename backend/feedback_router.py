"""
JobEka — feedback on finished work, written with AI help.

The user types a few words (e.g. "fast clean friendly cheap"), taps "AI write",
and Gemini turns them into 2–3 polite sentences. The user can edit it, then save.

  POST /api/feedback/ai        {kind, item_id, keywords}   → {text, ai}
  POST /api/feedback           {kind, item_id, keywords, text, ai_used}   save / update mine
  GET  /api/feedback/mine      my feedback  → {"<kind>:<item_id>": {...}}
  GET  /api/feedback/about?email=   feedback other people wrote about this person

kind = "help" (Request now) · "booking" (service booking) · "part" (part-time job) · "full" (full-time application)
All need the login token. The backend checks the item really belongs to the user and finds who it is about.
"""
import json
import re
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import database, jobs_collection
from cv_router import _ask_ai, GEMINI_API_KEY, ANTHROPIC_API_KEY     # same Gemini helper as the CV builder

router = APIRouter(prefix="/api/feedback", tags=["Feedback"])
feedback_collection = database.get_collection("feedback")
users_collection    = database.get_collection("users")

KINDS = ("help", "booking", "part", "full")
WHAT = {"help": "on-demand service", "booking": "booked service", "part": "part-time job", "full": "job application and hiring process"}
DONE_APP = ("hired", "rejected", "interview")


def _oid(v):
    if not ObjectId.is_valid(str(v)):
        raise HTTPException(status_code=400, detail="Invalid id")
    return ObjectId(str(v))

async def _me(user_id: str) -> dict:
    u = await users_collection.find_one({"_id": _oid(user_id)})
    if not u:
        raise HTTPException(status_code=401, detail="Please log in again")
    return u

async def _item(kind: str, item_id: str, email: str) -> dict:
    """Finds the finished item, checks it is the user's, and says who the feedback is about."""
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Unknown item type")
    oid = _oid(item_id)
    if kind == "help":
        r = await database.get_collection("service_requests").find_one({"_id": oid})
        if not r or r.get("customer_email") != email:
            raise HTTPException(status_code=404, detail="Request not found")
        if r.get("status") != "completed":
            raise HTTPException(status_code=400, detail="You can give feedback after the service is completed")
        p = r.get("provider") or {}
        return {"title": str(r.get("service_type", "service")).replace("_", " "), "about_email": p.get("email", ""), "about_name": p.get("name", "")}
    if kind == "booking":
        b = await database.get_collection("bookings").find_one({"_id": oid})
        if not b or b.get("customer_email") != email:
            raise HTTPException(status_code=404, detail="Booking not found")
        if b.get("status") != "completed":
            raise HTTPException(status_code=400, detail="You can give feedback after the booking is done")
        return {"title": str(b.get("service", "service")).replace("_", " "), "about_email": b.get("provider_email", ""), "about_name": b.get("provider_name", "")}
    if kind == "part":
        j = await jobs_collection.find_one({"_id": oid})
        if not j or ((j.get("appliedBy") or {}).get("email") or "").lower() != email:
            raise HTTPException(status_code=404, detail="Job not found")
        if j.get("status") != "closed":
            raise HTTPException(status_code=400, detail="You can give feedback after the job is completed")
        return {"title": j.get("title", "job"), "about_email": (j.get("employer_email") or "").lower(), "about_name": j.get("employer_name", "")}
    a = await database.get_collection("applications").find_one({"_id": oid})
    if not a or a.get("email") != email:
        raise HTTPException(status_code=404, detail="Application not found")
    if a.get("status") not in DONE_APP:
        raise HTTPException(status_code=400, detail="You can give feedback after the interview or the employer's decision")
    return {"title": a.get("job_title", "job"), "about_email": a.get("employer_email", ""), "about_name": a.get("company", "")}

def _words(keywords: str) -> list:
    return [w for w in re.split(r"[\s,]+", (keywords or "").strip()) if w][:12]

def _fallback(words: list, title: str, name: str) -> str:
    """Used when no AI key is set or the AI fails — still gives a sensible sentence."""
    who = f" with {name}" if name else ""
    return f"My experience{who} for {title}: {', '.join(words)}."


# ─────────────────────────── AI writes the feedback from a few words
@router.post("/ai")
async def ai_write(body: dict, user_id: str = Depends(get_current_user)):
    me = await _me(user_id)
    words = _words(body.get("keywords"))
    if not 1 <= len(words) <= 8:
        raise HTTPException(status_code=400, detail="Type 1 to 8 words, e.g. fast clean friendly")
    it = await _item(body.get("kind"), body.get("item_id"), me["email"])
    if not (GEMINI_API_KEY or ANTHROPIC_API_KEY):
        return {"text": _fallback(words, it["title"], it["about_name"]), "ai": "builtin"}
    prompt = (
        "You help a user of JobEka, a Sri Lankan jobs and services app, write a short review.\n"
        f"They are reviewing a {WHAT[body.get('kind')]}: \"{it['title']}\""
        + (f" with {it['about_name']}" if it["about_name"] else "") + ".\n"
        f"Their words: {', '.join(words)}\n"
        "Write 2 or 3 short sentences in simple, polite English, in first person, that use the meaning of their words. "
        "Work out from their words whether it was good, mixed or bad, and keep that tone. Do not invent facts, prices, names or dates they did not give. No emojis, no hashtags.\n"
        'Reply as JSON only: {"feedback": "..."}'
    )
    try:
        text, provider = await _ask_ai(prompt)
        m = re.search(r"\{.*\}", text, re.S)
        out = (json.loads(m.group(0)).get("feedback") if m else text) or ""
        out = re.sub(r"\s+", " ", str(out)).strip().strip('"')[:600]
        if not out:
            raise ValueError("empty")
        return {"text": out, "ai": provider}
    except Exception as e:
        print("Feedback AI error:", e)
        return {"text": _fallback(words, it["title"], it["about_name"]), "ai": "builtin"}


# ─────────────────────────── save (one feedback per item — saving again updates it)
@router.post("")
async def save(body: dict, user_id: str = Depends(get_current_user)):
    me = await _me(user_id)
    kind, item_id = body.get("kind"), str(body.get("item_id") or "")
    it = await _item(kind, item_id, me["email"])
    text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()[:600]
    if len(text) < 5:
        raise HTTPException(status_code=400, detail="Write a few words of feedback")
    now = datetime.utcnow()
    doc = {"kind": kind, "item_id": item_id, "title": it["title"],
           "from_email": me["email"], "from_name": me.get("name", ""),
           "about_email": it["about_email"], "about_name": it["about_name"],
           "keywords": " ".join(_words(body.get("keywords"))), "text": text,
           "ai_used": bool(body.get("ai_used")), "updatedAt": now}
    await feedback_collection.update_one({"from_email": me["email"], "kind": kind, "item_id": item_id},
                                         {"$set": doc, "$setOnInsert": {"createdAt": now}}, upsert=True)
    return {"ok": True, "text": text}


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    me = await _me(user_id)
    out = {}
    async for f in feedback_collection.find({"from_email": me["email"]}):
        out[f"{f['kind']}:{f['item_id']}"] = {"text": f["text"], "keywords": f.get("keywords", "")}
    return out


@router.get("/about")
async def about(email: str):
    """Public: what people said about a provider / employer — latest reviews."""
    items = []
    async for f in feedback_collection.find({"about_email": email.lower().strip()}).sort("updatedAt", -1).limit(50):
        items.append({"text": f["text"], "from": (f.get("from_name") or "User").split(" ")[0],
                      "title": f.get("title", ""), "at": f["updatedAt"].isoformat()})
    return {"count": len(items), "items": items}
