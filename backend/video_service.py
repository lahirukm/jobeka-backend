"""
JobEka — in-app video interview rooms.

Uses Daily (daily.co) when DAILY_API_KEY is set in the backend .env:
  • free plan: 10,000 participant-minutes a month, no app install, works in the phone browser
  • private rooms + short-lived join tokens (only the employer and that applicant can enter)
Without a key it falls back to a free Jitsi Meet room link.

.env:
  DAILY_API_KEY=xxxxxxxx          (dashboard.daily.co → Developers → API key)
  APP_TZ_OFFSET_MIN=330           (Sri Lanka, UTC+5:30 — interview times are local)
"""
import os
import secrets
import time
from datetime import datetime, timedelta

import httpx

DAILY_API = "https://api.daily.co/v1"
TZ_MIN = int(os.getenv("APP_TZ_OFFSET_MIN", "330"))
OPEN_BEFORE_MIN, OPEN_AFTER_MIN = 15, 180


def _key():
    return os.getenv("DAILY_API_KEY", "").strip()

def start_ts(date_: str, time_: str) -> int:
    """'2026-10-20', '10:30' (Sri Lanka time) → unix seconds (UTC)."""
    local = datetime.strptime(f"{date_} {time_}", "%Y-%m-%d %H:%M")
    return int((local - timedelta(minutes=TZ_MIN) - datetime(1970, 1, 1)).total_seconds())

async def create_room(date_: str, time_: str, label: str = "") -> dict:
    """Returns {"provider", "room", "url"} — a Daily private room, or a Jitsi link if no key / Daily fails."""
    st = start_ts(date_, time_)
    slug = "".join(ch for ch in label if ch.isalnum())[:20] or "Interview"
    name = f"jobeka-{slug.lower()}-{secrets.token_hex(4)}"
    if _key():
        try:
            async with httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {_key()}"}) as c:
                r = await c.post(f"{DAILY_API}/rooms", json={
                    "name": name, "privacy": "private",
                    "properties": {
                        "nbf": st - OPEN_BEFORE_MIN * 60, "exp": st + OPEN_AFTER_MIN * 60,
                        "eject_at_room_exp": True, "enable_prejoin_ui": True, "enable_chat": True,
                        "enable_screenshare": True, "max_participants": 6, "lang": "en",
                    }})
                if r.status_code < 300:
                    j = r.json()
                    return {"provider": "daily", "room": j["name"], "url": j["url"]}
                print("Daily room error:", r.status_code, r.text[:200])
        except Exception as e:
            print("Daily room error:", e)
    return {"provider": "jitsi", "room": name, "url": f"https://meet.jit.si/JobEka-{slug}-{secrets.token_hex(5)}"}

async def join_url(video: dict, user_name: str, owner: bool, date_: str, time_: str) -> str:
    """Link to open now. Daily → adds a personal token valid until the room closes."""
    if (video or {}).get("provider") != "daily":
        return (video or {}).get("url", "")
    st = start_ts(date_, time_)
    now = int(time.time())
    if now < st - OPEN_BEFORE_MIN * 60:
        raise ValueError("The video room opens 15 minutes before the interview")
    if now > st + OPEN_AFTER_MIN * 60:
        raise ValueError("This interview time is over")
    async with httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {_key()}"}) as c:
        r = await c.post(f"{DAILY_API}/meeting-tokens", json={"properties": {
            "room_name": video["room"], "user_name": (user_name or "Guest")[:40], "is_owner": owner,
            "exp": st + OPEN_AFTER_MIN * 60}})
        if r.status_code >= 300:
            raise ValueError("Couldn't open the video room. Try again in a moment.")
        return f'{video["url"]}?t={r.json()["token"]}'
