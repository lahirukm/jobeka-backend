from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.responses import Response
from bson import ObjectId, Binary
from bson.errors import InvalidId
from datetime import datetime

from database import database
from user_schema import UserRegister, UserLogin, TokenOut
from auth import hash_password, verify_password, create_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["Auth"])

users_collection   = database.get_collection("users")
avatars_collection = database.get_collection("avatars")     # profile pictures (Render disk is wiped on deploy)

AVATAR_MAX   = 2 * 1024 * 1024
AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}


def user_to_dict(user: dict) -> dict:
    """Convert MongoDB user doc to JSON-safe dict (remove password)."""
    u = dict(user)
    u["_id"] = str(u["_id"])
    u.pop("password", None)
    v = u.pop("avatar_v", None)
    u["avatar_url"] = f"/api/auth/avatar/{u['_id']}?v={v}" if v else ""
    return u


# ── POST /api/auth/register
@router.post("/register")
async def register(body: UserRegister):
    # Check duplicate email
    existing = await users_collection.find_one({"email": body.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Hash password
    hashed = hash_password(body.password)

    user_doc = {
        "name":      body.name.strip(),
        "email":     body.email.lower().strip(),
        "password":  hashed,
        "phone":     body.phone or "",
        "role":      body.role,       # job_seeker | employer | service_provider
        "company":   body.company or "",
        "skills":    body.skills or "",
        "createdAt": datetime.utcnow(),
    }

    result = await users_collection.insert_one(user_doc)
    user_doc["_id"] = str(result.inserted_id)
    user_doc.pop("password")

    token = create_token(str(result.inserted_id))
    print(f"✅ Registered: {body.email} as {body.role}")

    return TokenOut(token=token, user=user_doc)


# ── POST /api/auth/login
@router.post("/login")
async def login(body: UserLogin):
    user = await users_collection.find_one({"email": body.email.lower().strip()})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(str(user["_id"]))
    user_out = user_to_dict(user)
    print(f"✅ Login: {body.email}")

    return TokenOut(token=token, user=user_out)


# ── GET /api/auth/me — get current user profile
@router.get("/me")
async def get_me(user_id: str = Depends(get_current_user)):
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user_to_dict(user)


# ── PUT /api/auth/profile — update profile
@router.put("/profile")
async def update_profile(body: dict, user_id: str = Depends(get_current_user)):
    # phone number is fixed after registration (it is the contact employers / customers trust)
    allowed = {"name", "skills", "company"}
    update  = {k: v for k, v in body.items() if k in allowed}
    if "name" in update:
        update["name"] = str(update["name"]).strip()[:80]
        if not update["name"]:
            raise HTTPException(status_code=400, detail="Name can't be empty")
    if not update:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": update}
    )
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    return user_to_dict(user)


# ── Profile picture
# POST   /api/auth/avatar          (Bearer token, multipart "file")
# DELETE /api/auth/avatar          (Bearer token)
# GET    /api/auth/avatar/{user_id}
@router.post("/avatar")
async def upload_avatar(file: UploadFile = File(...), user_id: str = Depends(get_current_user)):
    ctype = (file.content_type or "").lower()
    if ctype not in AVATAR_TYPES:
        raise HTTPException(status_code=400, detail="Please choose a JPG, PNG or WEBP photo")
    data = await file.read()
    if len(data) > AVATAR_MAX:
        raise HTTPException(status_code=400, detail="Photo is larger than 2 MB")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    now = datetime.utcnow()
    await avatars_collection.update_one(
        {"user_id": user_id},
        {"$set": {"image": Binary(data), "content_type": ctype, "size": len(data), "updatedAt": now}},
        upsert=True,
    )
    v = int(now.timestamp())
    await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": {"avatar_v": v}})
    return {"avatar_url": f"/api/auth/avatar/{user_id}?v={v}"}


@router.delete("/avatar")
async def delete_avatar(user_id: str = Depends(get_current_user)):
    await avatars_collection.delete_one({"user_id": user_id})
    await users_collection.update_one({"_id": ObjectId(user_id)}, {"$unset": {"avatar_v": ""}})
    return {"avatar_url": ""}


@router.get("/avatar/{uid}")
async def get_avatar(uid: str):
    try:
        ObjectId(uid)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid id")
    a = await avatars_collection.find_one({"user_id": uid})
    if not a:
        raise HTTPException(status_code=404, detail="No photo")
    return Response(content=bytes(a["image"]), media_type=a.get("content_type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})
