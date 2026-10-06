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


# Words people may type for each service (English + common Sri Lankan terms)
SERVICE_WORDS = {
    "mechanic": "mechanic car repair engine garage vehicle breakdown van jeep bike",
    "battery": "battery dead jump start car battery",
    "tyre": "tyre tire puncture flat wheel tube",
    "towing": "towing tow truck breakdown recovery accident",
    "fuel": "fuel petrol diesel empty tank",
    "lockout": "key lockout locked keys car lock",
    "auto_electric": "auto electrician car wiring lights starter alternator",
    "plumber": "plumber pipe leak water tap toilet drain bathroom",
    "electrician": "electrician wiring power light switch fuse electrical",
    "ac_repair": "ac air conditioner aircon repair service gas fridge refrigerator",
    "carpenter": "carpenter wood furniture door window cupboard",
    "painter": "painter painting wall paint house",
    "cleaning": "cleaning cleaner house office deep clean",
    "mason": "mason bass construction cement tiles building wall",
    "three_wheeler": "three wheeler tuk tuk taxi hire ride",
    "lorry": "lorry truck hire transport goods",
    "courier": "courier delivery parcel document send",
    "moving": "house moving shifting movers relocation",
    "phone_repair": "phone repair mobile screen display battery smartphone",
    "computer_repair": "computer laptop repair pc printer software",
    "cctv": "cctv camera security installation",
    "tutor": "tutor tuition teacher class lessons maths english",
    "beautician": "beautician salon makeup hair bridal",
    "tailor": "tailor sewing dress stitching clothes",
}

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
    """email → (lat, lng) of providers who are online right now (lat/lng may be None)."""
    since = datetime.utcnow() - timedelta(minutes=ONLINE_TTL_MIN)
    out = {}
    async for p in providers_collection.find({"email": {"$in": list(emails)}}):
        if p.get("online") and p.get("updatedAt", since) >= since:
            out[p["email"]] = (p.get("lat"), p.get("lng"))
    return out

def _public(l, online=False, distance=None, live=None):
    d = {k: v for k, v in l.items() if k not in ("_id",)}
    d["_id"] = str(l["_id"])
    if live:                                   # online → show where the provider is right now
        d["lat"], d["lng"] = live
    d["all_services"] = list(l.get("services", [])) + list(l.get("custom_services", []))
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
    # services typed by the provider that are not in the list (e.g. "Water tank cleaning")
    custom = []
    for c in (body.get("custom_services") or []):
        c = re.sub(r"\s+", " ", str(c)).strip()[:40]
        if c and c.lower() not in [x.lower() for x in custom] and len(custom) < 10:
            custom.append(c)
    name = (body.get("business_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Enter your business or display name")
    if not services and not custom:
        raise HTTPException(status_code=400, detail="Choose or type at least one service")
    try:
        lat, lng = float(body["lat"]), float(body["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Set your location")
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
        "services": services, "custom_services": custom,
        "groups": sorted({GROUP_OF[s] for s in services} | ({"other"} if custom else set())),
        "lat": lat, "lng": lng, "area": (body.get("area") or "")[:80],
        "radius_km": 25,
        "hours": hours,
        "price_from": max(0, int(float(body.get("price_from") or 0))),
        "phone": (body.get("phone") or user.get("phone") or "")[:20],
        "whatsapp": (body.get("whatsapp") or "")[:20],
        "published": True,
        "updatedAt": datetime.utcnow(),
    }
    await listings_collection.update_one({"email": email}, {"$set": doc, "$setOnInsert": {"createdAt": datetime.utcnow(),
                                          "rating": 0, "rating_count": 0, "jobs_done": 0}}, upsert=True)
    # keep the instant-request services in sync with the listing
    await providers_collection.update_one({"email": email}, {"$set": {"services": services, "name": name,
                                          "phone": doc["phone"]}}, upsert=False)
    return _public(await listings_collection.find_one({"email": email}))


@router.delete("/listings")
async def delete_listing(email: str):
    """Provider removes their listing → no longer shown in Find Help, and goes offline."""
    email = email.lower()
    res = await listings_collection.delete_one({"email": email})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="You don't have a listing")
    await providers_collection.update_one({"email": email}, {"$set": {"online": False, "services": []}})
    return {"ok": True}


