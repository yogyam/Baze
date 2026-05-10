import os
import uuid
import random
import math
import json
import time
import base64
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo import MongoClient, GEOSPHERE
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ── Clients ────────────────────────────────────────────────────────────────
mongo = MongoClient(os.environ["MONGO_URI"])
db = mongo["cyclesentinel"]
spots = db["spots"]
spots.create_index([("location", GEOSPHERE)])  # 2dsphere for geo queries

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(title="CycleSentinel")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# UC Davis campus waypoints — simulates GPS movement along campus paths
CAMPUS_WAYPOINTS = [
    [-121.7407, 38.5382],  # Silo
    [-121.7450, 38.5400],  # Quad path
    [-121.7492, 38.5417],  # Memorial Union
    [-121.7510, 38.5395],  # Library path
    [-121.7497, 38.5388],  # Shields Library
    [-121.7540, 38.5375],  # South campus
    [-121.7557, 38.5393],  # Mondavi Center
    [-121.7580, 38.5410],  # North path
    [-121.7582, 38.5431],  # Bike Barn
    [-121.7560, 38.5450],  # Arboretum
]
_waypoint_index = 0


def next_location() -> list[float]:
    global _waypoint_index
    base = CAMPUS_WAYPOINTS[_waypoint_index % len(CAMPUS_WAYPOINTS)]
    _waypoint_index += 1
    # Small jitter so repeated readings at same waypoint don't stack exactly
    return [
        base[0] + random.uniform(-0.0003, 0.0003),
        base[1] + random.uniform(-0.0003, 0.0003),
    ]


# ── Models ─────────────────────────────────────────────────────────────────
class SensorEvent(BaseModel):
    samples:    list[dict]   # [{t, x, y, z, rms, peak}, ...]
    duration_s: float
    max_peak:   float
    avg_rms:    float
    image_b64:  str | None = None


# ── Gemini analysis (runs in background) ───────────────────────────────────
def _analyze_event(spot_id: str, event: SensorEvent) -> None:
    # Format time series as readable pattern for Gemini
    ts_lines = []
    for s in event.samples:
        level = "▁" if s["rms"] < 0.10 else ("▄" if s["rms"] < 0.25 else "█")
        ts_lines.append(f"  t={s['t']:.1f}s  rms={s['rms']:.3f}g  peak={s['peak']:.3f}g  {level}")
    ts_text = "\n".join(ts_lines)

    prompt = f"""You are a bicycle safety AI analyzing a {event.duration_s:.1f}-second IMU recording from a bike.

Vibration time series (rms and peak per 500ms window):
{ts_text}

Summary: max_peak={event.max_peak:.3f}g  avg_rms={event.avg_rms:.3f}g

{"An image was captured at the moment the sensor triggered. IMPORTANT: the camera may be pointing anywhere — at the road, at the rider, at surroundings. Only use the image if it clearly shows an outdoor road hazard (pothole, debris, obstacle, pedestrian on path). If the image shows indoors, equipment, a ceiling, hands, or anything unrelated to road conditions — IGNORE it completely and base your analysis only on the vibration data." if event.image_b64 else "No camera image available — base analysis on vibration data only."}

Classify this event based on the vibration pattern:
- GREEN: Anything that isn't clearly a road hazard. When in doubt, choose GREEN. Discard silently.
- ORANGE: Clearly sustained moderate roughness for several seconds. Imagine a gravel path or old cracked pavement.
- RED: Only for genuinely dangerous road conditions — severe sustained roughness (avg_rms > 0.4g held for most of the recording) OR repeated extreme spikes (peak > 1.0g multiple times). A single spike followed by calm = GREEN or ORANGE, not RED.

Warning rules — follow these strictly:
- Keep it simple and road-focused: "Rocky road section ahead", "Rough patch, slow down", "Uneven surface, caution zone"
- NEVER mention: sensors, equipment, modules, ceilings, hands, indoor scenes, or anything non-road
- NEVER say "check bike for damage" — we are warning about road conditions only
- Only mention the image if it unmistakably shows an outdoor road hazard (pothole, debris, pedestrian on path). Otherwise ignore the image entirely.
- Do not dramatize. A bumpy road is not an emergency.

Respond ONLY as valid JSON, nothing else:
{{"severity": "RED|ORANGE|GREEN", "warning": "..."}}"""

    has_image = bool(event.image_b64)
    print(f"[Gemini] sending → image={'YES' if has_image else 'NO'} | "
          f"samples={len(event.samples)} | max_peak={event.max_peak:.3f}g")

    try:
        if event.image_b64:
            img_bytes = base64.b64decode(event.image_b64)
            contents  = [
                types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                prompt,
            ]
        else:
            contents = prompt

        response = gemini.models.generate_content(
            model="gemini-2.5-flash", contents=contents
        )
        raw = response.text.strip()
        # Strip markdown fences if Gemini wraps in ```json
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result   = json.loads(raw.strip())
        severity = result.get("severity", "ORANGE").upper()
        warning  = result.get("warning", "Hazard detected ahead.")
        print(f"[Gemini] {spot_id[:8]}… → {severity}: {warning}")
    except Exception as e:
        print(f"[Gemini error] {e}")
        severity = "ORANGE"
        warning  = "Rough road ahead."

    if severity == "GREEN":
        spots.delete_one({"spot_id": spot_id})
        print(f"[Gemini] {spot_id[:8]}… → GREEN, discarded.")
        return

    spots.update_one(
        {"spot_id": spot_id},
        {"$set": {"severity": severity, "ai_summary": warning}}
    )


