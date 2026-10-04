from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from database import ping_db
import ml_loader
from jobs_router  import router as jobs_router
from ai_router    import router as ai_router
from users_router import router as users_router
from admin_router import router as admin_router
from payments_router import router as payments_router
from banners_router import router as banners_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ping_db()
    ml_loader.load_models()
    yield


app = FastAPI(
    title="JobEka API",
    description="JobEka — Jobs CRUD + AI Models + User Auth (JWT)",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes
app.include_router(users_router)  # /api/auth/register, /login, /me, /profile
app.include_router(jobs_router)   # /api/jobs/
app.include_router(ai_router)     # /api/ai/
app.include_router(admin_router)  # /api/admin/
app.include_router(payments_router)  # arrival OTP, PayHere payments, wallet, withdrawals
app.include_router(banners_router)   # home-screen promo banners


@app.get("/")
async def root():
    return {"message": "✅ JobEka API v2.0 running!", "docs": "/docs"}
