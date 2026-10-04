"""
JobEka — on-demand services (roadside help: breakdown, tyre, battery, fuel, towing, lockout).

Customer                                  Service provider (mechanic)
────────                                  ───────────────────────────
POST /api/service-requests      (search)  POST /api/providers/status   (online + location + services)
GET  /api/service-requests/{id} (poll)    GET  /api/service-requests/nearby
                                          POST /{id}/accept
                                          POST /{id}/location          (live position for the customer)
                                          POST /{id}/arrive            → OTP shown to the provider
POST /{id}/verify-otp  (customer types it)
                                          POST /{id}/bill              (amount after the work)
POST /{id}/pay  {method: card|cash}
   card → PayHere order (payments_router)
   cash → customer slides "paid"          POST /{id}/cash-received     (provider slides "received")
POST /{id}/cancel  (either side)
"""
import math, hashlib, secrets
from datetime import datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException

from database import database
from providers_router import ALL_SERVICES

router = APIRouter(prefix="/api", tags=["On-demand services"])
requests_collection     = database.get_collection("service_requests")
providers_collection    = database.get_collection("provider_status")
users_collection        = database.get_collection("users")
payments_collection     = database.get_collection("payments")
transactions_collection = database.get_collection("transactions")
listings_collection     = database.get_collection("provider_listings")   # Find Help listings (providers_router)

SERVICES        = ALL_SERVICES      # same catalogue as the Find Help directory
SEARCH_RADIUS   = 15      # km
REQUEST_TTL_MIN = 20      # a "searching" request expires after this
ONLINE_TTL_MIN  = 10      # provider counts as online if seen within this
OTP_MINUTES     = 15
LK_OFFSET       = timedelta(hours=5, minutes=30)


# ─────────────────────────────── helpers
def _oid(v):
    try:
        return ObjectId(v)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid id")

def _km(lat1, lng1, lat2, lng2):
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def _hash(rid, otp):
    return hashlib.sha256(f"svc:{rid}:{otp}".encode()).hexdigest()

def _out(doc, extra=None):
    d = {k: v for k, v in doc.items() if k not in ("otp_hash",)}
    d["_id"] = str(d["_id"])
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    if extra:
        d.update(extra)
    return d

async def _get(rid):
    doc = await requests_collection.find_one({"_id": _oid(rid)})
    if not doc:
        raise HTTPException(status_code=404, detail="Request not found")
    return doc

def _is_customer(doc, email):
    return (email or "").lower() == doc.get("customer_email")

def _is_provider(doc, email):
    return (email or "").lower() == (doc.get("provider") or {}).get("email")


# ─────────────────────────────── provider presence
@router.post("/providers/status")
async def provider_status(body: dict):
    email = (body.get("email") or "").lower()
    user = await users_collection.find_one({"email": email})
    if not user or user.get("role") != "service_provider":
        raise HTTPException(status_code=403, detail="Only service providers can go online")
    services = [s for s in (body.get("services") or []) if s in SERVICES]
    update = {
        "email": email, "name": user.get("name", ""), "phone": user.get("phone", ""),
        "online": bool(body.get("online")), "services": services, "updatedAt": datetime.utcnow(),
    }
    if body.get("lat") is not None and body.get("lng") is not None:
        update["lat"], update["lng"] = float(body["lat"]), float(body["lng"])
    await providers_collection.update_one({"email": email}, {"$set": update}, upsert=True)
    return {"ok": True, "online": update["online"], "services": services}

@router.get("/providers/me")
async def provider_me(email: str):
    p = await providers_collection.find_one({"email": email.lower()})
    if not p:
        return {"online": False, "services": []}
    return {"online": p.get("online", False), "services": p.get("services", [])}


