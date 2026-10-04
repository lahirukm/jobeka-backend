"""
JobEka — arrival OTP, PayHere card payment and worker wallet.

Flow
  1. Worker taps "I've Arrived"          → POST /api/jobs/{id}/arrive        → 4-digit OTP shown to worker
  2. Worker tells the OTP to the employer
  3. Employer types the OTP               → POST /api/jobs/{id}/verify-otp    → payment order created
  4. Employer pays by card (PayHere)      → GET  /api/payments/checkout/{order_id}  (opened in a WebView)
  5. Payment confirmed                    → notify_url (when deployed) or /confirm (Retrieval API)
  6. Job amount added to worker's wallet  → GET  /api/wallet?email=
  7. Worker requests a bank withdrawal    → POST /api/wallet/withdraw  → admin approves / rejects
"""
import os, re, base64, hashlib, secrets
from datetime import datetime, timedelta

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

from database import database, jobs_collection

load_dotenv()
router = APIRouter(tags=["Payments & Wallet"])

users_collection        = database.get_collection("users")
payments_collection     = database.get_collection("payments")
transactions_collection = database.get_collection("transactions")
withdrawals_collection  = database.get_collection("withdrawals")

# ── PayHere settings (.env)
PAYHERE_MERCHANT_ID     = os.getenv("PAYHERE_MERCHANT_ID", "")
PAYHERE_MERCHANT_SECRET = os.getenv("PAYHERE_MERCHANT_SECRET", "")
PAYHERE_APP_ID          = os.getenv("PAYHERE_APP_ID", "")       # Business App (for Retrieval API)
PAYHERE_APP_SECRET      = os.getenv("PAYHERE_APP_SECRET", "")
PAYHERE_SANDBOX         = os.getenv("PAYHERE_SANDBOX", "true").lower() == "true"
# Public address of this backend (your laptop IP while testing, Render URL after deployment)
PUBLIC_BASE_URL         = os.getenv("PUBLIC_BASE_URL", "http://localhost:8001").rstrip("/")
# Sandbox only: if the Retrieval API is not set up, trust PayHere's redirect to return_url.
PAYHERE_TRUST_RETURN    = os.getenv("PAYHERE_TRUST_RETURN", "false").lower() == "true"

PAYHERE_HOST  = "https://sandbox.payhere.lk" if PAYHERE_SANDBOX else "https://www.payhere.lk"
OTP_MINUTES   = 10
OTP_MAX_TRIES = 5
MIN_WITHDRAW  = 500


# ─────────────────────────────── helpers
def _oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid id")

def _hash_otp(job_id: str, otp: str) -> str:
    return hashlib.sha256(f"{job_id}:{otp}".encode()).hexdigest()

def _amount_from_salary(salary) -> float:
    """'LKR 3,000/day' → 3000.0"""
    # take the FIRST number only, so "LKR 1,500 - 2,000" → 1500
    m = re.search(r"\d[\d,]*(\.\d+)?", str(salary or ""))
    try:
        value = float(m.group(0).replace(",", "")) if m else 0
    except ValueError:
        value = 0
    if value <= 0:
        raise HTTPException(status_code=400, detail="Job salary is not a valid amount. Edit the job and enter a number.")
    return round(value, 2)

