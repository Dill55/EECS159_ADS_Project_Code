#!/usr/bin/env python3
"""
Raspberry Pi QR scanner.

Runs on the Pi. Reads QR codes from the Pi camera and talks to the server
(server/alternate_send.py).

How each QR code is processed:
  - a number        -> looked up on the server (GET /ask). The files the
                       server returns are saved into --out (default FlightPath/).
  - "GPS(lat,lon)"  -> converted to ArduPilot/MAVLink format: degrees * 1e7
                       as integers (e.g. GPS(33.2, -117.24) -> 332000000, -1172400000)
                       and POSTed to the server (/ads_server).
  - anything else   -> skipped, with a message in the terminal.

Install:  pip install opencv-python requests   (plus picamera2 on the Pi)
Run:      python3 alternate_read.py --ip 192.168.1.50
"""
import argparse
import base64
import binascii
import os
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


def to_ardupilot(lat, lon):
    """Convert decimal degrees to MAVLink/ArduPilot int32 degE7 format."""
    if not -90 <= lat <= 90:
        raise ValueError(f"latitude {lat} is out of range (-90 to 90)")
    if not -180 <= lon <= 180:
        raise ValueError(f"longitude {lon} is out of range (-180 to 180)")
    return round(lat * 1e7), round(lon * 1e7)


def summarize(value):
    """Copy of value for printing, with file contents shown as their size."""
    if isinstance(value, dict):
        if set(value) == {"b64"}:
            return f"<file, {len(base64.b64decode(value['b64']))} bytes>"
        return {k: summarize(v) for k, v in value.items()}
    return value


def process_qr(text):
    """Turn a GPS(...) QR code into the payload for the server, or None to skip it.

    Number codes never reach here: main() sends them to ask_server() instead.
    """
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

    print(f"[skip] {text!r} is neither GPS(...) nor a number")
    return None


def send_to_pi(url, payload):
    payload = {**payload, "scanned_at": time.time()}
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
        print(f"[sent] {summarize(payload)}")
        return True
    except requests.RequestException as e:
        print(f"[error] couldn't reach the Pi: {e}")
        return False


def ask_server(url, key):
    """GET /ask?key=<key> so the server can look the key up in its map.

    Returns the server's JSON reply on success, False if the server answered
    but refused (e.g. 404 key not in map — retrying won't help), or None if
    the request failed in a way worth retrying (network error, 5xx).
    """
    try:
        r = requests.get(url, params={"key": key}, timeout=5)
    except requests.RequestException as e:
        print(f"[error] couldn't reach the server: {e}")
        return None
    if 400 <= r.status_code < 500:
        try:
            msg = r.json().get("error", r.text)
        except ValueError:
            msg = r.text[:200]
        print(f"[skip] server rejected key {key!r} ({r.status_code}): {msg}")
        return False
    if r.status_code >= 500:
        print(f"[error] server error for key {key!r} ({r.status_code}): {r.text[:200]!r}")
        return None
    try:
        reply = r.json()
    except ValueError:
        print(f"[error] server reply to key {key!r} wasn't JSON: {r.text[:200]!r}")
        return None
    print(f"[asked] {r.url} -> {summarize(reply)}")
    return reply


def save_files(value, out_dir):
    """Write {"file name": contents} entries from the server into out_dir.

    Contents are plain text, or {"b64": ...} for a file sent from disk.
    Returns the list of paths written.
    """
    if not isinstance(value, dict):
        print("  not a file entry, nothing to write")
        return []
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, contents in value.items():
        safe = os.path.basename(str(name))  # never write outside out_dir
        if not safe:
            print(f"  skipping {name!r}: not a valid file name")
            continue
        if isinstance(contents, str):
            data, mode = contents, "w"
        elif isinstance(contents, dict) and isinstance(contents.get("b64"), str):
            try:
                data, mode = base64.b64decode(contents["b64"], validate=True), "wb"
            except binascii.Error:
                print(f"  skipping {name!r}: file data is corrupted")
                continue
        else:
            print(f"  skipping {name!r}: contents must be text or a file")
            continue
        path = os.path.join(out_dir, safe)
        with open(path, mode) as f:
            f.write(data)
        print(f"  wrote {path} ({len(data)} {'bytes' if mode == 'wb' else 'characters'})")
        written.append(path)
    return written


def main():
    p = argparse.ArgumentParser(description="Scan QR codes and upload them to a Raspberry Pi.")
    p.add_argument("--ip", required=True, help="Server's IP address or hostname, e.g. 192.168.1.50 or raspberrypi.local")
    p.add_argument("--port", type=int, default=5000, help="Port the Pi server listens on (default 5000)")
    p.add_argument("--out", default="FlightPath",
                   help="Folder to save files returned by the server (default FlightPath)")
    p.add_argument("--cooldown", type=float, default=3.0,
                   help="Seconds before the same code is handled again (default 3)")
    args = p.parse_args()

    url = f"http://{args.ip}:{args.port}/ads_server"
    ask_url = f"http://{args.ip}:{args.port}/ask"


    from picamera2 import Picamera2  # type: ignore[reportMissingImports]  # imported here so the helpers above work off-Pi
    picam = Picamera2()
    picam.configure(picam.create_video_configuration(main={"format": "RGB888", "size": (640, 480)}))
    picam.start()

    detector = cv2.QRCodeDetector()
    last_seen = {}  # raw text -> time it was last handled
    print(f"Scanning... sending to {url}. Press q in the video window to quit.")

    try:
        while True:
            frame = picam.capture_array()

            data, points, _ = detector.detectAndDecode(frame)
            if points is not None and data:
                pts = points.astype(int).reshape(-1, 2)
                cv2.polylines(frame, [pts], True, (0, 255, 0), 3)
                cv2.putText(frame, data[:50], (pts[0][0], pts[0][1] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                now = time.time()
                if now - last_seen.get(data, 0) > args.cooldown:
                    last_seen[data] = now
                    if NUMBER_PATTERN.match(data):
                        # Number codes: ask the server for the matching map entry
                        reply = ask_server(ask_url, data.strip())
                        if reply is None:
                            last_seen.pop(data)  # retry next frame if the ask failed
                        elif reply:
                            save_files(reply.get("value"), args.out)
                    else:
                        payload = process_qr(data)
                        if payload is not None and not send_to_pi(url, payload):
                            last_seen.pop(data)  # retry next frame if the send failed

            cv2.imshow("QR Scanner (q to quit)", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        picam.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()