# ─────────────────────────────── customer creates a request
@router.post("/service-requests")
async def create_request(body: dict):
    email = (body.get("customer_email") or "").lower()
    user = await users_collection.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=403, detail="Please log in again")
    if user.get("role") == "service_provider":
        raise HTTPException(status_code=403, detail="Service providers cannot request help")
    service = body.get("service_type")
    if service not in SERVICES:
        raise HTTPException(status_code=400, detail="Choose what kind of help you need")
    try:
        lat, lng = float(body["lat"]), float(body["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Your location is required")
    # one open request at a time
    # ── "Request now" from a listing → sent only to that provider
    target = (body.get("target_provider") or "").lower() or None
    if target:
        prov = await users_collection.find_one({"email": target, "role": "service_provider"})
        if not prov:
            raise HTTPException(status_code=404, detail="Provider not found")

    # one open request at a time
    open_req = await requests_collection.find_one({"customer_email": email,
                                                   "status": {"$in": ["searching", "accepted", "arrived", "in_progress", "payment_due", "cash_confirm_pending"]}})
    if open_req:
        return _out(open_req, {"existing": True})

    doc = {
        "customer_email": email, "customer_name": user.get("name", ""), "customer_phone": user.get("phone", ""),
        "service_type": service, "vehicle_type": body.get("vehicle_type") or "Car",
        "note": (body.get("note") or "")[:300], "address": (body.get("address") or "")[:200],
        "lat": lat, "lng": lng, "payment_method": "cash" if body.get("payment_method") == "cash" else "card",
        "status": "searching", "createdAt": datetime.utcnow(),
        # set when the customer taps "Request now" on one provider's listing
        "target_provider": target,
    }
    res = await requests_collection.insert_one(doc)
    doc["_id"] = res.inserted_id
    # how many providers can see it right now (shown to the customer)
    nearby = [1] if target else await _nearby_providers(service, lat, lng)
    print(f"🆘 Service request {res.inserted_id} ({service}) — {'to ' + target if target else str(len(nearby)) + ' provider(s) nearby'}")
    return _out(doc, {"providers_nearby": len(nearby)})

async def _nearby_providers(service, lat, lng):
    since = datetime.utcnow() - timedelta(minutes=ONLINE_TTL_MIN)
    out = []
    async for p in providers_collection.find({"online": True, "services": service, "updatedAt": {"$gte": since}}):
        if p.get("lat") is None:
            continue
        d = _km(lat, lng, p["lat"], p["lng"])
        if d <= SEARCH_RADIUS:
            out.append((d, p))
    return sorted(out, key=lambda x: x[0])

@router.get("/service-requests/active")
async def active_request(email: str, role: str = "customer"):
    """The user's current open request (to resume after closing the app)."""
    q = {"status": {"$in": ["searching", "accepted", "arrived", "in_progress", "payment_due", "cash_confirm_pending"]}}
    q["provider.email" if role == "provider" else "customer_email"] = email.lower()
    doc = await requests_collection.find_one(q, sort=[("createdAt", -1)])
    return _out(doc) if doc else {}

@router.get("/service-requests/nearby")
async def nearby_requests(email: str, lat: float, lng: float):
    email = email.lower()
    p = await providers_collection.find_one({"email": email})
    services = (p or {}).get("services") or []
    since = datetime.utcnow() - timedelta(minutes=REQUEST_TTL_MIN)
    out = []
    async for r in requests_collection.find({"status": "searching", "createdAt": {"$gte": since},
                                             "$or": [{"target_provider": None, "service_type": {"$in": services}},
                                                     {"target_provider": email}]}):
        d = _km(lat, lng, r["lat"], r["lng"])
        direct = r.get("target_provider") == email
        if direct or d <= SEARCH_RADIUS:      # requests sent to me directly are always shown
            out.append(_out(r, {"distance_km": round(d, 1), "direct": direct}))
    return sorted(out, key=lambda x: (not x["direct"], x["distance_km"]))

@router.get("/service-requests/{rid}")
async def get_request(rid: str):
    doc = await _get(rid)
    extra = {}
    if doc.get("status") == "searching":
        extra["providers_nearby"] = 1 if doc.get("target_provider") else len(await _nearby_providers(doc["service_type"], doc["lat"], doc["lng"]))
        if doc["createdAt"] < datetime.utcnow() - timedelta(minutes=REQUEST_TTL_MIN):
            await requests_collection.update_one({"_id": doc["_id"], "status": "searching"},
                                                 {"$set": {"status": "expired"}})
            doc["status"] = "expired"
    return _out(doc, extra)


# ─────────────────────────────── provider accepts / moves / arrives
@router.post("/service-requests/{rid}/accept")
async def accept(rid: str, body: dict):
    email = (body.get("provider_email") or "").lower()
    p = await providers_collection.find_one({"email": email})
    if not p:
        raise HTTPException(status_code=403, detail="Go online first")
    provider = {"email": email, "name": p.get("name", ""), "phone": p.get("phone", ""),
                "lat": body.get("lat", p.get("lat")), "lng": body.get("lng", p.get("lng"))}
    busy = await requests_collection.find_one({"provider.email": email,
                                               "status": {"$in": ["accepted", "arrived", "in_progress", "payment_due", "cash_confirm_pending"]}})
    if busy:
        raise HTTPException(status_code=400, detail="Finish your current job first")
    # atomic: only the first provider gets it (and only the chosen one for a direct request)
    res = await requests_collection.update_one(
        {"_id": _oid(rid), "status": "searching", "target_provider": {"$in": [None, email]}},
        {"$set": {"status": "accepted", "provider": provider, "acceptedAt": datetime.utcnow()}},
    )
    if res.modified_count == 0:
        cur = await _get(rid)
        if cur.get("target_provider") and cur["target_provider"] != email:
            raise HTTPException(status_code=403, detail="This request was sent to another provider")
        raise HTTPException(status_code=409, detail="Another provider already accepted this request")
    return _out(await _get(rid))


@router.post("/service-requests/{rid}/decline")
async def decline(rid: str, body: dict):
    doc = await _get(rid)
    email = (body.get("provider_email") or "").lower()
    if doc.get("target_provider") != email:
        raise HTTPException(status_code=403, detail="Not your request")
    if doc["status"] != "searching":
        raise HTTPException(status_code=400, detail="This request can no longer be declined")
    await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {"status": "declined"}})
    return {"status": "declined"}