def _md5_upper(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest().upper()

def _checkout_hash(order_id: str, amount: float) -> str:
    # PayHere: UPPER(MD5(merchant_id + order_id + amount + currency + UPPER(MD5(merchant_secret))))
    return _md5_upper(f"{PAYHERE_MERCHANT_ID}{order_id}{amount:.2f}LKR{_md5_upper(PAYHERE_MERCHANT_SECRET)}")

def _clean(doc: dict) -> dict:
    d = dict(doc)
    d["_id"] = str(d["_id"])
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        if isinstance(v, ObjectId):
            d[k] = str(v)
    return d

async def _complete_payment(order: dict, payhere_payment_id: str = "", method: str = ""):
    """Mark the order paid and credit the worker's wallet (runs only once per order)."""
    res = await payments_collection.update_one(
        {"_id": order["_id"], "status": {"$ne": "paid"}},
        {"$set": {"status": "paid", "paid_at": datetime.utcnow(),
                  "payhere_payment_id": payhere_payment_id, "method": method}},
    )
    if res.modified_count == 0:
        return  # already processed
    amount = order["amount"]
    await users_collection.update_one({"email": order["worker_email"]}, {"$inc": {"wallet_balance": amount}})
    now = datetime.utcnow()
    await transactions_collection.insert_many([
        {"email": order["worker_email"],   "type": "credit", "amount": amount, "job_id": order["job_id"],
         "description": f"Payment for job: {order['job_title']}", "order_id": order["order_id"], "createdAt": now},
        {"email": order["employer_email"], "type": "card_payment", "amount": amount, "job_id": order["job_id"],
         "description": f"Card payment for job: {order['job_title']}", "order_id": order["order_id"], "createdAt": now},
    ])
    if order.get("kind") == "service":
        # on-demand service request (roadside help) → mark it completed
        await database.get_collection("service_requests").update_one(
            {"_id": ObjectId(order["job_id"])},
            {"$set": {"status": "completed", "paid_method": "card", "paid_at": now, "completedAt": now}},
        )
        await database.get_collection("provider_listings").update_one({"email": order["worker_email"]}, {"$inc": {"jobs_done": 1}})
        sr = await database.get_collection("service_requests").find_one({"_id": ObjectId(order["job_id"])})
        if sr and sr.get("booking_id"):
            await database.get_collection("bookings").update_one({"_id": ObjectId(sr["booking_id"])}, {"$set": {"status": "completed", "completedAt": now}})
    else:
        await jobs_collection.update_one(
            {"_id": ObjectId(order["job_id"])},
            {"$set": {"payment_status": "paid", "paid_amount": amount, "paid_at": now}},
        )


# ─────────────────────────────── 1. worker arrives → OTP
@router.post("/api/jobs/{job_id}/arrive")
async def arrive(job_id: str, body: dict):
    job = await jobs_collection.find_one({"_id": _oid(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "applied":
        raise HTTPException(status_code=400, detail="This job is not in progress")
    worker_email = (body.get("email") or "").lower()
    applied_by = (job.get("appliedBy") or {}).get("email", "")
    if applied_by and worker_email and applied_by != worker_email:
        raise HTTPException(status_code=403, detail="Only the person who applied can mark arrival")
    if job.get("payment_status") == "paid":
        raise HTTPException(status_code=400, detail="This job has already been paid")
    if (job.get("arrival") or {}).get("verified"):
        raise HTTPException(status_code=400, detail="The employer has already verified your code")

    otp = f"{secrets.randbelow(10000):04d}"
    print(f"🔢 New arrival OTP for job {job_id} (valid {OTP_MINUTES} min)")
    await jobs_collection.update_one({"_id": job["_id"]}, {"$set": {
        "arrival": {
            "otp_hash":   _hash_otp(job_id, otp),
            "expires_at": datetime.utcnow() + timedelta(minutes=OTP_MINUTES),
            "attempts":   0,
            "verified":   False,
            "arrived_at": datetime.utcnow(),
        },
        "payment_status": "awaiting_otp",
    }})
    return {"otp": otp, "expires_in_minutes": OTP_MINUTES}


# ─────────────────────────────── 2. employer enters OTP → payment order
@router.post("/api/jobs/{job_id}/verify-otp")
async def verify_otp(job_id: str, body: dict):
    job = await jobs_collection.find_one({"_id": _oid(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    employer_email = (body.get("employer_email") or "").lower()
    if job.get("employer_email") and employer_email != job["employer_email"].lower():
        raise HTTPException(status_code=403, detail="Only the employer who posted this job can verify the OTP")

    arrival = job.get("arrival") or {}
    if not arrival:
        raise HTTPException(status_code=400, detail="The worker has not marked arrival yet")

    if not arrival.get("verified"):
        now = datetime.utcnow()
        if arrival.get("expires_at") and now > arrival["expires_at"]:
            print(f"⏰ OTP expired for job {job_id}: now(UTC)={now:%H:%M:%S} expired_at(UTC)={arrival['expires_at']:%H:%M:%S}")
            raise HTTPException(status_code=400, detail="OTP expired. Ask the worker for the new code on their screen.")
        if arrival.get("attempts", 0) >= OTP_MAX_TRIES:
            raise HTTPException(status_code=429, detail="Too many wrong attempts. Ask the worker for a new OTP.")
        if _hash_otp(job_id, str(body.get("otp", "")).strip()) != arrival.get("otp_hash"):
            await jobs_collection.update_one({"_id": job["_id"]}, {"$inc": {"arrival.attempts": 1}})
            print(f"❌ Wrong OTP for job {job_id}")
            left = OTP_MAX_TRIES - arrival.get("attempts", 0) - 1
            raise HTTPException(status_code=400, detail=f"Wrong OTP. {left} attempt(s) left.")
        await jobs_collection.update_one({"_id": job["_id"]}, {"$set": {
            "arrival.verified": True, "payment_status": "awaiting_payment"}})

    # Cash job → no card payment. The employer now confirms handing over the cash.
    if (job.get("payment_method") or "card") == "cash":
        if job.get("payment_status") not in ("cash_confirm_pending", "paid"):
            await jobs_collection.update_one({"_id": job["_id"]}, {"$set": {"payment_status": "awaiting_cash"}})
        return {"method": "cash", "amount": _amount_from_salary(job.get("salary"))}

    # Reuse an unpaid order for this job if one exists (e.g. employer cancelled the card page)
    order = await payments_collection.find_one({"job_id": job_id, "status": {"$ne": "paid"}})
    if not order:
        worker = job.get("appliedBy") or {}
        order_id = f"JE{datetime.utcnow().strftime('%y%m%d%H%M%S')}{secrets.randbelow(1000):03d}"
        order = {
            "order_id": order_id, "job_id": job_id, "job_title": job.get("title", "Job"),
            "amount": _amount_from_salary(job.get("salary")),
            "employer_email": job.get("employer_email", employer_email),
            "employer_name": job.get("employer_name", ""), "employer_phone": job.get("employer_phone", ""),
            "worker_email": worker.get("email", ""), "worker_name": worker.get("name", ""),
            "status": "pending", "createdAt": datetime.utcnow(),
        }
        await payments_collection.insert_one(order)
    return {
        "method": "card",
        "order_id": order["order_id"], "amount": order["amount"],
        "checkout_url": f"{PUBLIC_BASE_URL}/api/payments/checkout/{order['order_id']}",
        "return_url":   f"{PUBLIC_BASE_URL}/api/payments/return",
        "cancel_url":   f"{PUBLIC_BASE_URL}/api/payments/cancel",
    }


# ─────────────────────────────── 2b. CASH: employer confirms cash handed over
@router.post("/api/jobs/{job_id}/cash-paid")
async def cash_paid(job_id: str, body: dict):
    job = await jobs_collection.find_one({"_id": _oid(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if (job.get("payment_method") or "card") != "cash":
        raise HTTPException(status_code=400, detail="This job is paid by card")
    employer_email = (body.get("employer_email") or "").lower()
    if job.get("employer_email") and employer_email != job["employer_email"].lower():
        raise HTTPException(status_code=403, detail="Only the employer who posted this job can confirm cash")
    if not (job.get("arrival") or {}).get("verified"):
        raise HTTPException(status_code=400, detail="Verify the worker's OTP first")
    if job.get("payment_status") == "paid":
        return {"status": "paid"}
    amount = _amount_from_salary(job.get("salary"))
    await jobs_collection.update_one({"_id": job["_id"]}, {"$set": {
        "payment_status": "cash_confirm_pending", "cash_amount": amount, "cash_paid_at": datetime.utcnow()}})
    print(f"💵 Employer confirmed cash LKR {amount} for job {job_id}")
    return {"status": "cash_confirm_pending", "amount": amount}


# ─────────────────────────────── 2c. CASH: worker confirms cash received
@router.post("/api/jobs/{job_id}/cash-received")
async def cash_received(job_id: str, body: dict):
    job = await jobs_collection.find_one({"_id": _oid(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    worker_email = (body.get("email") or "").lower()
    applied_by = (job.get("appliedBy") or {}).get("email", "")
    if applied_by and worker_email and applied_by != worker_email:
        raise HTTPException(status_code=403, detail="Only the worker on this job can confirm")
    if job.get("payment_status") == "paid":
        return {"status": "paid", "amount": job.get("paid_amount")}
    if job.get("payment_status") != "cash_confirm_pending":
        raise HTTPException(status_code=400, detail="The employer has not confirmed the cash payment yet")
    amount = job.get("cash_amount") or _amount_from_salary(job.get("salary"))
    now = datetime.utcnow()
    await jobs_collection.update_one({"_id": job["_id"]}, {"$set": {
        "payment_status": "paid", "paid_amount": amount, "paid_at": now, "paid_method": "cash"}})
    # Cash is already in the worker's hand → recorded in history, NOT added to the wallet balance
    await transactions_collection.insert_many([
        {"email": applied_by or worker_email, "type": "cash", "amount": amount, "job_id": job_id,
         "description": f"Cash received for job: {job.get('title', 'Job')}", "createdAt": now},
        {"email": job.get("employer_email", ""), "type": "cash_payment", "amount": amount, "job_id": job_id,
         "description": f"Cash paid for job: {job.get('title', 'Job')}", "createdAt": now},
    ])
    print(f"✅ Worker confirmed cash LKR {amount} for job {job_id}")
    return {"status": "paid", "amount": amount}


# ─────────────────────────────── 3. PayHere checkout page (opened inside the app's WebView)
@router.get("/api/payments/checkout/{order_id}", response_class=HTMLResponse)
async def checkout_page(order_id: str):
    order = await payments_collection.find_one({"order_id": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not PAYHERE_MERCHANT_ID or not PAYHERE_MERCHANT_SECRET:
        return HTMLResponse("<h3>PayHere is not configured. Add PAYHERE_MERCHANT_ID and PAYHERE_MERCHANT_SECRET to .env</h3>", 500)
    name  = (order.get("employer_name") or "JobEka Employer").split(" ")
    first, last = name[0], (" ".join(name[1:]) or "Employer")
    fields = {
        "merchant_id": PAYHERE_MERCHANT_ID,
        "return_url":  f"{PUBLIC_BASE_URL}/api/payments/return",
        "cancel_url":  f"{PUBLIC_BASE_URL}/api/payments/cancel",
        "notify_url":  f"{PUBLIC_BASE_URL}/api/payments/notify",
        "order_id":    order_id,
        "items":       order["job_title"][:100],
        "currency":    "LKR",
        "amount":      f"{order['amount']:.2f}",
        "first_name":  first, "last_name": last,
        "email":       order.get("employer_email") or "employer@jobeka.lk",
        "phone":       order.get("employer_phone") or "0770000000",
        "address":     "Sri Lanka", "city": "Colombo", "country": "Sri Lanka",
        "hash":        _checkout_hash(order_id, order["amount"]),
    }
    inputs = "\n".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items())
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JobEka Payment</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px;color:#334155">
<p>Redirecting to secure card payment…</p>
<form id="f" method="post" action="{PAYHERE_HOST}/pay/checkout">{inputs}</form>
<script>document.getElementById('f').submit();</script></body></html>"""


@router.get("/api/payments/return", response_class=HTMLResponse)
async def payment_return(order_id: str = ""):
    return "<html><body style='font-family:sans-serif;text-align:center;padding-top:80px'>Payment completed. Returning to JobEka…</body></html>"

@router.get("/api/payments/cancel", response_class=HTMLResponse)
async def payment_cancel(order_id: str = ""):
    return "<html><body style='font-family:sans-serif;text-align:center;padding-top:80px'>Payment cancelled. Returning to JobEka…</body></html>"


# ─────────────────────────────── 4a. PayHere server notification (works once the backend is public)
@router.post("/api/payments/notify")
async def payment_notify(request: Request):
    form = await request.form()
    order_id    = form.get("order_id", "")
    amount      = form.get("payhere_amount", "")
    currency    = form.get("payhere_currency", "")
    status_code = form.get("status_code", "")
    md5sig      = form.get("md5sig", "")
    local = _md5_upper(f"{PAYHERE_MERCHANT_ID}{order_id}{amount}{currency}{status_code}{_md5_upper(PAYHERE_MERCHANT_SECRET)}")
    if local != md5sig:
        raise HTTPException(status_code=400, detail="Invalid signature")
    order = await payments_collection.find_one({"order_id": order_id})
    if order and status_code == "2":            # 2 = success
        await _complete_payment(order, form.get("payment_id", ""), form.get("method", ""))
    elif order:
        await payments_collection.update_one({"_id": order["_id"]}, {"$set": {"status": f"failed_{status_code}"}})
    return {"ok": True}


# ─────────────────────────────── 4b. App asks the backend to confirm (works on localhost)
async def _retrieve_payment(order_id: str):
    """PayHere Retrieval API (needs a Business App ID + Secret)."""
    if not (PAYHERE_APP_ID and PAYHERE_APP_SECRET):
        return None
    basic = base64.b64encode(f"{PAYHERE_APP_ID}:{PAYHERE_APP_SECRET}".encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(f"{PAYHERE_HOST}/merchant/v1/oauth/token",
                                headers={"Authorization": f"Basic {basic}"},
                                data={"grant_type": "client_credentials"})
        tok.raise_for_status()
        access = tok.json().get("access_token")
        res = await client.get(f"{PAYHERE_HOST}/merchant/v1/payment/search",
                               params={"order_id": order_id},
                               headers={"Authorization": f"Bearer {access}"})
        res.raise_for_status()
        data = res.json().get("data") or []
        return data[0] if data else {}

@router.post("/api/payments/{order_id}/confirm")
async def confirm_payment(order_id: str):
    order = await payments_collection.find_one({"order_id": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("status") == "paid":
        return {"status": "paid", "amount": order["amount"]}
    try:
        info = await _retrieve_payment(order_id)
    except Exception as e:
        info = None
        print("PayHere retrieval error:", e)
    if info and str(info.get("status", "")).upper() in ("RECEIVED", "SUCCESS", "2"):
        await _complete_payment(order, str(info.get("payment_id", "")), str(info.get("method", "")))
        return {"status": "paid", "amount": order["amount"]}
    if info is None and PAYHERE_TRUST_RETURN and PAYHERE_SANDBOX:
        # Sandbox testing on localhost without the Retrieval API
        await _complete_payment(order, "", "sandbox-return")
        return {"status": "paid", "amount": order["amount"], "note": "sandbox: confirmed from return page"}
    return {"status": order.get("status", "pending"), "amount": order["amount"]}

@router.get("/api/payments/by-job/{job_id}")
async def payment_by_job(job_id: str):
    order = await payments_collection.find_one({"job_id": job_id}, sort=[("createdAt", -1)])
    return _clean(order) if order else {}


# ─────────────────────────────── 5. wallet
@router.get("/api/wallet")
async def wallet(email: str):
    email = email.lower()
    user = await users_collection.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    txs = [_clean(t) async for t in transactions_collection.find({"email": email}).sort("createdAt", -1).limit(50)]
    wds = [_clean(w) async for w in withdrawals_collection.find({"email": email}).sort("createdAt", -1).limit(20)]
    return {"balance": round(user.get("wallet_balance", 0), 2), "transactions": txs, "withdrawals": wds}

@router.post("/api/wallet/withdraw")
async def request_withdrawal(body: dict):
    email   = (body.get("email") or "").lower()
    try:
        amount = round(float(body.get("amount", 0)), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Enter a valid amount")
    bank, branch = (body.get("bank_name") or "").strip(), (body.get("branch") or "").strip()
    acc_name, acc_no = (body.get("account_name") or "").strip(), re.sub(r"\s", "", body.get("account_number") or "")
    if amount < MIN_WITHDRAW:
        raise HTTPException(status_code=400, detail=f"Minimum withdrawal is LKR {MIN_WITHDRAW}")
    if not (bank and acc_name and acc_no.isdigit() and 6 <= len(acc_no) <= 20):
        raise HTTPException(status_code=400, detail="Enter bank, account name and a valid account number")
    # Deduct only if the balance is enough (atomic)
    res = await users_collection.update_one(
        {"email": email, "wallet_balance": {"$gte": amount}},
        {"$inc": {"wallet_balance": -amount}},
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    now = datetime.utcnow()
    wd = {"email": email, "amount": amount, "bank_name": bank, "branch": branch,
          "account_name": acc_name, "account_number": acc_no, "status": "pending", "createdAt": now}
    result = await withdrawals_collection.insert_one(wd)
    await transactions_collection.insert_one({
        "email": email, "type": "withdrawal", "amount": amount, "withdrawal_id": str(result.inserted_id),
        "description": f"Withdrawal to {bank} ****{acc_no[-4:]} (pending)", "createdAt": now})
    return {"message": "Withdrawal request submitted", "id": str(result.inserted_id)}


# ─────────────────────────────── Sandbox test: open in a desktop browser
# Builds a LKR 100 PayHere checkout without any job or OTP, so the PayHere
# setup (merchant ID + domain + secret) can be tested on its own.
@router.get("/api/payments/sandbox-test", response_class=HTMLResponse)
async def sandbox_test():
    if not PAYHERE_SANDBOX:
        raise HTTPException(status_code=404, detail="Not available")
    if not PAYHERE_MERCHANT_ID or not PAYHERE_MERCHANT_SECRET:
        return HTMLResponse("<h3>PAYHERE_MERCHANT_ID / PAYHERE_MERCHANT_SECRET missing</h3>", 500)
    order_id = f"TEST{datetime.utcnow().strftime('%H%M%S')}"
    amount = 100.0
    fields = {
        "merchant_id": PAYHERE_MERCHANT_ID,
        "return_url":  f"{PUBLIC_BASE_URL}/api/payments/return",
        "cancel_url":  f"{PUBLIC_BASE_URL}/api/payments/cancel",
        "notify_url":  f"{PUBLIC_BASE_URL}/api/payments/notify",
        "order_id": order_id, "items": "JobEka sandbox test", "currency": "LKR",
        "amount": f"{amount:.2f}",
        "first_name": "Test", "last_name": "Employer", "email": "test@jobeka.lk",
        "phone": "0771234567", "address": "Sri Lanka", "city": "Colombo", "country": "Sri Lanka",
        "hash": _checkout_hash(order_id, amount),
    }
    rows = "".join(f"<tr><td>{k}</td><td>{'(hidden)' if k == 'hash' else v}</td></tr>" for k, v in fields.items())
    inputs = "".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items())
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:sans-serif;padding:24px">
<h3>PayHere sandbox test (LKR 100)</h3>
<p>Posting to <b>{PAYHERE_HOST}/pay/checkout</b> from <b>{PUBLIC_BASE_URL}</b></p>
<table border="1" cellpadding="4" style="border-collapse:collapse;font-size:13px">{rows}</table><br>
<form method="post" action="{PAYHERE_HOST}/pay/checkout">{inputs}
<button type="submit" style="padding:12px 20px;font-size:16px">Go to PayHere</button></form>
</body></html>"""


# ─────────────────────────────── PayHere config check (no secrets shown)
@router.get("/api/payments/config-check")
async def payhere_config_check():
    return {
        "sandbox": PAYHERE_SANDBOX,
        "checkout_host": PAYHERE_HOST,
        "merchant_id": PAYHERE_MERCHANT_ID or "(missing)",
        "merchant_secret_set": bool(PAYHERE_MERCHANT_SECRET),
        "merchant_secret_length": len(PAYHERE_MERCHANT_SECRET),
        "secret_has_spaces_or_quotes": any(ch in PAYHERE_MERCHANT_SECRET for ch in " '\""),
        "public_base_url": PUBLIC_BASE_URL,
        "trust_return": PAYHERE_TRUST_RETURN,
        "retrieval_api_configured": bool(PAYHERE_APP_ID and PAYHERE_APP_SECRET),
    }


# ─────────────────────────────── 6. admin: withdrawal requests
ADMIN_KEY = os.getenv("ADMIN_KEY", "jobeka_admin_2025")

def _check_admin(key):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

@router.get("/api/admin/withdrawals")
async def admin_withdrawals(x_admin_key: str = Header(None)):
    _check_admin(x_admin_key)
    return [_clean(w) async for w in withdrawals_collection.find().sort("createdAt", -1)]

@router.post("/api/admin/withdrawals/{wid}/{action}")
async def admin_withdrawal_action(wid: str, action: str, x_admin_key: str = Header(None)):
    _check_admin(x_admin_key)
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Action must be approve or reject")
    wd = await withdrawals_collection.find_one({"_id": _oid(wid)})
    if not wd or wd["status"] != "pending":
        raise HTTPException(status_code=400, detail="Request not found or already processed")
    new_status = "paid" if action == "approve" else "rejected"
    await withdrawals_collection.update_one({"_id": wd["_id"]}, {"$set": {"status": new_status, "processedAt": datetime.utcnow()}})
    if action == "reject":  # give the money back to the wallet
        await users_collection.update_one({"email": wd["email"]}, {"$inc": {"wallet_balance": wd["amount"]}})
        await transactions_collection.insert_one({
            "email": wd["email"], "type": "refund", "amount": wd["amount"], "withdrawal_id": wid,
            "description": "Withdrawal rejected – amount returned to wallet", "createdAt": datetime.utcnow()})
    return {"status": new_status}
