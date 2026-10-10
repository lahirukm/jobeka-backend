"""
JobEka — place suggestions while typing an address (streets, buildings, villages in Sri Lanka).

GET /api/places/suggest?q=kaduwela&lat=&lng=   → [{name, sub, lat, lng}]

Uses Photon (OpenStreetMap search made for type-ahead, free, no key) and caches answers
for a day so we stay polite to the free service. The app also has an offline list of
towns, so the first letters always show something instantly.
"""
import time
import httpx
from fastapi import APIRouter

router = APIRouter(tags=["Places"])

PHOTON = "https://photon.komoot.io/api/"
LK_BBOX = "79.4,5.8,82.1,9.95"            # minLon,minLat,maxLon,maxLat — Sri Lanka
_cache: dict = {}
TTL = 24 * 3600


def _label(p: dict):
    name = p.get("name") or " ".join(x for x in (p.get("housenumber"), p.get("street")) if x)
    parts = [p.get("street") if p.get("name") else None, p.get("locality") or p.get("district"),
             p.get("city"), p.get("county") or p.get("state")]
    seen, sub = set([name]), []
    for x in parts:
        if x and x not in seen:
            seen.add(x); sub.append(x)
    return name, ", ".join(sub[:3])


@router.get("/api/places/suggest")
async def suggest(q: str = "", lat: float = None, lng: float = None):
    q = q.strip()
    if len(q) < 2:
        return []
    key = (q.lower(), round(lat or 0, 1), round(lng or 0, 1))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]

    params = {"q": q, "limit": 8, "lang": "en", "bbox": LK_BBOX}
    if lat is not None and lng is not None:
        params.update({"lat": lat, "lon": lng})
    try:
        async with httpx.AsyncClient(timeout=4.0, headers={"User-Agent": "JobEka/1.0 (student project)"}) as c:
            r = await c.get(PHOTON, params=params)
            feats = r.json().get("features", []) if r.status_code == 200 else []
    except Exception as e:
        print("place suggest error:", e)
        return []

    out, seen = [], set()
    for f in feats:
        p = f.get("properties", {})
        if (p.get("countrycode") or "LK").upper() != "LK":
            continue
        name, sub = _label(p)
        lon_, lat_ = (f.get("geometry", {}).get("coordinates") or [None, None])[:2]
        if not name or lat_ is None or (name, sub) in seen:
            continue
        seen.add((name, sub))
        out.append({"name": name, "sub": sub, "lat": lat_, "lng": lon_, "kind": p.get("osm_value", "")})

    if len(_cache) > 2000:
        _cache.clear()
    _cache[key] = (time.time(), out)
    return out
