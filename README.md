# CycleSentinel v2 — Bimodal Bicycle Safety System

> **HackDavis 2026** · Built by Yogya Mehrotra (Computer Engineering, UC Santa Cruz)

CycleSentinel is an autonomous safety attachment for bicycles that fuses **vibration sensing** and **computer vision** to detect, classify, and reroute around road hazards in real time — powered by Gemini multimodal AI and a cloud-hosted FastAPI backend.

---

## The Problem

Every year, thousands of cyclists are injured due to road hazards — potholes, cracked pavement, debris, and unexpected obstacles. Unlike cars, bicycles have no suspension, no crumple zones, and no onboard safety systems. Cyclists are entirely dependent on their own reaction time and visibility.

On college campuses like UC Davis, where thousands of students commute daily by bike, deteriorating infrastructure goes unreported and unrepaired for months. There is no system that tells you **where** the dangerous roads are before you ride them, and no feedback loop that gets those hazards fixed.

**CycleSentinel solves this.**

---

## What It Does

CycleSentinel mounts on any bicycle and continuously monitors two data streams:

### 1. IMU (Vibration Sensing)
An accelerometer reads road surface conditions 100 times per second. A 500ms rolling window computes the **total vibration magnitude** across all three axes (gravity-removed), capturing both sustained roughness and sharp impact spikes.

### 2. Camera (Visual Context)
A forward-facing camera captures the scene at the moment a vibration event is detected. The raw frame is sent alongside the sensor data to Gemini for analysis.

### 3. Gemini Multimodal AI Fusion
When the bridge detects 5 seconds of unusual vibration, it sends the full time-series data + the camera snapshot to **Gemini 2.5 Flash**. Gemini analyzes both signals together and returns:
- A **severity classification** (RED / ORANGE / GREEN)
- A **plain-English warning** for the cyclist ("Rocky road section ahead, slow down")

GREEN events are silently discarded. Only genuine hazards reach the map.

### 4. Live Hazard Map
A real-time dashboard shows hazard zones across the UC Davis campus as translucent circles — red for critical, orange for moderate. Clicking any circle reveals Gemini's AI-generated description of what happened at that location.

### 5. Safe Route Planning
Given a start and end location, the system queries MongoDB for all hazard zones near the direct route and generates a detour that avoids them — a campus-scale Waze for cyclists.

---

## Social Impact

| Problem | CycleSentinel's Response |
|---|---|
| Dangerous roads go unreported | Automatic hazard logging with GPS coordinates |
| Cyclists hit hazards they can't see | Real-time warnings before you reach the danger zone |
| Campus facilities have no feedback loop | AI-generated work orders with specific location data |
| Data stays siloed on one device | Shared hazard map benefits every cyclist on campus |

Every RED alert automatically generates an **AggieFacilities-style work order** via Gemini — complete with vibration magnitude, camera context, and location — creating a direct pipeline from detection to repair. The more cyclists use CycleSentinel, the better the map gets for everyone.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        EDGE LAYER                           │
│                                                             │
│  [Arduino Uno R4]  →  [bridge.py]  →  [camera.py]         │
│  MMA7660FC IMU         Trigger &        Raw frame           │
│  raw x,y,z,rms,peak    recording        to /tmp/            │
└──────────────────────────────┬──────────────────────────────┘
                               │ POST /api/event
                               │ {time series + image}
┌──────────────────────────────▼──────────────────────────────┐
│                      CLOUD LAYER (Vultr)                    │
│                                                             │
│  FastAPI Backend                                            │
│  ├── /api/event     → stores PENDING spot in MongoDB        │
│  ├── _analyze_event → Gemini 2.5 Flash (background task)   │
│  │   ├── image + vibration time series → severity + warning │
│  │   └── GREEN → discard | RED/ORANGE → update MongoDB      │
│  ├── /api/map/hotspots → GeoJSON for dashboard              │
│  └── /api/route    → OSRM safe routing around hazards       │
│                                                             │
│  MongoDB Atlas (2dsphere geo index)                         │
└──────────────────────────────┬──────────────────────────────┘
                               │ polls every 3s
