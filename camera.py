"""
CycleSentinel — Camera
Captures frames from the webcam and saves the latest to a shared
temp file that bridge.py reads whenever it needs a snapshot for Gemini.
"""

import sys
import time
import cv2

FRAME_PATH    = "/tmp/cs_frame.jpg"
DISPLAY_SCALE = 0.75


def find_camera() -> int:
    for idx in [0, 1]:   # 0 = iPhone Continuity Camera, 1 = built-in FaceTime
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            cap.release()
            print(f"[camera] Using camera index {idx}")
            return idx
    raise RuntimeError("No camera found.")


def run(cam_index: int) -> None:
    cap = cv2.VideoCapture(cam_index)
    time.sleep(2)   # Continuity Camera warmup
    print(f"[camera] Ready. Saving frames to {FRAME_PATH}. Press Q to quit.")

    fail_count = 0
    frame_n    = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            fail_count += 1
            if fail_count > 10:
                print("[camera] Camera lost — check connection.")
                break
            time.sleep(0.1)
            continue
        fail_count = 0
        frame_n   += 1

        # Save every 10 frames (~3 fps) — bridge reads this on trigger
        if frame_n % 10 == 0:
            cv2.imwrite(FRAME_PATH, frame, [cv2.IMWRITE_JPEG_QUALITY, 70])

        # Show live feed
        h, w   = frame.shape[:2]
        display = cv2.resize(frame, (int(w * DISPLAY_SCALE), int(h * DISPLAY_SCALE)))
        cv2.putText(display, "CycleSentinel Camera", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 80), 2)
        cv2.imshow("CycleSentinel Camera", display)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else find_camera()
    run(idx)
