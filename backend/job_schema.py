from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime


class JobCreate(BaseModel):
    title:          str
    type:           str = "Part Time"
    category:       str
    location:       str
    latitude:       Optional[float] = None
    longitude:      Optional[float] = None
    salary:         str
    working_hours:  Optional[str]   = ""
    description:    Optional[str]   = ""
    requirements:   Optional[str]   = ""
    employer_name:  Optional[str]   = ""
    employer_phone: Optional[str]   = ""
    employer_email: Optional[str]   = ""   # ← NEW: identifies who posted
    payment_method: Optional[str]   = "card"  # "card" (PayHere) or "cash" (paid by hand)
    # full-time / contract details (used by the AI match)
    required_skills:  Optional[List[str]] = []
    experience_years: Optional[float]     = 0
    education_level:  Optional[str]       = ""
    languages:        Optional[List[str]] = []


class JobUpdate(BaseModel):
    title:          Optional[str]   = None
    type:           Optional[str]   = None
    category:       Optional[str]   = None
    location:       Optional[str]   = None
    latitude:       Optional[float] = None
    longitude:      Optional[float] = None
    salary:         Optional[str]   = None
    working_hours:  Optional[str]   = None
    description:    Optional[str]   = None
    requirements:   Optional[str]   = None
    status:         Optional[str]   = None
    employer_name:  Optional[str]   = None  # ← NEW
    employer_phone: Optional[str]   = None  # ← NEW
    payment_method: Optional[str]   = None  # "card" or "cash"
    required_skills:  Optional[List[str]] = None
    experience_years: Optional[float]     = None
    education_level:  Optional[str]       = None
    languages:        Optional[List[str]] = None


class JobOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id:            str   = Field(alias="_id")
    title:         str
    type:          str
    category:      str
    location:      str
    latitude:      Optional[float] = None
    longitude:     Optional[float] = None
    salary:        str
    working_hours: Optional[str]  = ""
    description:   Optional[str] = ""
    requirements:  Optional[str] = ""
    status:        str            = "active"
    applicants:    int            = 0
    createdAt:     datetime
