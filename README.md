# JobEka API — FastAPI Backend

Production REST API for **[JobEka](https://github.com/lahirukm/Job_Eka)**, an AI-powered job and services platform for Sri Lanka. It handles authentication, jobs, ML-based matching, real-time service requests, payments and AI features.

**Live:** https://jobeka-backend.onrender.com/docs (Swagger UI)

---

## 🧰 Tech Stack

- **Python + FastAPI** (async)
- **MongoDB Atlas** with Motor (async driver)
- **scikit-learn** RandomForest models loaded from `.pkl` for job matching
- **Google Gemini** for CV writing, job descriptions, interview prep and feedback
- **JWT (python-jose) + bcrypt** authentication with role-based access
- **PayHere** payment integration (sandbox)
- Deployed on **Render**

## 📦 Modules

| Router | Responsibility |
|---|---|
| `users_router` | Register, login, profile, `/me` (JWT) |
| `jobs_router` | Part-time & full-time job CRUD |
| `ai_router`, `ai_match.py`, `ml_loader.py` | ML job matching with trained models |
| `applications_router` | Full-time job applications |
| `service_router` | On-demand "Request now" between customers and service providers |
| `providers_router` | Find Help Near Me listings & bookings |
| `payments_router` | Arrival OTP, PayHere payments, wallet, withdrawals |
| `cv_router` | AI CV builder with templates |
| `interview_prep_router` | Interview answers generated from the user's CV |
| `job_ai_router` | AI-written job descriptions & requirements |
| `feedback_router` | AI feedback on completed jobs/services |
| `govt_jobs_router` | Government job posts with Gazette PDFs |
| `places_router` | Address autocomplete |
| `admin_router`, `banners_router`, `home_services_router` | Admin tools & home-screen content |
| `account_router` | Saved jobs, notifications, privacy, help & support |

## 🚀 Run Locally

```bash
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Create `backend/.env`:

```env
MONGO_URI=mongodb+srv://<user>:<url-encoded-password>@<cluster>/
DB_NAME=jobeka
JWT_SECRET=<random-secret>
GEMINI_API_KEY=<your-key>
COMMISSION_RATE=0.06
PAYHERE_MERCHANT_ID=<sandbox-id>
PAYHERE_MERCHANT_SECRET=<sandbox-secret>
PAYHERE_SANDBOX=true
PUBLIC_BASE_URL=http://localhost:8000
```

Then open http://localhost:8000/docs.

## 👤 Author

**K.M.L. Sampath** ([@lahirukm](https://github.com/lahirukm)) — backend, ML integration and mobile app, as part of the JobEka final year project team.
