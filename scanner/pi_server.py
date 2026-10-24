#!/usr/bin/env python3
"""
Raspberry Pi receiver for QR scans.

Runs on the Pi. Receives processed scans from qr_scanner.py and appends each
one to scans.csv. Two kinds of scan arrive:

  type "gps":     lat, lon (decimal degrees) and lat_e7, lon_e7 — the integer
                  degE7 values ArduPilot/MAVLink expects (e.g. for
                  MISSION_ITEM_INT x/y or SET_POSITION_TARGET_GLOBAL_INT).
  type "mapped":  key (the scanned number) and value (from your map).

Edit handle_gps() / handle_mapped() to act on the data, e.g. send a waypoint
to the flight controller with pymavlink.

Install:  pip install flask      (or: sudo apt install python3-flask)
Run:      python3 pi_server.py
"""
import csv
import datetime
import json
import os

from flask import Flask, jsonify, request

app = Flask(__name__)
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scans.csv")
FIELDS = ["timestamp", "type", "raw", "lat", "lon", "lat_e7", "lon_e7", "key", "value"]


def log_scan(row):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def handle_gps(lat, lon, lat_e7, lon_e7):
    """Called for every GPS scan. lat_e7/lon_e7 are ready for ArduPilot."""
    print(f"  GPS -> lat_e7={lat_e7}, lon_e7={lon_e7}")


def handle_mapped(key, value):
    """Called for every mapped-number scan."""
    print(f"  map[{key}] -> {value}")


@app.post("/qr")
def receive_qr():
    body = request.get_json(silent=True) or {}
    kind = body.get("type")
    when = datetime.datetime.fromtimestamp(
        body.get("scanned_at") or datetime.datetime.now().timestamp()
    ).isoformat(timespec="seconds")

    if kind == "gps":
        try:
            lat_e7, lon_e7 = int(body["lat_e7"]), int(body["lon_e7"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="gps scan needs integer lat_e7 and lon_e7"), 400
        print(f"[{when}] GPS scan {body.get('raw')!r}")
        handle_gps(body.get("lat"), body.get("lon"), lat_e7, lon_e7)
    elif kind == "mapped":
        if "key" not in body or "value" not in body:
            return jsonify(error="mapped scan needs key and value"), 400
        print(f"[{when}] mapped scan {body.get('raw')!r}")
        handle_mapped(body["key"], body["value"])
    else:
        return jsonify(error="type must be 'gps' or 'mapped'"), 400

    row = {**body, "timestamp": when}
    if not isinstance(row.get("value"), (str, int, float, type(None))):
        row["value"] = json.dumps(row["value"])  # store lists/objects as JSON text
    log_scan(row)
    return jsonify(status="ok")


@app.get("/scans")
def list_scans():
    """Open http://<pi-ip>:5000/scans in a browser to see everything received."""
    if not os.path.exists(LOG_FILE):
        return jsonify([])
    with open(LOG_FILE, newline="") as f:
        return jsonify(list(csv.DictReader(f)))


if __name__ == "__main__":
    # 0.0.0.0 makes it reachable from other devices on your network
    app.run(host="0.0.0.0", port=5000)
