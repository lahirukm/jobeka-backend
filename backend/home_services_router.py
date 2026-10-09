"""
JobEka — "Popular services" tiles on the personal Home screen.

The admin uploads a PNG icon for each tile from the admin dashboard.
Images live in MongoDB (Render's free disk is wiped on every deploy).

App:   GET    /api/home-services                    → active tiles in order
       GET    /api/home-services/{id}/image
Admin: GET    /api/admin/home-services
       POST   /api/admin/home-services              multipart: file, label, service, active
       PATCH  /api/admin/home-services/{id}         {label?, service?, active?, order?}
       DELETE /api/admin/home-services/{id}
"""
import os
from datetime import datetime

from bson import ObjectId, Binary
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import Response

from database import database

router = APIRouter(tags=["Home – popular services"])
tiles_collection = database.get_collection("home_services")

ADMIN_KEY  = os.getenv("ADMIN_KEY", "jobeka_admin_2025")
MAX_ACTIVE = 12
MAX_BYTES  = 1024 * 1024
ALLOWED    = {"image/png", "image/webp", "image/jpeg"}


def _admin(key):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

def _oid(v):
    try:
        return ObjectId(v)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid id")

def _public(t: dict) -> dict:
    return {
        "_id": str(t["_id"]),
        "label": t.get("label", ""),
        "service": t.get("service", ""),          # catalog id (e.g. "plumber") or any text to search
        "active": t.get("active", True),
        "order": t.get("order", 0),
        "size_kb": round(t.get("size", 0) / 1024),
        "image_url": f"/api/home-services/{t['_id']}/image?v={int(t.get('updatedAt', datetime.utcnow()).timestamp())}",
    }


# ─────────────────────────── app
@router.get("/api/home-services")
async def list_tiles():
    cur = tiles_collection.find({"active": True}, {"image": 0}).sort([("order", 1), ("createdAt", 1)]).limit(MAX_ACTIVE)
    return [_public(t) async for t in cur]

@router.get("/api/home-services/{tile_id}/image")
async def tile_image(tile_id: str):
    t = await tiles_collection.find_one({"_id": _oid(tile_id)})
    if not t:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=bytes(t["image"]), media_type=t.get("content_type", "image/png"),
                    headers={"Cache-Control": "public, max-age=86400"})


# ─────────────────────────── admin
@router.get("/api/admin/home-services")
async def admin_list(x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    cur = tiles_collection.find({}, {"image": 0}).sort([("order", 1), ("createdAt", 1)])
    return [_public(t) async for t in cur]

@router.post("/api/admin/home-services")
async def admin_create(file: UploadFile = File(...), label: str = Form(...), service: str = Form(""),
                       active: bool = Form(True), x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    if file.content_type not in ALLOWED:
        raise HTTPException(status_code=400, detail="Use a PNG (transparent background works best), WEBP or JPEG")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="Icon is larger than 1 MB")
    label = label.strip()[:24]
    if not label:
        raise HTTPException(status_code=400, detail="Enter a name for the tile")
    if active and await tiles_collection.count_documents({"active": True}) >= MAX_ACTIVE:
        raise HTTPException(status_code=400, detail=f"Only {MAX_ACTIVE} tiles can be shown. Hide one first.")
    last = await tiles_collection.find_one({}, sort=[("order", -1)])
    now = datetime.utcnow()
    doc = {"label": label, "service": (service or label).strip()[:60], "active": active,
           "order": (last or {}).get("order", 0) + 1, "image": Binary(data), "content_type": file.content_type,
           "size": len(data), "createdAt": now, "updatedAt": now}
    res = await tiles_collection.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _public(doc)

@router.patch("/api/admin/home-services/{tile_id}")
async def admin_update(tile_id: str, body: dict, x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    oid = _oid(tile_id)
    update = {k: body[k] for k in ("label", "service", "order", "active") if k in body}
    if update.get("active") is True and await tiles_collection.count_documents({"active": True, "_id": {"$ne": oid}}) >= MAX_ACTIVE:
        raise HTTPException(status_code=400, detail=f"Only {MAX_ACTIVE} tiles can be shown. Hide one first.")
    update["updatedAt"] = datetime.utcnow()
    res = await tiles_collection.update_one({"_id": oid}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}

@router.delete("/api/admin/home-services/{tile_id}")
async def admin_delete(tile_id: str, x_admin_key: str = Header(None)):
    _admin(x_admin_key)
    res = await tiles_collection.delete_one({"_id": _oid(tile_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}
