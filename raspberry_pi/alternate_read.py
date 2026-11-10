#!/usr/bin/env python3
"""
Webcam QR scanner -> Raspberry Pi uploader.

Runs on your computer. Opens the webcam, detects QR codes, processes each
one, and POSTs the result to the receiver on the Raspberry Pi
(raspberry_pi/pi_qr_read_server.py).

How each QR code is processed:
  - "GPS(lat,lon)"  -> converted to ArduPilot/MAVLink format: degrees * 1e7
                       as integers (e.g. GPS(33.2, -117.24) -> 332000000, -1172400000)
  - a number        -> looked up in your map file (qr_map.json); the map's
                       value is what gets sent. Numbers not in the map are skipped.
  - anything else   -> skipped (not sent), with a message in the terminal.

Sending files: a map entry shaped like {"name on Pi": {"file": "path"}} sends
that file from this computer to the Pi. Any file works, text or binary. The
file is read fresh on every scan, and relative paths are relative to the
folder qr_map.json is in. Contents typed directly ({"name": "text"}) still work.

Number codes are looked up on the server (GET /ask); the files it returns
are saved into --out (default FlightPath/).

Install:  pip install opencv-python requests   (plus picamera2 on the Pi)
Run:      python3 alternate_read.py --ip 192.168.1.50
"""
import argparse
import base64
import json
import os
import re
import time

import binascii

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


def load_files(value, map_dir):
    """Replace {"file": path} entries with that file's bytes, base64-encoded.

    The Pi turns {"b64": ...} back into the exact original bytes, so binary
    files arrive unchanged. Raises OSError if a file can't be read.
    """
    if not isinstance(value, dict):
        return value
    out = {}
    for name, spec in value.items():
        if isinstance(spec, dict) and "file" in spec:
            with open(os.path.join(map_dir, spec["file"]), "rb") as f:
                out[name] = {"b64": base64.b64encode(f.read()).decode("ascii")}
        else:
            out[name] = spec
    return out


def summarize(value):
    """Copy of value for printing, with file contents shown as their size."""
    if isinstance(value, dict):
        if set(value) == {"b64"}:
            return f"<file, {len(base64.b64decode(value['b64']))} bytes>"
        return {k: summarize(v) for k, v in value.items()}
    return value


def process_qr(text, mapping, map_dir="."):
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
        try:
            value = load_files(value, map_dir)
        except OSError as e:
            print(f"[skip] {text!r}: couldn't read file: {e}")
            return None
        return {"type": "mapped", "raw": text, "key": key, "value": value}

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
    p.add_argument("--map", default="qr_map.json",
                   help="Optional local JSON map (default qr_map.json). Number codes are "
                        "looked up on the server, so this file is not required.")
    p.add_argument("--out", default="FlightPath",
                   help="Folder to save files returned by the server (default FlightPath)")
    p.add_argument("--cooldown", type=float, default=3.0,
                   help="Seconds before the same code is handled again (default 3)")
    args = p.parse_args()

    if os.path.exists(args.map):
        mapping = load_map(args.map)
        print(f"Loaded {len(mapping)} entries from {args.map}")
    else:
        mapping = {}
    map_dir = os.path.dirname(os.path.abspath(args.map))

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
                        payload = process_qr(data, mapping, map_dir)
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