@router.post("/service-requests/{rid}/location")
async def provider_location(rid: str, body: dict):
    doc = await _get(rid)
    if not _is_provider(doc, body.get("provider_email")):
        raise HTTPException(status_code=403, detail="Not your request")
    await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {
        "provider.lat": float(body["lat"]), "provider.lng": float(body["lng"]), "provider.updatedAt": datetime.utcnow()}})
    return {"ok": True}

@router.post("/service-requests/{rid}/arrive")
async def arrive(rid: str, body: dict):
    doc = await _get(rid)
    if not _is_provider(doc, body.get("provider_email")):
        raise HTTPException(status_code=403, detail="Not your request")
    if doc["status"] not in ("accepted", "arrived"):
        raise HTTPException(status_code=400, detail="This request is not waiting for arrival")
    otp = f"{secrets.randbelow(10000):04d}"
    await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {
        "status": "arrived", "otp_hash": _hash(rid, otp), "otp_attempts": 0,
        "otp_expires": datetime.utcnow() + timedelta(minutes=OTP_MINUTES), "arrivedAt": datetime.utcnow()}})
    return {"otp": otp, "expires_in_minutes": OTP_MINUTES}

@router.post("/service-requests/{rid}/verify-otp")
async def verify_otp(rid: str, body: dict):
    doc = await _get(rid)
    if not _is_customer(doc, body.get("customer_email")):
        raise HTTPException(status_code=403, detail="Not your request")
    if doc["status"] != "arrived":
        raise HTTPException(status_code=400, detail="The provider has not marked arrival yet")
    if datetime.utcnow() > doc.get("otp_expires", datetime.utcnow()):
        raise HTTPException(status_code=400, detail="Code expired. Ask the provider for a new one.")
    if doc.get("otp_attempts", 0) >= 5:
        raise HTTPException(status_code=429, detail="Too many wrong attempts. Ask the provider for a new code.")
    if _hash(rid, str(body.get("otp", "")).strip()) != doc.get("otp_hash"):
        await requests_collection.update_one({"_id": doc["_id"]}, {"$inc": {"otp_attempts": 1}})
        raise HTTPException(status_code=400, detail=f"Wrong code. {4 - doc.get('otp_attempts', 0)} attempt(s) left.")
    await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {"status": "in_progress", "startedAt": datetime.utcnow()},
                                                               "$unset": {"otp_hash": ""}})
    return {"status": "in_progress"}


# ─────────────────────────────── bill & payment
@router.post("/service-requests/{rid}/bill")
async def bill(rid: str, body: dict):
    doc = await _get(rid)
    if not _is_provider(doc, body.get("provider_email")):
        raise HTTPException(status_code=403, detail="Not your request")
    if doc["status"] != "in_progress":
        raise HTTPException(status_code=400, detail="The work has not started yet")
    try:
        amount = round(float(body.get("amount")), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Enter a valid amount")
    if amount <= 0 or amount > 500000:
        raise HTTPException(status_code=400, detail="Enter a valid amount")
    await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {
        "status": "payment_due", "amount": amount, "work_note": (body.get("note") or "")[:300], "billedAt": datetime.utcnow()}})
    return {"status": "payment_due", "amount": amount}

