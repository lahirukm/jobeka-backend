from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import Optional
from datetime import datetime


# ── Register request
class UserRegister(BaseModel):
    name:     str
    email:    str
    password: str
    phone:    Optional[str] = ""
    role:     str = "job_seeker"  # job_seeker | employer | service_provider
    # Extra fields for employer/service
    company:  Optional[str] = ""
    skills:   Optional[str] = ""


# ── Login request
class UserLogin(BaseModel):
    email:    str
    password: str


# ── Response (no password)
class UserOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id:        str = Field(alias="_id")
    name:      str
    email:     str
    phone:     Optional[str] = ""
    role:      str
    company:   Optional[str] = ""
    skills:    Optional[str] = ""
    createdAt: datetime


# ── Token response
class TokenOut(BaseModel):
    token:     str
    user:      dict
