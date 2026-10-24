#!/usr/bin/env python3
"""
Webcam QR scanner -> Raspberry Pi uploader.

Runs on your computer. Opens the webcam, detects QR codes, processes each
one, and POSTs the result to the receiver on the Raspberry Pi (pi_server.py).

How each QR code is processed:
  - "GPS(lat,lon)"  -> converted to ArduPilot/MAVLink format: degrees * 1e7
                       as integers (e.g. GPS(33.2, -117.24) -> 332000000, -1172400000)
  - a number        -> looked up in your map file (qr_map.json); the map's
                       value is what gets sent. Numbers not in the map are skipped.
  - anything else   -> skipped (not sent), with a message in the terminal.

Install:  pip install opencv-python requests
Run:      python qr_scanner.py --pi 192.168.1.50 --map qr_map.json
"""
import argparse
import json
import re
import time

import cv2
import requests

# Matches GPS(num1,num2) — case-insensitive, spaces allowed, optional decimals/sign
GPS_PATTERN = re.compile(
    r"^\s*GPS\s*\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\)\s*$",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*$")


def load_map(path):
    with open(path) as f:
        m = json.load(f)
    if not isinstance(m, dict):
        raise SystemExit(f"{path} must contain a JSON object like {{\"1\": \"value\"}}")
    # JSON keys are always strings; keep them that way for lookup
    return {str(k).strip(): v for k, v in m.items()}


def to_ardupilot(lat, lon):
    """Convert decimal degrees to MAVLink/ArduPilot int32 degE7 format."""
    if not -90 <= lat <= 90:
        raise ValueError(f"latitude {lat} is out of range (-90 to 90)")
    if not -180 <= lon <= 180:
        raise ValueError(f"longitude {lon} is out of range (-180 to 180)")
    return round(lat * 1e7), round(lon * 1e7)


def lookup_number(text, mapping):
    """Find a number in the map. '5', '05' and '5.0' all match a key of '5'."""
    text = text.strip()
    if text in mapping:
        return text, mapping[text]
    num = float(text)
    for candidate in (str(int(num)) if num.is_integer() else None, repr(num)):
        if candidate is not None and candidate in mapping:
            return candidate, mapping[candidate]
    return None, None


def process_qr(text, mapping):
    """Turn raw QR text into the payload for the Pi, or None to skip it."""
    gps = GPS_PATTERN.match(text)
    if gps:
        lat, lon = float(gps.group(1)), float(gps.group(2))
        try:
            lat_e7, lon_e7 = to_ardupilot(lat, lon)
        except ValueError as e:
            print(f"[skip] {text!r}: {e}")
            return None
        return {"type": "gps", "raw": text, "lat": lat, "lon": lon,
                "lat_e7": lat_e7, "lon_e7": lon_e7}

    if NUMBER_PATTERN.match(text):
        key, value = lookup_number(text, mapping)
        if key is None:
            print(f"[skip] {text!r} is not in the map")
            return None
        return {"type": "mapped", "raw": text, "key": key, "value": value}

    print(f"[skip] {text!r} is neither GPS(...) nor a number")
    return None


def send_to_pi(url, payload):
    payload = {**payload, "scanned_at": time.time()}
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
        print(f"[sent] {payload}")
        return True
    except requests.RequestException as e:
        print(f"[error] couldn't reach the Pi: {e}")
        return False


def main():
    p = argparse.ArgumentParser(description="Scan QR codes and upload them to a Raspberry Pi.")
    p.add_argument("--pi", required=True, help="Pi's IP address or hostname, e.g. 192.168.1.50 or raspberrypi.local")
    p.add_argument("--port", type=int, default=5000, help="Port the Pi server listens on (default 5000)")
    p.add_argument("--map", default="qr_map.json", help="JSON file mapping numbers to values (default qr_map.json)")
    p.add_argument("--camera", type=int, default=0, help="Webcam index (default 0)")
    p.add_argument("--cooldown", type=float, default=3.0,
                   help="Seconds before the same code is handled again (default 3)")
    args = p.parse_args()

    mapping = load_map(args.map)
    print(f"Loaded {len(mapping)} entries from {args.map}")

    url = f"http://{args.pi}:{args.port}/qr"
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        raise SystemExit(f"Couldn't open webcam {args.camera}. Try --camera 1.")

    detector = cv2.QRCodeDetector()
    last_seen = {}  # raw text -> time it was last handled
    print(f"Scanning... sending to {url}. Press q in the video window to quit.")

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[error] lost the webcam feed")
            break

        data, points, _ = detector.detectAndDecode(frame)
        if points is not None and data:
            pts = points.astype(int).reshape(-1, 2)
            cv2.polylines(frame, [pts], True, (0, 255, 0), 3)
            cv2.putText(frame, data[:50], (pts[0][0], pts[0][1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            now = time.time()
            if now - last_seen.get(data, 0) > args.cooldown:
                last_seen[data] = now
                payload = process_qr(data, mapping)
                if payload is not None and not send_to_pi(url, payload):
                    last_seen.pop(data)  # retry next frame if the send failed

        cv2.imshow("QR Scanner (q to quit)", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
