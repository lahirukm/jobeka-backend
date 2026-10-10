"""
JobEka — "My account" features on the Profile screen.
Every endpoint uses the login token (Authorization: Bearer <token>).

Saved jobs
  GET    /api/me/saved-jobs                 saved jobs (newest first, with job details)
  GET    /api/me/saved-jobs/ids             just the ids (to show filled bookmarks)
  POST   /api/me/saved-jobs/{job_id}
  DELETE /api/me/saved-jobs/{job_id}

Notifications (built from what really happened to the user)
  GET    /api/me/notifications              {items, unread}
  POST   /api/me/notifications/seen         mark all as read
  GET    /api/me/notification-settings
  PUT    /api/me/notification-settings      {applications, payments, new_jobs, offers}

Privacy & security
  POST   /api/me/change-password            {current_password, new_password}
  DELETE /api/me/account                    {password}

Help & support
  POST   /api/me/support                    {category, subject, message}
  GET    /api/me/support                    my tickets + JobEka's replies
  Admin: GET /api/admin/support-tickets · PATCH /api/admin/support-tickets/{id} {status?, reply?}
"""
import os
from datetime import datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Depends, Header

from database import database, jobs_collection
from auth import get_current_user, hash_password, verify_password

router = APIRouter(tags=["My account"])

users_collection        = database.get_collection("users")
saved_collection        = database.get_collection("saved_jobs")
applications_collection = database.get_collection("applications")
transactions_collection = database.get_collection("transactions")
withdrawals_collection  = database.get_collection("withdrawals")
tickets_collection      = database.get_collection("support_tickets")

ADMIN_KEY = os.getenv("ADMIN_KEY", "jobeka_admin_2025")
DEFAULT_PREFS = {"applications": True, "payments": True, "new_jobs": True, "offers": True}
CATEGORIES = {"payment", "job", "service", "account", "bug", "other"}


def _oid(v):
    try:
        return ObjectId(v)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid id")

