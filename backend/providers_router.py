"""
JobEka — "Find Help Near Me" directory.

A service provider creates ONE permanent listing (business name, services, base
location, hours, price, contact). The listing is always visible to customers.
Going online/offline (provider_status, see service_router) only decides whether
they also get instant "Request now" jobs and the 🟢 badge.

Provider:  GET  /api/listings/me?email=          PUT /api/listings   (create / update)
Customer:  GET  /api/listings/nearby?lat&lng&service&group&q&radius
           GET  /api/listings/{id}
Bookings:  POST /api/bookings                      (customer books for a later date/time)
           GET  /api/bookings?email&role=customer|provider
           POST /api/bookings/{id}/{accept|decline|cancel|complete}
"""
import math, re
from datetime import datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException

from database import database

router = APIRouter(prefix="/api", tags=["Find help (directory & bookings)"])
listings_collection  = database.get_collection("provider_listings")
providers_collection = database.get_collection("provider_status")
bookings_collection  = database.get_collection("bookings")
users_collection     = database.get_collection("users")

# ── service catalogue (ids shared with the app)
CATALOG = {
    "vehicle":  ["mechanic", "battery", "tyre", "towing", "fuel", "lockout", "auto_electric"],
    "home":     ["plumber", "electrician", "ac_repair", "carpenter", "painter", "cleaning", "mason"],
    "delivery": ["three_wheeler", "lorry", "courier", "moving"],
    "tech":     ["phone_repair", "computer_repair", "cctv"],
    "personal": ["tutor", "beautician", "tailor"],
}
ALL_SERVICES = {s for v in CATALOG.values() for s in v}
GROUP_OF = {s: g for g, v in CATALOG.items() for s in v}
ONLINE_TTL_MIN = 10
SL_OFFSET = timedelta(hours=5, minutes=30)   # Sri Lanka time


def _oid(v):
    try:
        return ObjectId(v)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid id")

def _km(lat1, lng1, lat2, lng2):
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def _iso(d):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}

def _open_now(hours: dict) -> bool:
    if not hours:
        return False
    now = datetime.utcnow() + SL_OFFSET
    day = (now.weekday() + 1) % 7            # 0 = Sunday … 6 = Saturday
    if day not in (hours.get("days") or []):
        return False
    hm = now.strftime("%H:%M")
    o, c = hours.get("open", "08:00"), hours.get("close", "18:00")
    return o <= hm < c if o < c else (hm >= o or hm < c)   # supports overnight hours

async def _online_map(emails):
    since = datetime.utcnow() - timedelta(minutes=ONLINE_TTL_MIN)
    out = {}
    async for p in providers_collection.find({"email": {"$in": list(emails)}}):
        out[p["email"]] = bool(p.get("online")) and p.get("updatedAt", since) >= since
    return out

def _public(l, online=False, distance=None):
    d = {k: v for k, v in l.items() if k not in ("_id",)}
    d["_id"] = str(l["_id"])
    if not l.get("show_exact", True):          # privacy: area only (~1 km precision)
        d["lat"], d["lng"] = round(l["lat"], 2), round(l["lng"], 2)
    d["online"] = online
    d["open_now"] = _open_now(l.get("hours"))
    if distance is not None:
        d["distance_km"] = round(distance, 1)
    return _iso(d)


# ─────────────────────────── provider: my listing
@router.get("/listings/me")
async def my_listing(email: str):
    l = await listings_collection.find_one({"email": email.lower()})
    return _public(l) if l else {}

