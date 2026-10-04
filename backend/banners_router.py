"""
JobEka — home-screen promo banners.

Images are stored in MongoDB (not on disk) because Render's free disk is wiped
on every deploy. Recommended banner size: 1200 x 500 px (PNG/JPG/WEBP, < 2 MB).

App:   GET  /api/banners                     → active banners (max 4, in order)
       GET  /api/banners/{id}/image          → the image itself
Admin: GET  /api/admin/banners               → all banners
       POST /api/admin/banners               → upload (multipart: file, title, link, active)
       PATCH /api/admin/banners/{id}         → {active?, order?, title?, link?}
       DELETE /api/admin/banners/{id}
"""
import os
from datetime import datetime

from bson import ObjectId, Binary
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import Response

from database import database

router = APIRouter(tags=["Banners"])
banners_collection = database.get_collection("banners")

ADMIN_KEY   = os.getenv("ADMIN_KEY", "jobeka_admin_2025")
MAX_ACTIVE  = 4
MAX_BYTES   = 2 * 1024 * 1024
ALLOWED     = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _admin(key):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

def _oid(v):
    try:
        return ObjectId(v)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid id")

def _public(b: dict) -> dict:
    """Banner info without the image bytes."""
    return {
        "_id": str(b["_id"]),
        "title": b.get("title", ""),
        "link": b.get("link", ""),
        "active": b.get("active", True),
        "order": b.get("order", 0),
        "content_type": b.get("content_type", "image/png"),
        "size_kb": round(b.get("size", 0) / 1024),
        "image_url": f"/api/banners/{b['_id']}/image?v={int(b.get('updatedAt', datetime.utcnow()).timestamp())}",
        "createdAt": b.get("createdAt").isoformat() if b.get("createdAt") else None,
    }

async def _active_count(exclude=None):
    q = {"active": True}
    if exclude:
        q["_id"] = {"$ne": exclude}
    return await banners_collection.count_documents(q)


# ─────────────────────────── app (public)
@router.get("/api/banners")
async def list_banners():
    cur = banners_collection.find({"active": True}, {"image": 0}).sort([("order", 1), ("createdAt", -1)]).limit(MAX_ACTIVE)
    return [_public(b) async for b in cur]

@router.get("/api/banners/{banner_id}/image")
async def banner_image(banner_id: str):
    b = await banners_collection.find_one({"_id": _oid(banner_id)})
    if not b:
        raise HTTPException(status_code=404, detail="Banner not found")
    return Response(content=bytes(b["image"]), media_type=b.get("content_type", "image/png"),
                    headers={"Cache-Control": "public, max-age=86400"})


# ─────────────────────────── admin
@router.get("/api/admin/banners")
async def admin_list(x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    cur = banners_collection.find({}, {"image": 0}).sort([("order", 1), ("createdAt", -1)])
    return [_public(b) async for b in cur]

@router.post("/api/admin/banners")
async def admin_upload(
    file: UploadFile = File(...),
    title: str = Form(""),
    link: str = Form(""),
    active: bool = Form(True),
    x_admin_key: str = Header(None),
):
    _admin(x_admin_key)
    if file.content_type not in ALLOWED:
        raise HTTPException(status_code=400, detail="Only PNG, JPG or WEBP images are allowed")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="Image is larger than 2 MB. Please compress it.")
    if active and await _active_count() >= MAX_ACTIVE:
        raise HTTPException(status_code=400, detail=f"Only {MAX_ACTIVE} banners can be active. Turn one off first.")
    last = await banners_collection.find_one({}, sort=[("order", -1)])
    now = datetime.utcnow()
    doc = {
        "title": title.strip()[:80], "link": link.strip()[:300], "active": active,
        "order": (last or {}).get("order", 0) + 1,
        "image": Binary(data), "content_type": file.content_type, "size": len(data),
        "createdAt": now, "updatedAt": now,
    }
    res = await banners_collection.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _public(doc)

@router.patch("/api/admin/banners/{banner_id}")
async def admin_update(banner_id: str, body: dict, x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    oid = _oid(banner_id)
    update = {k: body[k] for k in ("title", "link", "order", "active") if k in body}
    if update.get("active") is True and await _active_count(exclude=oid) >= MAX_ACTIVE:
        raise HTTPException(status_code=400, detail=f"Only {MAX_ACTIVE} banners can be active. Turn one off first.")
    update["updatedAt"] = datetime.utcnow()
    res = await banners_collection.update_one({"_id": oid}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Banner not found")
    return {"ok": True}

@router.delete("/api/admin/banners/{banner_id}")
async def admin_delete(banner_id: str, x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    res = await banners_collection.delete_one({"_id": _oid(banner_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Banner not found")
    return {"ok": True}
