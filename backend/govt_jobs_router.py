"""
JobEka — Government jobs (posted manually by the admin from the Gazette / official notices).

Each vacancy can have a poster image and the Gazette PDF. Files are stored in MongoDB
(Render's free disk is wiped on every deploy).

App:   GET  /api/govt-jobs?q=&category=&district=&closed=0     list (open ones first)
       GET  /api/govt-jobs/meta                                 categories + districts with counts
       GET  /api/govt-jobs/{id}                                 one vacancy (+1 view)
       GET  /api/govt-jobs/{id}/poster                          poster image
       GET  /api/govt-jobs/{id}/gazette                         Gazette PDF
Admin: GET    /api/admin/govt-jobs
       POST   /api/admin/govt-jobs                multipart: fields + poster? + gazette?
       PATCH  /api/admin/govt-jobs/{id}           multipart: any fields + poster? + gazette? (remove_poster / remove_gazette = "1")
       DELETE /api/admin/govt-jobs/{id}
"""
import os
import re
from datetime import datetime, date

from bson import ObjectId, Binary
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Form, Request
from fastapi.responses import Response
from pymongo import ReturnDocument

from database import database

router = APIRouter(tags=["Government jobs"])
govt_collection  = database.get_collection("govt_jobs")
files_collection = database.get_collection("govt_job_files")

ADMIN_KEY   = os.getenv("ADMIN_KEY", "jobeka_admin_2025")
POSTER_MAX  = 3 * 1024 * 1024
GAZETTE_MAX = 10 * 1024 * 1024
IMG_TYPES   = {"image/jpeg", "image/png", "image/webp"}

CATEGORIES = ["Education", "Health", "Police & Defence", "Administration", "Engineering", "Banking & Finance",
              "Agriculture", "Transport", "Management", "IT", "Other"]
FIELDS = {  # name: max length
    "title": 150, "title_si": 150, "organization": 150, "category": 40, "district": 40, "salary": 80,
    "positions": 20, "qualifications": 3000, "description": 3000, "apply_method": 10, "apply_link": 300,
    "address": 400, "gazette_no": 60, "gazette_date": 10, "closing_date": 10, "age_limit": 60,
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _admin(key):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

def _oid(v):
    try:
        return ObjectId(v)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid id")

def _today():
    return date.today().isoformat()

def _out(g: dict) -> dict:
    d = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in g.items()}
    d["_id"] = str(g["_id"])
    v = int(g.get("updatedAt", g.get("createdAt", datetime.utcnow())).timestamp())
    d["poster_url"]  = f"/api/govt-jobs/{d['_id']}/poster?v={v}" if g.get("has_poster") else ""
    d["gazette_url"] = f"/api/govt-jobs/{d['_id']}/gazette?v={v}" if g.get("has_gazette") else ""
    cd = g.get("closing_date") or ""
    d["is_open"] = (not cd) or cd >= _today()
    if cd:
        d["days_left"] = (date.fromisoformat(cd) - date.today()).days
    return d

def _clean(form: dict, partial: bool) -> dict:
    out = {}
    for k, mx in FIELDS.items():
        if k in form and form[k] is not None:
            out[k] = str(form[k]).strip()[:mx]
    if not partial and not out.get("title"):
        raise HTTPException(status_code=400, detail="Enter the job title")
    if not partial and not out.get("organization"):
        raise HTTPException(status_code=400, detail="Enter the ministry / department")
    for k in ("closing_date", "gazette_date"):
        if out.get(k) and not DATE_RE.match(out[k]):
            raise HTTPException(status_code=400, detail=f"{k.replace('_', ' ').title()} must be YYYY-MM-DD")
    if "apply_method" in out and out["apply_method"] not in ("post", "online", "both", ""):
        out["apply_method"] = "post"
    if out.get("apply_link") and not out["apply_link"].startswith(("http://", "https://")):
        out["apply_link"] = "https://" + out["apply_link"]
    if "category" in out and out["category"] and out["category"] not in CATEGORIES:
        out["category"] = "Other"
    return out

async def _store_file(job_id: str, kind: str, f: UploadFile):
    data = await f.read()
    ctype = (f.content_type or "").lower()
    if kind == "poster":
        if ctype not in IMG_TYPES:
            raise HTTPException(status_code=400, detail="Poster must be a JPG, PNG or WEBP image")
        if len(data) > POSTER_MAX:
            raise HTTPException(status_code=400, detail="Poster is larger than 3 MB")
    else:
        if ctype != "application/pdf":
            raise HTTPException(status_code=400, detail="Gazette must be a PDF file")
        if len(data) > GAZETTE_MAX:
            raise HTTPException(status_code=400, detail="Gazette PDF is larger than 10 MB")
    if not data:
        raise HTTPException(status_code=400, detail=f"The {kind} file is empty")
    await files_collection.update_one(
        {"job_id": job_id, "kind": kind},
        {"$set": {"data": Binary(data), "content_type": ctype, "size": len(data),
                  "name": (f.filename or kind)[-100:], "updatedAt": datetime.utcnow()}},
        upsert=True)
    return len(data)

async def _form_dict(request: Request) -> dict:
    form = await request.form()
    return {k: v for k, v in form.items() if not hasattr(v, "read")}