@router.put("/listings")
async def save_listing(body: dict):
    email = (body.get("email") or "").lower()
    user = await users_collection.find_one({"email": email})
    if not user or user.get("role") != "service_provider":
        raise HTTPException(status_code=403, detail="Only service providers can create a listing")
    services = [s for s in (body.get("services") or []) if s in ALL_SERVICES]
    name = (body.get("business_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Enter your business or display name")
    if not services:
        raise HTTPException(status_code=400, detail="Choose at least one service")
    try:
        lat, lng = float(body["lat"]), float(body["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Set your base location")
    hours = body.get("hours") or {}
    tm = re.compile(r"^\d{2}:\d{2}$")
    hours = {
        "days":  [int(d) for d in (hours.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6],
        "open":  hours.get("open") if tm.match(str(hours.get("open", ""))) else "08:00",
        "close": hours.get("close") if tm.match(str(hours.get("close", ""))) else "18:00",
    }
    doc = {
        "email": email, "business_name": name[:60],
        "description": (body.get("description") or "")[:400],
        "services": services, "groups": sorted({GROUP_OF[s] for s in services}),
        "lat": lat, "lng": lng, "area": (body.get("area") or "")[:80],
        "show_exact": bool(body.get("show_exact", True)),
        "radius_km": max(1, min(50, int(body.get("radius_km") or 10))),
        "hours": hours,
        "price_from": max(0, int(float(body.get("price_from") or 0))),
        "phone": (body.get("phone") or user.get("phone") or "")[:20],
        "whatsapp": (body.get("whatsapp") or "")[:20],
        "published": bool(body.get("published", True)),
        "updatedAt": datetime.utcnow(),
    }
    await listings_collection.update_one({"email": email}, {"$set": doc, "$setOnInsert": {"createdAt": datetime.utcnow(),
                                          "rating": 0, "rating_count": 0, "jobs_done": 0}}, upsert=True)
    # keep the instant-request services in sync with the listing
    await providers_collection.update_one({"email": email}, {"$set": {"services": services, "name": name,
                                          "phone": doc["phone"]}}, upsert=False)
    return _public(await listings_collection.find_one({"email": email}))


# ─────────────────────────── customer: find help near me
@router.get("/listings/nearby")
async def nearby(lat: float, lng: float, service: str = "", group: str = "", q: str = "", radius: float = 25):
    query = {"published": True}
    if service:
        query["services"] = service
    elif group:
        query["groups"] = group
    rows = []
    async for l in listings_collection.find(query):
        d = _km(lat, lng, l["lat"], l["lng"])
        if d > radius:
            continue
        if q:
            text = f"{l.get('business_name','')} {l.get('description','')} {' '.join(l.get('services', []))} {l.get('area','')}".lower()
            if q.lower() not in text:
                continue
        rows.append((d, l))
    online = await _online_map([l["email"] for _, l in rows])
    out = [_public(l, online.get(l["email"], False), d) for d, l in rows]
    # 🟢 online first, then open now, then nearest
    out.sort(key=lambda x: (not x["online"], not x["open_now"], x["distance_km"]))
    return out

@router.get("/listings/{lid}")
async def listing(lid: str):
    l = await listings_collection.find_one({"_id": _oid(lid)})
    if not l:
        raise HTTPException(status_code=404, detail="Listing not found")
    online = await _online_map([l["email"]])
    return _public(l, online.get(l["email"], False))


# ─────────────────────────── bookings (for later)
@router.post("/bookings")
async def create_booking(body: dict):
    email = (body.get("customer_email") or "").lower()
    user = await users_collection.find_one({"email": email})
    if not user or user.get("role") == "service_provider":
        raise HTTPException(status_code=403, detail="Please log in with a personal or business account")
    l = await listings_collection.find_one({"_id": _oid(body.get("listing_id"))})
    if not l:
        raise HTTPException(status_code=404, detail="Provider not found")
    service = body.get("service") if body.get("service") in l["services"] else l["services"][0]
    try:
        when = datetime.fromisoformat(str(body.get("when")).replace("Z", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="Choose a date and time")
    doc = {
        "listing_id": str(l["_id"]), "provider_email": l["email"], "provider_name": l["business_name"],
        "provider_phone": l.get("phone", ""),
        "customer_email": email, "customer_name": user.get("name", ""), "customer_phone": user.get("phone", ""),
        "service": service, "when": when, "note": (body.get("note") or "")[:300],
        "address": (body.get("address") or "")[:200],
        "status": "pending", "createdAt": datetime.utcnow(),
    }
    res = await bookings_collection.insert_one(doc)
    doc["_id"] = str(res.inserted_id)
    return _iso(doc)

@router.get("/bookings")
async def list_bookings(email: str, role: str = "customer"):
    key = "provider_email" if role == "provider" else "customer_email"
    out = []
    async for b in bookings_collection.find({key: email.lower()}).sort("when", -1).limit(50):
        b["_id"] = str(b["_id"])
        out.append(_iso(b))
    return out

@router.post("/bookings/{bid}/{action}")
async def booking_action(bid: str, action: str, body: dict):
    b = await bookings_collection.find_one({"_id": _oid(bid)})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    email = (body.get("email") or "").lower()
    rules = {
        "accept":   ("provider", ["pending"], "accepted"),
        "decline":  ("provider", ["pending"], "declined"),
        "complete": ("provider", ["accepted"], "completed"),
        "cancel":   ("customer", ["pending", "accepted"], "cancelled"),
    }
    if action not in rules:
        raise HTTPException(status_code=400, detail="Unknown action")
    who, allowed, new = rules[action]
    owner = b["provider_email"] if who == "provider" else b["customer_email"]
    if email != owner:
        raise HTTPException(status_code=403, detail="Not your booking")
    if b["status"] not in allowed:
        raise HTTPException(status_code=400, detail=f"This booking is already {b['status']}")
    await bookings_collection.update_one({"_id": b["_id"]}, {"$set": {"status": new, f"{new}At": datetime.utcnow()}})
    if new == "completed":
        await listings_collection.update_one({"email": b["provider_email"]}, {"$inc": {"jobs_done": 1}})
    return {"status": new}
