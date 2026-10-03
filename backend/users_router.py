from fastapi import APIRouter, HTTPException, Depends
from bson import ObjectId
from datetime import datetime

from database import database
from user_schema import UserRegister, UserLogin, TokenOut
from auth import hash_password, verify_password, create_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["Auth"])

users_collection = database.get_collection("users")


def user_to_dict(user: dict) -> dict:
    """Convert MongoDB user doc to JSON-safe dict (remove password)."""
    u = dict(user)
    u["_id"] = str(u["_id"])
    u.pop("password", None)
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
    allowed = {"name", "phone", "skills", "company"}
    update  = {k: v for k, v in body.items() if k in allowed}
    if not update:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": update}
    )
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    return user_to_dict(user)