# ─────────────────────────── customer: find help near me
@router.get("/services/suggest")
async def suggest_services(q: str = ""):
    """Services that providers typed themselves (not in the fixed list), matching what the customer types.
    This is how the list grows to cover any service in the world."""
    q = q.strip().lower()
    counts = {}
    async for l in listings_collection.find({"custom_services": {"$exists": True, "$ne": []}}, {"custom_services": 1}):
        for c in l.get("custom_services", []):
            if not q or q in c.lower():
                key = c.strip().lower()
                name, n = counts.get(key, (c.strip(), 0))
                counts[key] = (name, n + 1)
    items = sorted(counts.values(), key=lambda x: (-x[1], x[0]))[:8]
    return [{"name": n, "providers": c} for n, c in items]


@router.get("/listings/nearby")
async def nearby(lat: float, lng: float, service: str = "", group: str = "", q: str = "", radius: float = 25):
    query = {"published": {"$ne": False}}
    if service:   # list service id, or a provider-typed service (any capitalisation)
        query["$or"] = [{"services": service},
                        {"custom_services": {"$regex": f"^{re.escape(service.strip())}$", "$options": "i"}}]
    elif group:
        query["groups"] = group
    listings = [l async for l in listings_collection.find(query)]
    online = await _online_map([l["email"] for l in listings])
    out = []
    for l in listings:
        live = online.get(l["email"])
        pos = live if live and live[0] is not None else (l.get("lat"), l.get("lng"))
        if pos[0] is None:
            continue
        d = _km(lat, lng, pos[0], pos[1])
        if d > radius:
            continue
        if q:
            text = " ".join([l.get("business_name", ""), l.get("description", ""), l.get("area", ""),
                             " ".join(SERVICE_WORDS.get(x, x) for x in l.get("services", [])).replace("_", " "),
                             " ".join(l.get("custom_services", []))]).lower()
            if q.lower() not in text:
                continue
        out.append(_public(l, l["email"] in online, d, live if live and live[0] is not None else None))
    # 🟢 online first, then open now, then nearest
    out.sort(key=lambda x: (not x["online"], not x["open_now"], x["distance_km"]))
    return out

@router.get("/listings/{lid}")
async def listing(lid: str):
    l = await listings_collection.find_one({"_id": _oid(lid)})
    if not l:
        raise HTTPException(status_code=404, detail="Listing not found")
    online = await _online_map([l["email"]])
    live = online.get(l["email"])
    return _public(l, l["email"] in online, None, live if live and live[0] is not None else None)


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
    offered = list(l.get("services", [])) + list(l.get("custom_services", []))
    service = body.get("service") if body.get("service") in offered else offered[0]
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
        "lat": body.get("lat"), "lng": body.get("lng"),          # where the provider must go
        "payment_method": "cash" if body.get("payment_method") == "cash" else "card",
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
    if action == "start":
        return await _start_booking(b, email)
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


async def _start_booking(b, email):
    """Provider starts an accepted booking → it becomes a live request
    (tracking → arrival code → bill → card/cash), exactly like "Request now"."""
    if email != b["provider_email"]:
        raise HTTPException(status_code=403, detail="Not your booking")
    if b["status"] == "in_progress" and b.get("request_id"):
        return {"status": "in_progress", "request_id": b["request_id"]}
    if b["status"] != "accepted":
        raise HTTPException(status_code=400, detail="Accept the booking first")
    if b.get("lat") is None:
        raise HTTPException(status_code=400, detail="This booking has no customer location")
    requests_collection = database.get_collection("service_requests")
    busy = await requests_collection.find_one({"provider.email": email,
                                               "status": {"$in": ["accepted", "arrived", "in_progress", "payment_due", "cash_confirm_pending"]}})
    if busy:
        raise HTTPException(status_code=400, detail="Finish your current job first")
    p = await providers_collection.find_one({"email": email}) or {}
    now = datetime.utcnow()
    req = {
        "customer_email": b["customer_email"], "customer_name": b.get("customer_name", ""), "customer_phone": b.get("customer_phone", ""),
        "service_type": b["service"], "vehicle_type": "", "note": b.get("note", ""), "address": b.get("address", ""),
        "lat": float(b["lat"]), "lng": float(b["lng"]), "payment_method": b.get("payment_method", "card"),
        "status": "accepted", "target_provider": email, "booking_id": str(b["_id"]),
        "provider": {"email": email, "name": b.get("provider_name", ""), "phone": b.get("provider_phone", ""),
                     "lat": p.get("lat"), "lng": p.get("lng")},
        "createdAt": now, "acceptedAt": now,
    }
    res = await requests_collection.insert_one(req)
    await bookings_collection.update_one({"_id": b["_id"]}, {"$set": {"status": "in_progress", "request_id": str(res.inserted_id), "startedAt": now}})
    return {"status": "in_progress", "request_id": str(res.inserted_id)}
