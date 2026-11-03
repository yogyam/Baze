"""
Baze — ride simulator

Sends fake sensor events through the same path bridge.py uses, so the
Kafka pipeline can be run and demoed without the Arduino or camera.

    python3 scripts/simulate_ride.py            # 3 events, 1 s apart
    python3 scripts/simulate_ride.py -n 10 -i 0.5
    BAZE_TRANSPORT=http python3 scripts/simulate_ride.py
"""

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bridge  # noqa: E402


def fake_session(rough: bool) -> list[dict]:
    """10 windows of 500 ms each, like a real 5-second recording."""
    base = 0.35 if rough else 0.08
    samples = []
    for i in range(10):
        rms = max(0.0, random.gauss(base, 0.05))
        samples.append({
            "t": round(i * 0.5, 2),
            "x": round(random.gauss(0, 0.1), 4),
            "y": round(random.gauss(0, 0.1), 4),
            "z": round(random.gauss(1, rms), 4),
            "rms": round(rms, 4),
            "peak": round(rms * random.uniform(1.5, 3.0), 4),
        })
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--count", type=int, default=3)
    parser.add_argument("-i", "--interval", type=float, default=1.0)
    args = parser.parse_args()

    print(f"[sim] sending {args.count} events via {bridge.TRANSPORT} as {bridge.DEVICE_ID}")
    for i in range(args.count):
        event = bridge.build_event(fake_session(rough=i % 2 == 0), image_b64=None)
        print(f"[sim] event {i + 1}: {event['event_id'][:8]}… avg_rms={event['avg_rms']}")
        bridge.send_event(event)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
