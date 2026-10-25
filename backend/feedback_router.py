"""
JobEka — feedback on finished work, written with AI help.

The user picks 1–5 stars, types a few words (e.g. "fast clean friendly"), taps "Write with AI",
and Gemini turns them into 2–3 polite sentences that match the stars. The user can edit it, then save.

  POST /api/feedback/ai        {kind, item_id, keywords, rating?}   → {text, ai}
  POST /api/feedback           {kind, item_id, rating, keywords, text, ai_used}   save / update mine
  GET  /api/feedback/mine      my feedback  → {"<kind>:<item_id>": {...}}
  GET  /api/feedback/about?email=   reviews about this person → {count, average, breakdown, items}
  GET  /api/feedback/about-me        reviews about ME (provider / employer) → {count, new, average, breakdown, items}
                                     ?mark_seen=true marks them as read (the "Reviews about me" screen does this)

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
KIND_LABEL = {"help": "On-demand service", "booking": "Booking", "part": "Part-time job", "full": "Full-time job"}
STAR_WORD = {1: "very bad", 2: "bad", 3: "okay / mixed", 4: "good", 5: "excellent"}


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

def _about(email: str) -> dict:
    """Mongo filter for feedback about this email (case-insensitive — older rows may have capitals)."""
    return {"about_email": {"$regex": f"^{re.escape((email or '').strip())}$", "$options": "i"}}

def _first(name: str) -> str:
    return (name or "User").split(" ")[0]

def _short(name: str) -> str:
    """'Nimal Perera' → 'Nimal P.' — how real review apps show the writer."""
    parts = (name or "JobEka user").split()
    return parts[0] + (f" {parts[-1][0].upper()}." if len(parts) > 1 else "")

def _rating(v):
    try:
        r = int(v)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= 5 else None

def _summary(rows: list) -> dict:
    """Average stars + how many 5★, 4★ … 1★ (old reviews without stars are skipped)."""
    stars = [r["rating"] for r in rows if r.get("rating")]
    breakdown = {str(n): stars.count(n) for n in (5, 4, 3, 2, 1)}
    return {"average": round(sum(stars) / len(stars), 1) if stars else None, "rated": len(stars), "breakdown": breakdown}

def _row(f: dict, seen=None) -> dict:
    out = {"id": str(f["_id"]), "kind": f["kind"], "kind_label": KIND_LABEL.get(f["kind"], ""),
           "title": f.get("title", ""), "text": f["text"], "rating": f.get("rating"),
           "from": _short(f.get("from_name")), "edited": bool(f.get("createdAt") and f["updatedAt"] > f["createdAt"]),
           "at": f["updatedAt"].isoformat()}
    if seen is not None:
        out["new"] = f["updatedAt"] > seen
    return out

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
    if not words:
        raise HTTPException(status_code=400, detail="Pick a tag or type a few words, e.g. fast clean friendly")
    rating = _rating(body.get("rating"))
    it = await _item(body.get("kind"), body.get("item_id"), me["email"])
    if not (GEMINI_API_KEY or ANTHROPIC_API_KEY):
        return {"text": _fallback(words, it["title"], it["about_name"]), "ai": "builtin"}
    prompt = (
        "You help a user of JobEka, a Sri Lankan jobs and services app, write a short review.\n"
        f"They are reviewing a {WHAT[body.get('kind')]}: \"{it['title']}\""
        + (f" with {it['about_name']}" if it["about_name"] else "") + ".\n"
        + (f"Their star rating: {rating} out of 5 ({STAR_WORD[rating]}).\n" if rating else "")
        + f"Their words: {', '.join(words)}\n"
        "Write 2 or 3 short sentences in simple, polite English, in first person, that use the meaning of their words. "
        + ("Match the tone to the star rating. " if rating else "Work out from their words whether it was good, mixed or bad, and keep that tone. ")
        + "Do not invent facts, prices, names or dates they did not give. No emojis, no hashtags.\n"
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
    rating = _rating(body.get("rating"))
    if not rating:
        raise HTTPException(status_code=400, detail="Tap the stars to rate (1 to 5)")
    now = datetime.utcnow()
    doc = {"kind": kind, "item_id": item_id, "title": it["title"],
           "from_email": me["email"], "from_name": me.get("name", ""),
           "about_email": (it["about_email"] or "").lower().strip(), "about_name": it["about_name"],
           "rating": rating, "keywords": " ".join(_words(body.get("keywords"))), "text": text,
           "ai_used": bool(body.get("ai_used")), "updatedAt": now}
    await feedback_collection.update_one({"from_email": me["email"], "kind": kind, "item_id": item_id},
                                         {"$set": doc, "$setOnInsert": {"createdAt": now}}, upsert=True)
    return {"ok": True, "rating": rating, "text": text}


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    me = await _me(user_id)
    out = {}
    async for f in feedback_collection.find({"from_email": me["email"]}):
        out[f"{f['kind']}:{f['item_id']}"] = {"rating": f.get("rating"), "text": f["text"], "keywords": f.get("keywords", "")}
    return out


@router.get("/about")
async def about(email: str):
    """Public: what people said about a provider / employer — average stars + latest reviews."""
    items = [_row(f) async for f in feedback_collection.find(_about(email)).sort("updatedAt", -1).limit(100)]
    return {"count": len(items), **_summary(items), "items": items}


@router.get("/about-me")
async def about_me(mark_seen: bool = False, user_id: str = Depends(get_current_user)):
    """Feedback customers / workers / applicants wrote about me — shown on my dashboards and the Reviews screen."""
    me = await _me(user_id)
    seen = me.get("feedback_seen_at") or datetime(2000, 1, 1)
    items = [_row(f, seen) async for f in feedback_collection.find(_about(me["email"])).sort("updatedAt", -1).limit(200)]
    if mark_seen:
        await users_collection.update_one({"_id": me["_id"]}, {"$set": {"feedback_seen_at": datetime.utcnow()}})
    return {"count": len(items), "new": sum(1 for i in items if i["new"]), **_summary(items), "items": items}


async def feedback_notifications(email: str, since: datetime) -> list:
    """Used by /api/me/notifications — "New feedback from Nimal 💬" when someone reviews me."""
    out = []
    async for f in feedback_collection.find({**_about(email), "updatedAt": {"$gte": since}}):
        out.append({"id": f"fb-{f['_id']}-{int(f['updatedAt'].timestamp())}", "kind": "applications",
                    "title": (f"{'★' * f['rating']}{'☆' * (5 - f['rating'])}  New review from {_short(f.get('from_name'))}"
                              if f.get("rating") else f"New review from {_short(f.get('from_name'))} 💬"),
                    "body": f"{f.get('title', '')}: “{f['text'][:110]}{'…' if len(f['text']) > 110 else ''}”",
                    "icon": "chatbubble-ellipses", "color": "#7C3AED", "at": f["updatedAt"],
                    "link": {"path": "/my-reviews"}})
    return out