┌──────────────────────────────▼──────────────────────────────┐
│                     DASHBOARD LAYER                         │
│                                                             │
│  Leaflet.js + CartoDB Voyager tiles                         │
│  ├── Live hazard circles (RED/ORANGE, translucent)          │
│  ├── Gemini AI summary in popup + sidebar                   │
│  ├── Safe route planner (address or map-click)              │
│  └── Direct vs safe route comparison                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Microcontroller | Arduino Uno R4 WiFi |
| IMU Sensor | MMA7660FC (Grove 3-Axis ±1.5g) |
| Edge Bridge | Python + pyserial |
| Camera | iPhone Continuity Camera / Logitech webcam |
| Backend | FastAPI + Uvicorn |
| Cloud Compute | Vultr Cloud Instance |
| AI | Google Gemini 2.5 Flash (multimodal) |
| Database | MongoDB Atlas (2dsphere geospatial index) |
| Routing | OSRM (OpenStreetMap bike routing) |
| Geocoding | Nominatim |
| Dashboard | Vanilla JS + Leaflet.js |

---

## Repository Structure

```
Baize/
├── arduino/
│   └── CycleSentinel/
│       └── CycleSentinel.ino   # Arduino firmware — reads IMU, streams raw data
├── backend/
│   ├── main.py                 # FastAPI backend — event ingestion, Gemini, routing
│   └── requirements.txt
├── bridge.py                   # Serial bridge — trigger detection, session recording
├── camera.py                   # Camera capture — saves frames for Gemini
└── dashboard/
    └── index.html              # Live hazard map — Leaflet.js dashboard
```

---

## How to Run

### Prerequisites
- Arduino IDE with "Arduino UNO R4 Boards" package installed
- Python 3.11+
- MongoDB Atlas account (free tier)
- Google Gemini API key (aistudio.google.com)

### 1. Flash the Arduino
Open `arduino/CycleSentinel/CycleSentinel.ino` in Arduino IDE and upload to your Arduino Uno R4 WiFi. Wire the Grove 3-Axis Accelerometer to the I2C port on the Grove Shield.

### 2. Set up the backend
```bash
cd backend
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create `backend/.env`:
```
MONGO_URI=mongodb+srv://...
GEMINI_API_KEY=...
```

```bash
venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

### 3. Run the bridge
```bash
venv/bin/python3 bridge.py
```

### 4. Run the camera (optional)
```bash
venv/bin/python3 camera.py
```

### 5. Open the dashboard
Open `dashboard/index.html` in your browser.

---

## The Detection Flow

1. Arduino streams `{x, y, z, rms, peak}` over USB serial at 2 Hz
2. `bridge.py` monitors for `rms > 0.12g` or `peak > 0.20g` — anything above smooth riding
3. On trigger: snapshot the camera, record 5 seconds of IMU data
4. POST the full time-series + image to the backend
5. Backend stores a PENDING spot, kicks off Gemini analysis in the background
6. Gemini reads the vibration curve over time + the image, classifies RED/ORANGE/GREEN
7. GREEN → silently deleted. RED/ORANGE → map pin appears with Gemini's warning
8. Dashboard polls every 3 seconds and updates automatically

---

## Why Gemini?

Traditional threshold-based systems fire false alarms constantly — a single bump, a cable, someone touching the sensor. By sending the **full 5-second time series** to Gemini alongside the camera image, CycleSentinel can distinguish between:

- A hand touching a wire (GREEN — discard)
- A single speed bump (ORANGE — caution)
- 5 seconds of sustained rough pavement with debris in frame (RED — critical)

Gemini understands context. Rule-based systems don't.

---

## Prizes Targeted

- 🏆 Best Use of Gemini API
- 🏆 Best Use of Vultr
- 🏆 Best Use of MongoDB Atlas
- 🏆 Best Social Good Hack

---

*Built in 48 hours at HackDavis 2026.*