# ── Endpoints ──────────────────────────────────────────────────────────────
@app.post("/api/event")
async def receive_event(event: SensorEvent, bg: BackgroundTasks):
    spot_id = str(uuid.uuid4())
    spot = {
        "spot_id":    spot_id,
        "location":   {"type": "Point", "coordinates": next_location()},
        "severity":   "PENDING",
        "metrics":    {"max_peak": event.max_peak, "avg_rms": event.avg_rms,
                       "samples": len(event.samples)},
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "ai_summary": "⏳ Analyzing…",
    }
    spots.insert_one(spot)
    spot.pop("_id", None)
    bg.add_task(_analyze_event, spot_id, event)
    return {"spot_id": spot_id, "status": "analyzing"}




@app.get("/api/map/hotspots")
async def get_hotspots():
    all_spots = list(spots.find({}, {"_id": 0}))
    features = [
        {
            "type": "Feature",
            "geometry": s["location"],
            "properties": {
                "spot_id":   s["spot_id"],
                "severity":  s["severity"],
                "rms":       s.get("metrics", {}).get("avg_rms", 0),
                "timestamp": s["timestamp"],
                "ai_summary": s.get("ai_summary"),
            },
        }
        for s in all_spots
    ]
    return {"type": "FeatureCollection", "features": features}


@app.delete("/api/map/reset")
async def reset_map():
    result = spots.delete_many({})
    return {"deleted": result.deleted_count}


@app.get("/health")
async def health():
    return {"status": "ok", "spots_in_db": spots.count_documents({})}


# ── Routing helpers ────────────────────────────────────────────────────────
OSRM = "https://routing.openstreetmap.de/routed-bike/route/v1/driving"
HAZARD_RADIUS_M    = 50   # flag hazard if within this distance of route
AVOIDANCE_OFFSET_M = 65   # detour waypoint offset — must visually clear the circle
CLUSTER_RADIUS_M   = 200  # merge hazards within this distance into one cluster


def _osrm_route(waypoints: list[tuple]) -> tuple[list, float]:
    coords = ";".join(f"{lon},{lat}" for lon, lat in waypoints)
    url = f"{OSRM}/{coords}?overview=full&geometries=geojson"
    with urllib.request.urlopen(url, timeout=6) as r:
        data = json.loads(r.read())
    route = data["routes"][0]
    return route["geometry"]["coordinates"], route["distance"]


def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_on_route(h_lat, h_lon, route_coords) -> tuple[float, tuple]:
    best_d, best_pt = float("inf"), None
    for lon, lat in route_coords:
        d = _haversine(h_lat, h_lon, lat, lon)
        if d < best_d:
            best_d, best_pt = d, (lat, lon)
    return best_d, best_pt