@router.post("/service-requests/{rid}/pay")
async def pay(rid: str, body: dict):
    doc = await _get(rid)
    if not _is_customer(doc, body.get("customer_email")):
        raise HTTPException(status_code=403, detail="Not your request")
    if doc["status"] != "payment_due":
        raise HTTPException(status_code=400, detail="Nothing to pay right now")
    method = "cash" if body.get("method") == "cash" else "card"
    if method == "cash":
        await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {
            "status": "cash_confirm_pending", "paid_method": "cash", "cash_paid_at": datetime.utcnow()}})
        return {"method": "cash", "status": "cash_confirm_pending", "amount": doc["amount"]}
    # card → a PayHere order handled by payments_router (kind = "service")
    order = await payments_collection.find_one({"job_id": rid, "kind": "service", "status": {"$ne": "paid"}})
    if not order:
        order = {
            "order_id": f"JS{datetime.utcnow().strftime('%y%m%d%H%M%S')}{secrets.randbelow(1000):03d}",
            "kind": "service", "job_id": rid, "job_title": f"Roadside help – {doc['service_type']}",
            "amount": doc["amount"],
            "employer_email": doc["customer_email"], "employer_name": doc.get("customer_name", ""),
            "employer_phone": doc.get("customer_phone", ""),
            "worker_email": doc["provider"]["email"], "worker_name": doc["provider"].get("name", ""),
            "status": "pending", "createdAt": datetime.utcnow(),
        }
        await payments_collection.insert_one(order)
    return {"method": "card", "order_id": order["order_id"], "amount": order["amount"], "title": order["job_title"]}

@router.post("/service-requests/{rid}/cash-received")
async def cash_received(rid: str, body: dict):
    doc = await _get(rid)
    if not _is_provider(doc, body.get("provider_email")):
        raise HTTPException(status_code=403, detail="Not your request")
    if doc["status"] == "completed":
        return {"status": "completed"}
    if doc["status"] != "cash_confirm_pending":
        raise HTTPException(status_code=400, detail="The customer has not paid in cash yet")
    now = datetime.utcnow()
    await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {"status": "completed", "paid_at": now, "completedAt": now}})
    await listings_collection.update_one({"email": doc["provider"]["email"]}, {"$inc": {"jobs_done": 1}})
    await transactions_collection.insert_many([
        {"email": doc["provider"]["email"], "type": "cash", "amount": doc["amount"], "service_request_id": rid,
         "description": f"Cash received – roadside {doc['service_type']}", "createdAt": now},
        {"email": doc["customer_email"], "type": "cash_payment", "amount": doc["amount"], "service_request_id": rid,
         "description": f"Cash paid – roadside {doc['service_type']}", "createdAt": now},
    ])
    return {"status": "completed"}


# ─────────────────────────────── cancel
@router.post("/service-requests/{rid}/cancel")
async def cancel(rid: str, body: dict):
    doc = await _get(rid)
    email = body.get("email")
    if _is_customer(doc, email):
        if doc["status"] not in ("searching", "pending", "accepted", "arrived"):
            raise HTTPException(status_code=400, detail="The work has already started")
        await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {"status": "cancelled", "cancelledBy": "customer"}})
        return {"status": "cancelled"}
    if _is_provider(doc, email):
        if doc["status"] not in ("accepted", "arrived"):
            raise HTTPException(status_code=400, detail="The work has already started")
        # give it back to other providers
        await requests_collection.update_one({"_id": doc["_id"]}, {"$set": {"status": "searching", "createdAt": datetime.utcnow(),
                                                                            "target_provider": None},
                                                                   "$unset": {"provider": "", "otp_hash": ""}})
        return {"status": "searching"}
    raise HTTPException(status_code=403, detail="Not your request")


# ─────────────────────────────── history
@router.get("/service-requests")
async def history(email: str, role: str = "customer"):
    key = "provider.email" if role == "provider" else "customer_email"
    cur = requests_collection.find({key: email.lower()}).sort("createdAt", -1).limit(30)
    return [_out(d) async for d in cur]
