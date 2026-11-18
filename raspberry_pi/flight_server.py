#!/usr/bin/env python3
"""
Flight server: task-based concurrent synchronous state machines (synchSMs).

Each task is a small state machine with its own period. A single scheduler
loop wakes up every BASE_PERIOD seconds and ticks every task whose period has
elapsed. Tasks communicate only through shared variables (the `shared` dict),
so there are no threads or locks in the control logic.

Rules for tick functions:
  - Do one step of work and return quickly. Never block (no sleep, no
    waiting on the network or the camera). If something isn't ready yet,
    stay in the same state and check again on the next tick.
  - Blocking I/O (Flask server, camera, sockets) belongs in its own thread
    or process that hands data over through a queue.Queue, which a task
    polls with get_nowait().

Scanning: while the flight task is in IDLE, it runs the scanner chosen by
SCAN_SOURCE as a child process and reads the results the server confirms
(see ScannerProcess). When a GPS waypoint comes in, the scanner is stopped
and the flight starts.

Run:  python3 flight_server.py
"""
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from functools import reduce
from typing import Callable


# ---------------------------------------------------------------------------
# Scanner configuration
# ---------------------------------------------------------------------------
# "alternate": run alternate_read.py on the Pi. The Pi's camera scans QR codes
#              and sends them to the server at SERVER_IP:SERVER_PORT
#              (server/alternate_send.py).
# "offgrid":   run pi_qr_read_server.py on the Pi. It listens on SERVER_PORT
#              and a separate scanner (e.g. a laptop) sends scans to it.


SCAN_SOURCE = "alternate"
SERVER_IP = "192.168.1.50"      # only used by "alternate"
SERVER_PORT = 5000
SCANNER_RESTART_DELAY = 5.0     # seconds to wait before relaunching a scanner that exited

PI_DIR = os.path.dirname(os.path.abspath(__file__))
EVENT_PREFIX = "FLIGHT_EVENT "  # scanner output lines starting with this carry JSON results


# ---------------------------------------------------------------------------
# Shared variables (the only way tasks talk to each other)
# ---------------------------------------------------------------------------
shared = {
    "waypoint": None,        # (lat_e7, lon_e7) waiting to be flown to
    "armed": False,
    "mission_done": False,
    "home" : None,            # (lat_e7, lon_e7) of home position
}

# Filled by the scanner's output reader thread, drained by the scan task.
scan_queue: "queue.Queue[dict]" = queue.Queue()


# ---------------------------------------------------------------------------
# Scanner process (runs alternate_read.py or pi_qr_read_server.py)
# ---------------------------------------------------------------------------
def scanner_command():
    if SCAN_SOURCE == "alternate":
        script, args = "alternate_read.py", ["--ip", SERVER_IP, "--port", str(SERVER_PORT)]
    elif SCAN_SOURCE == "offgrid":
        script, args = "pi_qr_read_server.py", ["--port", str(SERVER_PORT)]
    else:
        raise ValueError(f"SCAN_SOURCE must be 'alternate' or 'offgrid', not {SCAN_SOURCE!r}")
    # -u: unbuffered output, so results arrive as soon as they're printed
    return [sys.executable, "-u", os.path.join(PI_DIR, script), *args]