def _bearing(lat1, lon1, lat2, lon2) -> float:
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def _offset(lat, lon, bearing_deg, dist_m) -> tuple[float, float]:
    R = 6_371_000
    d = dist_m / R
    lat1, lon1 = math.radians(lat), math.radians(lon)
    b = math.radians(bearing_deg)
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
    lon2 = lon1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1),
                              math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


class RouteRequest(BaseModel):
    start: list[float]  # [lon, lat]
    end:   list[float]  # [lon, lat]


@app.post("/api/route")
async def safe_route(req: RouteRequest):
    start = (req.start[0], req.start[1])  # (lon, lat)
    end   = (req.end[0],   req.end[1])

    direct_coords, direct_dist = _osrm_route([start, end])

    # Find RED/ORANGE hazards near the direct route
    hazards = list(spots.find({"severity": {"$in": ["RED", "ORANGE"]}}, {"_id": 0}))
    on_route = []
    for h in hazards:
        h_lon, h_lat = h["location"]["coordinates"]
        dist, nearest = _nearest_on_route(h_lat, h_lon, direct_coords)
        if dist < HAZARD_RADIUS_M:
            on_route.append((nearest, h["severity"]))

    if not on_route:
        return {
            "direct":          {"type": "LineString", "coordinates": direct_coords},
            "safe":            {"type": "LineString", "coordinates": direct_coords},
            "hazards_avoided": 0,
            "direct_dist_m":   round(direct_dist),
            "safe_dist_m":     round(direct_dist),
            "message":         "Route is clear — no hazards detected!",
        }

    # Cluster nearby hazards so we create one waypoint per cluster, not per hazard
    clusters = []
    for (r_lat, r_lon), sev in on_route:
        merged = False
        for c in clusters:
            if _haversine(r_lat, r_lon, c["lat"], c["lon"]) < CLUSTER_RADIUS_M:
                c["lats"].append(r_lat); c["lons"].append(r_lon)
                c["lat"] = sum(c["lats"]) / len(c["lats"])
                c["lon"] = sum(c["lons"]) / len(c["lons"])
                merged = True; break
        if not merged:
            clusters.append({"lat": r_lat, "lon": r_lon, "lats": [r_lat], "lons": [r_lon]})

    # Try both left and right offsets per cluster, pick the shorter resulting route
    end_lat, end_lon = end[1], end[0]

    def _try_route(side: int) -> tuple[list, float]:
        wps = []
        for c in clusters:
            brg = _bearing(c["lat"], c["lon"], end_lat, end_lon)
            a_lat, a_lon = _offset(c["lat"], c["lon"], (brg + side) % 360, AVOIDANCE_OFFSET_M)
            wps.append((a_lon, a_lat))
        return _osrm_route([start] + wps + [end])

    best_coords, best_dist = direct_coords, direct_dist
    for side in [90, 270]:   # right then left
        try:
            coords, dist = _try_route(side)
            if dist < best_dist:
                best_coords, best_dist = coords, dist
        except Exception:
            pass

    # Sanity check: if best detour is >60% longer than direct, it's gone off-road.
    # Return direct route instead — better to warn cyclist than send them on a wild detour.
    if best_dist > direct_dist * 1.60:
        best_coords, best_dist = direct_coords, direct_dist
        message = (f"Hazard zone detected but no clean detour found. "
                   f"Proceed with caution through {len(on_route)} hazard(s).")
    else:
        extra_m = round(best_dist - direct_dist)
        message = (f"Avoided {len(on_route)} hazard(s) in {len(clusters)} zone(s). "
                   f"Safe route is {extra_m:+d}m vs direct path.")

    return {
        "direct":          {"type": "LineString", "coordinates": direct_coords},
        "safe":            {"type": "LineString", "coordinates": best_coords},
        "hazards_avoided": len(on_route),
        "direct_dist_m":   round(direct_dist),
        "safe_dist_m":     round(best_dist),
        "message":         message,
    }