async def _me(user_id: str) -> dict:
    u = await users_collection.find_one({"_id": _oid(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return u

def _iso(d):
    return d.isoformat() if isinstance(d, datetime) else d

def _job_out(j: dict) -> dict:
    d = {k: _iso(v) for k, v in j.items() if k not in ("arrival",)}
    d["_id"] = str(j["_id"])
    return d


# ─────────────────────────── saved jobs
@router.get("/api/me/saved-jobs/ids")
async def saved_ids(user_id: str = Depends(get_current_user)):
    return [s["job_id"] async for s in saved_collection.find({"user_id": user_id}, {"job_id": 1})]

@router.get("/api/me/saved-jobs")
async def saved_jobs(user_id: str = Depends(get_current_user)):
    out = []
    async for s in saved_collection.find({"user_id": user_id}).sort("createdAt", -1):
        job = await jobs_collection.find_one({"_id": ObjectId(s["job_id"])}) if ObjectId.is_valid(s["job_id"]) else None
        if not job:                                   # job was deleted → forget it
            await saved_collection.delete_one({"_id": s["_id"]})
            continue
        d = _job_out(job)
        d["savedAt"] = _iso(s["createdAt"])
        d["is_open"] = job.get("status", "active") == "active"
        out.append(d)
    return out

@router.post("/api/me/saved-jobs/{job_id}")
async def save_job(job_id: str, user_id: str = Depends(get_current_user)):
    if not await jobs_collection.find_one({"_id": _oid(job_id)}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Job not found")
    await saved_collection.update_one({"user_id": user_id, "job_id": job_id},
                                      {"$setOnInsert": {"createdAt": datetime.utcnow()}}, upsert=True)
    return {"saved": True}

@router.delete("/api/me/saved-jobs/{job_id}")
async def unsave_job(job_id: str, user_id: str = Depends(get_current_user)):
    await saved_collection.delete_one({"user_id": user_id, "job_id": job_id})
    return {"saved": False}


# ─────────────────────────── notifications
APP_TEXT = {
    "shortlisted": ("You're shortlisted! 🎉", "{company} shortlisted you for {title}. They may contact you soon.", "star", "#F59E0B"),
    "interview":   ("Interview invitation 📅", "{company} invited you to an interview for {title}. Check the date & place.", "calendar", "#7C3AED"),
    "hired":       ("You got the job! 🥳",   "{company} hired you for {title}. See your first working day.",      "trophy", "#16A34A"),
    "rejected":    ("Application update",    "{company} chose another candidate for {title}. Keep applying!",      "close-circle", "#64748B"),
}

def _money(n):
    return f"LKR {float(n or 0):,.2f}"

@router.get("/api/me/notifications")
async def notifications(user_id: str = Depends(get_current_user)):
    u = await _me(user_id)
    email = u["email"]
    prefs = {**DEFAULT_PREFS, **(u.get("notif_prefs") or {})}
    seen = u.get("notif_seen_at") or datetime(2000, 1, 1)
    since = datetime.utcnow() - timedelta(days=60)
    items = []

    if prefs["applications"]:
        async for a in applications_collection.find({"email": email, "status": {"$in": list(APP_TEXT)}, "updatedAt": {"$gte": since}}):
            t, body, icon, color = APP_TEXT[a["status"]]
            items.append({"id": f"app-{a['_id']}-{a['status']}", "kind": "applications", "title": t,
                          "body": body.format(company=a.get("company") or "The employer", title=a.get("job_title") or "the job"),
                          "icon": icon, "color": color, "at": a["updatedAt"],
                          "link": {"path": "/my-applications"}})

    if prefs["payments"]:
        TX = {
            "credit":     ("Payment received",      "{amt} was added to your wallet.",                   "arrow-down-circle", "#16A34A"),
            "cash":       ("Cash job completed",    "You were paid {amt} in cash.",                       "cash",              "#16A34A"),
            "commission": ("JobEka fee",            "{amt} service fee (6%) for a cash job.",             "pie-chart",         "#F97316"),
            "refund":     ("Withdrawal refunded",   "{amt} was returned to your wallet.",                 "refresh-circle",    "#2563EB"),
            "card_payment": ("Payment successful",  "You paid {amt} by card.",                             "card",              "#2563EB"),
        }
        async for t in transactions_collection.find({"email": email, "type": {"$in": list(TX)}, "createdAt": {"$gte": since}}):
            title, body, icon, color = TX[t["type"]]
            items.append({"id": f"tx-{t['_id']}", "kind": "payments", "title": title, "body": body.format(amt=_money(t.get("amount"))),
                          "icon": icon, "color": color, "at": t["createdAt"], "link": {"path": "/wallet"}})
        async for w in withdrawals_collection.find({"email": email, "status": {"$in": ["paid", "rejected"]}}):
            at = w.get("processedAt") or w.get("createdAt")
            if not at or at < since:
                continue
            paid = w["status"] == "paid"
            items.append({"id": f"wd-{w['_id']}-{w['status']}", "kind": "payments",
                          "title": "Withdrawal sent 🏦" if paid else "Withdrawal rejected",
                          "body": f"{_money(w.get('amount'))} " + ("was transferred to your bank account." if paid else "was returned to your wallet. Check your bank details."),
                          "icon": "business" if paid else "alert-circle", "color": "#16A34A" if paid else "#DC2626",
                          "at": at, "link": {"path": "/wallet"}})

    if prefs["new_jobs"]:
        recent = datetime.utcnow() - timedelta(days=3)
        n = await jobs_collection.count_documents({"status": "active", "createdAt": {"$gte": recent}, "employer_email": {"$ne": email}})
        if n:
            newest = await jobs_collection.find_one({"status": "active", "createdAt": {"$gte": recent}}, sort=[("createdAt", -1)])
            items.append({"id": f"jobs-{newest['_id']}", "kind": "new_jobs", "title": f"{n} new job{'s' if n > 1 else ''} posted",
                          "body": f"Latest: {newest.get('title', 'a new job')}{' · ' + newest['location'] if newest.get('location') else ''}",
                          "icon": "briefcase", "color": "#F97316", "at": newest["createdAt"], "link": {"path": "/(tabs)/jobs"}})

    async for tk in tickets_collection.find({"user_id": user_id, "reply": {"$nin": [None, ""]}, "repliedAt": {"$gte": since}}):
        items.append({"id": f"tk-{tk['_id']}-{int(tk['repliedAt'].timestamp())}", "kind": "support", "title": "JobEka Support replied",
                      "body": tk["reply"][:140], "icon": "chatbubbles", "color": "#7C3AED", "at": tk["repliedAt"],
                      "link": {"path": "/support"}})

    items.sort(key=lambda x: x["at"], reverse=True)
    items = items[:60]
    for it in items:
        it["unread"] = it["at"] > seen
        it["at"] = _iso(it["at"])
    return {"items": items, "unread": sum(1 for it in items if it["unread"])}

@router.post("/api/me/notifications/seen")
async def notifications_seen(user_id: str = Depends(get_current_user)):
    await users_collection.update_one({"_id": _oid(user_id)}, {"$set": {"notif_seen_at": datetime.utcnow()}})
    return {"ok": True}

@router.get("/api/me/notification-settings")
async def get_settings(user_id: str = Depends(get_current_user)):
    u = await _me(user_id)
    return {**DEFAULT_PREFS, **(u.get("notif_prefs") or {})}

@router.put("/api/me/notification-settings")
async def put_settings(body: dict, user_id: str = Depends(get_current_user)):
    u = await _me(user_id)
    prefs = {**DEFAULT_PREFS, **(u.get("notif_prefs") or {})}
    for k in DEFAULT_PREFS:
        if k in body:
            prefs[k] = bool(body[k])
    await users_collection.update_one({"_id": u["_id"]}, {"$set": {"notif_prefs": prefs}})
    return prefs


# ─────────────────────────── privacy & security
@router.post("/api/me/change-password")
async def change_password(body: dict, user_id: str = Depends(get_current_user)):
    u = await _me(user_id)
    cur, new = body.get("current_password") or "", body.get("new_password") or ""
    if not verify_password(cur, u["password"]):
        raise HTTPException(status_code=400, detail="Current password is wrong")
    if len(new) < 8 or new.isdigit() or new.isalpha():
        raise HTTPException(status_code=400, detail="Use at least 8 characters with letters and numbers")
    if verify_password(new, u["password"]):
        raise HTTPException(status_code=400, detail="New password must be different")
    await users_collection.update_one({"_id": u["_id"]}, {"$set": {"password": hash_password(new), "passwordChangedAt": datetime.utcnow()}})
    return {"ok": True}

@router.delete("/api/me/account")
async def delete_account(body: dict, user_id: str = Depends(get_current_user)):
    u = await _me(user_id)
    if not verify_password(body.get("password") or "", u["password"]):
        raise HTTPException(status_code=400, detail="Password is wrong")
    bal = round(u.get("wallet_balance", 0), 2)
    if bal > 0:
        raise HTTPException(status_code=400, detail=f"Withdraw your wallet balance (LKR {bal:,.2f}) before deleting your account")
    if bal < 0:
        raise HTTPException(status_code=400, detail=f"Please settle the JobEka fees you owe (LKR {abs(bal):,.2f}) first")
    if await withdrawals_collection.count_documents({"email": u["email"], "status": "pending"}):
        raise HTTPException(status_code=400, detail="You have a withdrawal in progress. Try again after it is paid.")
    email = u["email"]
    await jobs_collection.update_many({"employer_email": email, "status": "active"}, {"$set": {"status": "closed"}})
    for col in ("saved_jobs", "avatars"):
        await database.get_collection(col).delete_many({"user_id": user_id})
    await database.get_collection("cv_profiles").delete_many({"email": email})
    await database.get_collection("cv_files").delete_many({"email": email})
    await applications_collection.update_many({"email": email, "status": "applied"}, {"$set": {"status": "withdrawn"}})
    await users_collection.delete_one({"_id": u["_id"]})
    print(f"🗑️ Account deleted: {email}")
    return {"deleted": True}


# ─────────────────────────── help & support
@router.post("/api/me/support")
async def create_ticket(body: dict, user_id: str = Depends(get_current_user)):
    u = await _me(user_id)
    cat = (body.get("category") or "other").lower()
    subject = (body.get("subject") or "").strip()[:100]
    message = (body.get("message") or "").strip()[:2000]
    if cat not in CATEGORIES:
        cat = "other"
    if len(subject) < 3 or len(message) < 10:
        raise HTTPException(status_code=400, detail="Add a short subject and describe the problem (at least 10 characters)")
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    if await tickets_collection.count_documents({"user_id": user_id, "createdAt": {"$gte": hour_ago}}) >= 5:
        raise HTTPException(status_code=429, detail="Too many requests — please wait a little")
    now = datetime.utcnow()
    doc = {"user_id": user_id, "email": u["email"], "name": u.get("name", ""), "phone": u.get("phone", ""), "role": u.get("role", ""),
           "category": cat, "subject": subject, "message": message, "status": "open", "reply": "", "createdAt": now, "updatedAt": now}
    res = await tickets_collection.insert_one(doc)
    return {"_id": str(res.inserted_id), "ticket_no": str(res.inserted_id)[-6:].upper(), "status": "open"}

def _ticket_out(t):
    d = {k: _iso(v) for k, v in t.items()}
    d["_id"] = str(t["_id"])
    d["ticket_no"] = d["_id"][-6:].upper()
    return d

@router.get("/api/me/support")
async def my_tickets(user_id: str = Depends(get_current_user)):
    return [_ticket_out(t) async for t in tickets_collection.find({"user_id": user_id}).sort("createdAt", -1).limit(30)]

@router.get("/api/admin/support-tickets")
async def admin_tickets(x_admin_key: str = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return [_ticket_out(t) async for t in tickets_collection.find({}).sort("createdAt", -1).limit(300)]

@router.patch("/api/admin/support-tickets/{tid}")
async def admin_update_ticket(tid: str, body: dict, x_admin_key: str = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    upd = {"updatedAt": datetime.utcnow()}
    if body.get("status") in ("open", "in_progress", "resolved"):
        upd["status"] = body["status"]
    if "reply" in body:
        upd["reply"] = (body.get("reply") or "").strip()[:2000]
        upd["repliedAt"] = datetime.utcnow()
        upd.setdefault("status", "in_progress" if upd["reply"] else "open")
    res = await tickets_collection.update_one({"_id": _oid(tid)}, {"$set": upd})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"ok": True}