# ─────────────────────────── app
@router.get("/api/govt-jobs")
async def list_jobs(q: str = "", category: str = "", district: str = "", closed: int = 0):
    query = {"active": True}
    if category and category != "All":
        query["category"] = category
    if district and district != "All":
        query["district"] = {"$in": [district, "All Island", ""]}
    if not closed:
        query["$or"] = [{"closing_date": {"$gte": _today()}}, {"closing_date": ""}, {"closing_date": None}]
    items = []
    words = [w for w in q.lower().split() if w]
    async for g in govt_collection.find(query).sort("createdAt", -1).limit(300):
        if words:
            text = " ".join(str(g.get(k, "")) for k in ("title", "title_si", "organization", "category", "district", "qualifications")).lower()
            if not all(w in text for w in words):
                continue
        items.append(_out(g))
    # open vacancies first (closing soonest), closed ones last
    items.sort(key=lambda x: (not x["is_open"], x.get("closing_date") or "9999"))
    return items

@router.get("/api/govt-jobs/meta")
async def meta():
    cats, dists = {}, {}
    q = {"active": True, "$or": [{"closing_date": {"$gte": _today()}}, {"closing_date": ""}, {"closing_date": None}]}
    async for g in govt_collection.find(q, {"category": 1, "district": 1}):
        if g.get("category"):
            cats[g["category"]] = cats.get(g["category"], 0) + 1
        if g.get("district"):
            dists[g["district"]] = dists.get(g["district"], 0) + 1
    return {"categories": [{"name": k, "count": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])],
            "districts":  [{"name": k, "count": v} for k, v in sorted(dists.items(), key=lambda x: -x[1])],
            "all_categories": CATEGORIES}

@router.get("/api/govt-jobs/{gid}")
async def one_job(gid: str):
    g = await govt_collection.find_one_and_update({"_id": _oid(gid), "active": True}, {"$inc": {"views": 1}}, return_document=ReturnDocument.AFTER)
    if not g:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    return _out(g)

async def _file(gid: str, kind: str):
    f = await files_collection.find_one({"job_id": gid, "kind": kind})
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    name = re.sub(r'[^A-Za-z0-9._ -]', "_", f.get("name", kind))
    return Response(content=bytes(f["data"]), media_type=f["content_type"],
                    headers={"Cache-Control": "public, max-age=86400", "Content-Disposition": f'inline; filename="{name}"'})

@router.get("/api/govt-jobs/{gid}/poster")
async def poster(gid: str):
    return await _file(gid, "poster")

@router.get("/api/govt-jobs/{gid}/gazette")
async def gazette(gid: str):
    return await _file(gid, "gazette")


# ─────────────────────────── admin
@router.get("/api/admin/govt-jobs")
async def admin_list(x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    return [_out(g) async for g in govt_collection.find({}).sort("createdAt", -1)]

@router.post("/api/admin/govt-jobs")
async def admin_create(request: Request, poster: UploadFile = File(None), gazette: UploadFile = File(None),
                       x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    data = _clean(await _form_dict(request), partial=False)
    now = datetime.utcnow()
    doc = {**{k: "" for k in FIELDS}, **data, "apply_method": data.get("apply_method") or "post",
           "active": True, "views": 0, "has_poster": False, "has_gazette": False, "createdAt": now, "updatedAt": now}
    res = await govt_collection.insert_one(doc)
    gid = str(res.inserted_id)
    try:
        if poster and poster.filename:
            await _store_file(gid, "poster", poster); doc["has_poster"] = True
        if gazette and gazette.filename:
            await _store_file(gid, "gazette", gazette); doc["has_gazette"] = True
    except HTTPException:
        await govt_collection.delete_one({"_id": res.inserted_id})
        await files_collection.delete_many({"job_id": gid})
        raise
    await govt_collection.update_one({"_id": res.inserted_id}, {"$set": {"has_poster": doc["has_poster"], "has_gazette": doc["has_gazette"]}})
    doc["_id"] = res.inserted_id
    print(f"🏛️ Government job posted: {doc['title']}")
    return _out(doc)

@router.patch("/api/admin/govt-jobs/{gid}")
async def admin_update(gid: str, request: Request, poster: UploadFile = File(None), gazette: UploadFile = File(None),
                       x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    oid = _oid(gid)
    if not await govt_collection.find_one({"_id": oid}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Vacancy not found")
    raw = await _form_dict(request)
    upd = _clean(raw, partial=True)
    if "active" in raw:
        upd["active"] = str(raw["active"]).lower() in ("1", "true", "yes")
    if raw.get("remove_poster") == "1":
        await files_collection.delete_one({"job_id": gid, "kind": "poster"}); upd["has_poster"] = False
    if raw.get("remove_gazette") == "1":
        await files_collection.delete_one({"job_id": gid, "kind": "gazette"}); upd["has_gazette"] = False
    if poster and poster.filename:
        await _store_file(gid, "poster", poster); upd["has_poster"] = True
    if gazette and gazette.filename:
        await _store_file(gid, "gazette", gazette); upd["has_gazette"] = True
    upd["updatedAt"] = datetime.utcnow()
    await govt_collection.update_one({"_id": oid}, {"$set": upd})
    return _out(await govt_collection.find_one({"_id": oid}))

@router.delete("/api/admin/govt-jobs/{gid}")
async def admin_delete(gid: str, x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    res = await govt_collection.delete_one({"_id": _oid(gid)})
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    await files_collection.delete_many({"job_id": gid})
    return {"ok": True}
