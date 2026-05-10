"""
CycleSentinel — IMU Bridge
Reads raw sensor data from Arduino, triggers a recording session when
something interesting is detected, then sends the full time-series +
camera snapshot to the backend. Gemini decides severity — not rules.
"""

import sys
import os
import json
import time
import base64
import urllib.request
import glob

import serial

BAUD_RATE      = 115200
BACKEND        = "http://localhost:8000/api/event"
FRAME_PATH     = "/tmp/cs_frame.jpg"   # written by camera.py

# ── Trigger threshold (low — just detects "something happened") ────────────
# We're not classifying here, just deciding whether to start recording.
TRIGGER_RMS    = 0.12   # g — above normal smooth riding
TRIGGER_PEAK   = 0.20   # g — any noticeable impact

# ── Recording session ──────────────────────────────────────────────────────
RECORD_SECONDS = 5.0    # collect this many seconds of data after trigger
COOLDOWN_S     = 15.0   # minimum gap between sessions (prevents spam)


def find_port() -> str:
    candidates = glob.glob("/dev/cu.usbmodem*")
    if not candidates:
        raise RuntimeError("No Arduino found. Check USB.")
    if len(candidates) > 1:
        print(f"[bridge] Multiple ports: {candidates} — using {candidates[0]}")
    return candidates[0]


def read_frame() -> str | None:
    """Read the latest camera frame as base64, or None if unavailable."""
    if not os.path.exists(FRAME_PATH):
        return None
    try:
        with open(FRAME_PATH, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None


def post_event(samples: list, image_b64: str | None) -> None:
    max_peak = max(s["peak"] for s in samples)
    avg_rms  = sum(s["rms"] for s in samples) / len(samples)

    payload = {
        "samples":    samples,
        "duration_s": len(samples) * 0.5,
        "max_peak":   round(max_peak, 4),
        "avg_rms":    round(avg_rms, 4),
        "image_b64":  image_b64,
    }
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        BACKEND, data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            print(f"  → event sent | spot: {result.get('spot_id','?')[:8]}… | Gemini analyzing in background…")
    except Exception as e:
        print(f"  [warn] post failed: {e}")


def run(port: str) -> None:
    print(f"[bridge] Connecting to {port} @ {BAUD_RATE}…")
    ser = serial.Serial(port, BAUD_RATE, timeout=2)
    time.sleep(2)   # wait for Arduino to boot — do NOT reset buffer (we want startup messages)
    print("[bridge] Connected — reading Arduino output:\n")

    state         = "IDLE"      # IDLE | RECORDING
    session       = []          # list of sample dicts
    session_start = 0.0
    snapshot      = None        # base64 frame taken at trigger
    last_event_t  = 0.0

    while True:
        try:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[arduino] {raw}")   # print startup messages and errors verbatim
                continue

            rms  = data.get("rms",  0.0)
            peak = data.get("peak", 0.0)
            now  = time.time()

            # Sensor fault: all axes read 0 → vibMagnitude returns exactly 1.0g
            if abs(rms - 1.0) < 0.001 and abs(peak - 1.0) < 0.001:
                print(f"[sensor] ⚠ FAULT — accelerometer not responding (check Grove connector)")
                continue

            print(f"[sensor] rms={rms:.4f}g  peak={peak:.4f}g  [{state}]")

            if state == "IDLE":
                # Check if we should start a recording session
                triggered = (rms > TRIGGER_RMS or peak > TRIGGER_PEAK)
                cooled    = (now - last_event_t) >= COOLDOWN_S

                if triggered and cooled:
                    snapshot = read_frame()
                    has_img  = "📸 snapshot captured" if snapshot else "⚠ no snapshot (camera.py not running?)"
                    print(f"  ↑ TRIGGERED — recording {RECORD_SECONDS}s session | {has_img}")
                    session       = []
                    session_start = now
                    state         = "RECORDING"
                    # Fall through to record this sample too

            if state == "RECORDING":
                sample = {
                    "t":    round(now - session_start, 2),
                    "x":    round(data.get("x", 0), 4),
                    "y":    round(data.get("y", 0), 4),
                    "z":    round(data.get("z", 0), 4),
                    "rms":  round(rms,  4),
                    "peak": round(peak, 4),
                }
                session.append(sample)

                elapsed = now - session_start
                print(f"  ● recording {elapsed:.1f}/{RECORD_SECONDS}s  "
                      f"({len(session)} samples)")

                if elapsed >= RECORD_SECONDS:
                    print(f"  ✓ session complete — {len(session)} samples, "
                          f"peak={max(s['peak'] for s in session):.3f}g")
                    post_event(session, snapshot)
                    last_event_t = now
                    state        = "IDLE"
                    session      = []
                    snapshot     = None

        except KeyboardInterrupt:
            print("\n[bridge] Stopped.")
            break
        except Exception as e:
            print(f"  [error] {e}")
            time.sleep(0.2)

    ser.close()


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    run(port)
