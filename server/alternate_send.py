"""
Server for the Raspberry Pi QR scanner (raspberry_pi/alternate_read.py).

  GET  /ask?key=<n>  looks n up in scanner/qr_map.json and returns the entry,
                     with {"file": path} entries replaced by that file's bytes
                     (base64), so the Pi can save them.
  POST /ads_server   receives GPS scans from the Pi.
  GET  /scans        shows everything logged in scans.csv.

Install:  pip install flask
Run:      python3 alternate_send.py
"""
import base64
import csv
import datetime
import json
import os
import re

from flask import Flask, jsonify, request

app = Flask(__name__)
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SERVER_DIR, "scans.csv")
MAP_FILE = os.path.join(SERVER_DIR, "scanner", "qr_map.json")
MAP_DIR = os.path.dirname(MAP_FILE)  # {"file": path} entries are relative to this
FIELDS = ["timestamp", "type", "raw", "lat", "lon", "lat_e7", "lon_e7", "key", "value"]

NUMBER_PATTERN = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*$")

def load_map(path):
    with open(path) as f:
        m = json.load(f)
    if not isinstance(m, dict):
        raise SystemExit(f"{path} must contain a JSON object like {{\"1\": \"value\"}}")
    # JSON keys are always strings; keep them that way for lookup
    return {str(k).strip(): v for k, v in m.items()}


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


mapping = load_map(MAP_FILE)

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

def log_scan(row):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def summarize(value):
    """Copy of value for printing/logging, with file contents shown as their size."""
    if isinstance(value, dict):
        if set(value) == {"b64"} and isinstance(value["b64"], str):
            b64 = value["b64"]
            return f"<file, {len(b64) * 3 // 4 - b64[-2:].count('=')} bytes>"
        return {k: summarize(v) for k, v in value.items()}
    return value

def handle_gps(lat, lon, lat_e7, lon_e7):
    """Called for every GPS scan. lat_e7/lon_e7 are ready for ArduPilot."""
    print(f"  GPS -> lat_e7={lat_e7}, lon_e7={lon_e7}")


@app.post("/ads_server")
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
    else:
        # Number codes go to GET /ask instead, so only GPS scans arrive here
        return jsonify(error="type must be 'gps'"), 400

    log_scan({**body, "timestamp": when})
    return jsonify(status="ok")


@app.get("/ask")
def receive_ask():
    """Respond to the Pi's GET /ask request with the file asked for based on the mapping in map.json."""
    key = request.args.get("key", "").strip()
    if not key:
        return jsonify(error="missing key"), 400
    if not NUMBER_PATTERN.match(key):
        return jsonify(error=f"key {key!r} is not a number"), 400

    matched, value = lookup_number(key, mapping)
    if matched is None:
        print(f"[ask] key {key!r} is not in the map")
        return jsonify(error=f"key {key!r} is not in the map"), 404

    try:
        value = load_files(value, MAP_DIR)
    except OSError as e:
        print(f"[ask] key {key!r}: couldn't read file: {e}")
        return jsonify(error=f"couldn't read file for key {key!r}: {e}"), 500

    print(f"[ask] map[{matched}] -> {summarize(value)}")
    log_scan({"timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
              "type": "ask", "raw": key, "key": matched,
              "value": json.dumps(summarize(value))})
    return jsonify(type="mapped", raw=key, key=matched, value=value)


@app.get("/scans")
def list_scans():
    """Open http://<pi-ip>:5000/scans in a browser to see everything received."""
    if not os.path.exists(LOG_FILE):
        return jsonify([])
    with open(LOG_FILE, newline="") as f:
        return jsonify(list(csv.DictReader(f)))


if __name__ == "__main__":
    # 0.0.0.0 makes it reachable from other devices on your network
    print(f"Loaded {len(mapping)} entries from {MAP_FILE}")
    app.run(host="0.0.0.0", port=5000)