class ScannerProcess:
    """Starts/stops the scanner without blocking the tick functions.

    A background thread reads the scanner's output. Lines starting with
    EVENT_PREFIX are JSON results confirmed by the server and go onto
    scan_queue; everything else is echoed to this terminal.
    """

    def __init__(self):
        self.proc = None
        self.last_start = -math.inf

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure_running(self):
        """Launch the scanner if it isn't running (rate-limited so a crash doesn't spin)."""
        if self.running():
            return
        if self.proc is not None:
            print(f"[flight] scanner exited with code {self.proc.returncode}")
            self.proc = None
        if time.monotonic() - self.last_start < SCANNER_RESTART_DELAY:
            return
        self.last_start = time.monotonic()
        cmd = scanner_command()
        print(f"[flight] launching scanner: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read_output, args=(self.proc,), daemon=True).start()

    def stop(self):
        """Ask the scanner to quit. Waiting for it happens in a background thread."""
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        print("[flight] stopping scanner")
        proc.terminate()
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()

    @staticmethod
    def _reap(proc):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    @staticmethod
    def _read_output(proc):
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith(EVENT_PREFIX):
                try:
                    scan_queue.put(json.loads(line[len(EVENT_PREFIX):]))
                except json.JSONDecodeError:
                    print(f"[flight] bad event from scanner: {line!r}")
            else:
                print(f"[scanner] {line}")


scanner = ScannerProcess()


# ---------------------------------------------------------------------------
# Task framework
# ---------------------------------------------------------------------------
@dataclass
class Task:
    name: str
    period: float                       # seconds between ticks
    state: Enum                         # current state
    tick: Callable[[Enum], Enum]        # tick(state) -> next state
    elapsed: float = 0.0


# ---------------------------------------------------------------------------
# Task 1: Scan handling
# ---------------------------------------------------------------------------
class ScanState(Enum):
    START = auto()
    WAIT = auto()
    PROCESS = auto()


def scan_tick(state):
    # --- transitions ---
    if state == ScanState.START:
        state = ScanState.WAIT
    elif state == ScanState.WAIT:
        if not scan_queue.empty():
            state = ScanState.PROCESS
    elif state == ScanState.PROCESS:
        state = ScanState.WAIT
    else:
        state = ScanState.START

    # --- actions ---
    if state == ScanState.PROCESS:
        try:
            scan = scan_queue.get_nowait()
        except queue.Empty:
            return state
        if scan.get("type") == "gps":
            try:
                shared["waypoint"] = (int(scan["lat_e7"]), int(scan["lon_e7"]))
                print(f"[flight] waypoint received: {shared['waypoint']}")
            except (KeyError, TypeError, ValueError):
                print(f"[flight] ignoring malformed GPS scan: {scan}")
        elif scan.get("type") == "mapped":
            print(f"[flight] mapped scan received: {scan}")  # TODO: act on files/values

    return state


# ---------------------------------------------------------------------------
# Task 2: Flight control
# ---------------------------------------------------------------------------
class FlightState(Enum):
    START = auto()
    IDLE = auto()
    ARMING = auto()
    TAKEOFF = auto()
    NAVIGATE = auto()
    LAND = auto()


def flight_tick(state):
    # --- transitions ---
    if state == FlightState.START:
        state = FlightState.IDLE
    elif state == FlightState.IDLE:
        if shared["waypoint"] is not None:
            scanner.stop()
            state = FlightState.ARMING
    elif state == FlightState.ARMING:
        if shared["armed"]:
            state = FlightState.TAKEOFF
    elif state == FlightState.TAKEOFF:
        # TODO: check altitude reached
        state = FlightState.NAVIGATE
    elif state == FlightState.NAVIGATE:
        # TODO: check waypoint reached
        state = FlightState.LAND
    elif state == FlightState.LAND:
        # TODO: check landed
        shared["mission_done"] = True
        shared["waypoint"] = None   # clear so IDLE waits for a new scan
        shared["armed"] = False
        state = FlightState.IDLE
    else:
        state = FlightState.START

    # --- actions ---
    if state == FlightState.IDLE:
        scanner.ensure_running()  # launches on entry, relaunches if it exits
    elif state == FlightState.ARMING:
        pass  # TODO: send arm command (non-blocking), set shared["armed"] on ack
    elif state == FlightState.TAKEOFF:
        pass  # TODO: send takeoff command
    elif state == FlightState.NAVIGATE:
        pass  # TODO: send shared["waypoint"] to the flight controller
    elif state == FlightState.LAND:
        pass  # TODO: send land command

    return state


# ---------------------------------------------------------------------------
# Task 3: Telemetry scan
# ---------------------------------------------------------------------------
class TelemetryScanState(Enum):
    START = auto()
    SCAN = auto()


def telemetryScan_tick(state):
    # --- transitions ---
    if state == TelemetryScanState.START:
        state = TelemetryScanState.SCAN
    elif state == TelemetryScanState.SCAN:
        state = TelemetryScanState.SCAN
    else:
        state = TelemetryScanState.START

    # --- actions ---
    if state == TelemetryScanState.SCAN:
        pass  # TODO: read telemetry

    return state


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
TASKS = [
    Task("scan",      period=0.05, state=ScanState.START,      tick=scan_tick),
    Task("flight",    period=0.10, state=FlightState.START,    tick=flight_tick),
    Task("telemetryScan", period=1.00, state=TelemetryScanState.START, tick=telemetryScan_tick),
]


def gcd_period(tasks):
    """Base period = GCD of all task periods (in ms to avoid float issues)."""
    ms = [round(t.period * 1000) for t in tasks]
    return reduce(math.gcd, ms) / 1000


BASE_PERIOD = gcd_period(TASKS)


def run():
    next_tick = time.monotonic()
    while True:
        for task in TASKS:
            task.elapsed += BASE_PERIOD
            if task.elapsed >= task.period - 1e-9:
                task.state = task.tick(task.state)
                task.elapsed = 0.0

        # Fixed-rate timing: schedule from the previous deadline, not from
        # "now", so the loop doesn't drift.
        next_tick += BASE_PERIOD
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # Overran the period: tick functions took too long.
            next_tick = time.monotonic()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopping flight server.")
    except Exception as e:
        print(f"\nFlight server crashed: {e!r}")
        raise
    finally:
        scanner.stop()
        print(f"Final shared state: {shared}")
        print(f"Final task states: {[t.state for t in TASKS]}")
