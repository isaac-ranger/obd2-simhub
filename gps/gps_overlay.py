#!/usr/bin/env python3
"""
gps_overlay.py — XGPS160 live feed + browser overlay HTTP server
================================================================

Separate process from the OBD feed. Reads NMEA from the XGPS (or replays a
capture), keeps the latest fix, and serves gps/overlay/ over HTTP so Chrome
or an OBS browser source can show an ego-centered, North-up trail map.

Usage:

  py gps\\gps_overlay.py --list-ports
  py gps\\gps_overlay.py --port COM5
  py gps\\gps_overlay.py --replay xgps160-capture.txt
  py gps\\gps_overlay.py --port COM5 --http-port 8765

Then open http://127.0.0.1:8765/  (optional ?meters=200)

Live serial runs also leave replay-ready raw NMEA in runs/gps-last.txt by
default. Use --run-log full to keep a timestamped run, or off to disable it.

Requires: python 3.9+, pyserial for a COM port (replay is stdlib only).
The live door is gps_capture.open_source — same two-door rule as capture.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from obd_config import parse_with_config
from gps_capture import LineFramer, open_source
from nmea import parse_gga, parse_rmc

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY_DIR = os.path.join(HERE, "overlay")

# Outgoing SPP ports embed the remote MAC; incoming ones use a zero address.
_OUTGOING_MAC = re.compile(
    r"&([0-9A-Fa-f]{12})_C", re.IGNORECASE)
_ZERO_MAC = "000000000000"


def bt_remote_mac(hwid):
    """Remote Bluetooth MAC from a BTHENUM hwid, or None if incoming/unknown."""
    if not hwid or "BTHENUM" not in hwid.upper():
        return None
    m = _OUTGOING_MAC.search(hwid)
    if not m:
        return None
    mac = m.group(1).lower()
    return None if mac == _ZERO_MAC else mac


def bt_device_name(mac):
    """Friendly name from the Windows Bluetooth device registry, or None.

    Best-effort and Windows-only: a missing key, a locked-down hive, or any
    other OS just means --list-ports falls back to the MAC / description.
    """
    if not mac or sys.platform != "win32":
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices\\"
            + mac)
        raw, _ = winreg.QueryValueEx(key, "Name")
        winreg.CloseKey(key)
    except OSError:
        return None
    if isinstance(raw, str):
        return raw.strip("\x00") or None
    try:
        return bytes(raw).split(b"\x00", 1)[0].decode("ascii", "replace") or None
    except (TypeError, ValueError):
        return None


def describe_port(port_info):
    """(device, label) for one list_ports entry — names the BT peer when we can."""
    mac = bt_remote_mac(getattr(port_info, "hwid", "") or "")
    if mac:
        name = bt_device_name(mac) or mac
        return port_info.device, f"outgoing -> {name}"
    hwid = (getattr(port_info, "hwid", "") or "").upper()
    if "BTHENUM" in hwid:
        return port_info.device, "incoming (listen only - will not talk to a GPS)"
    return port_info.device, port_info.description or ""

# Hold last COG / freeze smoothed position when nearly stopped. Below this
# speed GPS course is noise (parking, lights, alley crawl).
HEADING_HOLD_KMH = 2.0
# EMA weight of each new raw fix while moving. Lower = smoother, more lag.
POS_SMOOTH_ALPHA = 0.2


class GpsRunLog:
    """Raw NMEA side-effect log for a live overlay session.

    Uses the same full/tail/off policy as obd_feed's RunLog, but preserves
    gps_capture's timestamp-tab-sentence format so the result goes straight
    back into --replay.
    """

    TAIL_NAME = "gps-last.txt"
    PREV_NAME = "gps-prev.txt"
    # The sample XGPS stream is about 2.2 KiB/s. 50 MiB is roughly 6.7 hours;
    # after a wrap, at least the newest half (about 3.3 hours) remains.
    TAIL_CAP = 50 * 1024 * 1024

    def __init__(self, directory, mode="tail", clock=time.monotonic):
        self.mode = mode
        self.dir = directory
        self.clock = clock
        self.note = ""
        self.f = None
        self.failed = None
        self.t0 = None
        if mode == "tail":
            self.path = os.path.join(directory, self.TAIL_NAME)
        elif mode == "full":
            self.path = os.path.join(
                directory, time.strftime("gps-%Y%m%d-%H%M%S.txt"))
        else:
            self.path = None

    def _open(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            if self.mode == "tail":
                prev = os.path.join(self.dir, self.PREV_NAME)
                try:
                    if os.path.exists(self.path):
                        os.replace(self.path, prev)
                    # w+ lets _wrap retain the newest half in place.
                    self.f = open(self.path, "w+", encoding="utf-8",
                                  newline="")
                except OSError:
                    # A viewer may lock gps-last.txt on Windows. Preserve this
                    # run under a timestamped name rather than losing it.
                    self.mode = "full"
                    self.path = os.path.join(
                        self.dir, time.strftime("gps-%Y%m%d-%H%M%S.txt"))
                    self.note = (f"({self.TAIL_NAME} is locked by another "
                                 f"program — keeping a full log at "
                                 f"{self.path} this run)")
                    print(f"GPS run log: {self.note}", flush=True)
            if self.f is None:
                self.f = open(self.path, "w", encoding="utf-8", newline="")
        except OSError as e:
            self.failed = str(e)
            self.f = None
            print(f"GPS run log unavailable ({e}) — continuing without one",
                  flush=True)

    def line(self, sentence):
        """Record one framed sentence, opening lazily on the first one."""
        if self.mode == "off" or self.failed:
            return
        now = self.clock()
        if self.f is None:
            self._open()
            if self.f is None:
                return
            self.t0 = now
        try:
            self.f.write(f"{now - self.t0:.3f}\t{sentence}\n")
            self.f.flush()
            if self.mode == "tail" and self.f.tell() > self.TAIL_CAP:
                self._wrap()
        except OSError as e:
            self.failed = str(e)
            print(f"GPS run log write failed ({e}) — continuing without one",
                  flush=True)
            self.close()

    def _wrap(self):
        self.f.seek(0)
        newest = self.f.read()
        newest = newest[len(newest) // 2:]
        newline = newest.find("\n")
        newest = newest[newline + 1:] if newline >= 0 else ""
        self.f.seek(0)
        self.f.truncate()
        self.f.write(newest)
        self.f.flush()

    def describe(self):
        if not self.path:
            return "(off)"
        if self.mode == "tail":
            return (f"{self.path}  (tail of this run; previous run kept at "
                    f"{self.PREV_NAME}; --run-log full to keep everything)")
        return self.path + ("  " + self.note if self.note else "")

    def kept(self):
        if self.mode == "off":
            return "GPS run log was off."
        if self.failed:
            return "GPS run log was unavailable this run."
        if self.f is None:
            return ("No GPS sentences arrived, so no run log was written "
                    "(previous log untouched).")
        if self.mode == "tail":
            return (f"GPS log at {self.path} — one more run keeps it as "
                    f"{self.PREV_NAME}, two overwrite it; copy it out to "
                    f"keep it for good.")
        return f"GPS log kept: {self.path}"

    def close(self):
        try:
            if self.f:
                self.f.close()
        except Exception:
            pass


class LiveState:
    """Thread-safe latest fix for /live."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fix = {
            "ok": False,
            "lat": None,
            "lon": None,
            "speed_kmh": 0.0,
            "heading_deg": 0.0,
            "accuracy_m": None,
            "sats": None,
            "t": 0.0,
            "source": "",
        }
        self._heading = 0.0
        self._heading_set = False
        self._lat = None
        self._lon = None
        self._accuracy_m = None
        self._sats = None

    def update_gga(self, gga: dict):
        if not gga:
            return
        with self._lock:
            self._accuracy_m = gga.get("accuracy_m")
            self._sats = gga.get("sats")
            if self._fix["ok"]:
                self._fix["accuracy_m"] = self._accuracy_m
                self._fix["sats"] = self._sats

    def update_rmc(self, fix: dict, source: str = ""):
        if not fix or not fix.get("valid"):
            return
        speed = float(fix["speed_kmh"])
        course = fix.get("course_deg")
        raw_lat = float(fix["lat"])
        raw_lon = float(fix["lon"])
        with self._lock:
            # Seed heading from the first COG we see (even when parked); after
            # that only trust course while actually moving.
            if course is not None and (
                    speed >= HEADING_HOLD_KMH or not self._heading_set):
                self._heading = float(course)
                self._heading_set = True
            # Position: freeze while stopped (reject GPS wander scribble);
            # EMA toward the raw fix while moving.
            if self._lat is None:
                self._lat, self._lon = raw_lat, raw_lon
            elif speed >= HEADING_HOLD_KMH:
                a = POS_SMOOTH_ALPHA
                self._lat = a * raw_lat + (1.0 - a) * self._lat
                self._lon = a * raw_lon + (1.0 - a) * self._lon
            self._fix = {
                "ok": True,
                "lat": self._lat,
                "lon": self._lon,
                "speed_kmh": speed,
                "heading_deg": self._heading,
                "accuracy_m": self._accuracy_m,
                "sats": self._sats,
                "t": time.time(),
                "source": source,
            }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._fix)


def make_handler(state: LiveState, static_dir: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Quiet: OBS and the browser poll /live hard.
            if self.path.startswith("/live"):
                return
            super().log_message(fmt, *args)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/live", "/live.json"):
                body = json.dumps(state.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            # No path escape out of the overlay dir.
            rel = rel.replace("\\", "/")
            if ".." in rel.split("/"):
                self.send_error(400)
                return
            full = os.path.normpath(os.path.join(static_dir, rel))
            if not full.startswith(os.path.normpath(static_dir)):
                self.send_error(400)
                return
            if not os.path.isfile(full):
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            data = open(full, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def reader_serial(src, empty_is_eof, source_name, state: LiveState,
                  stop: threading.Event, run_log: GpsRunLog | None = None):
    """Pump an open_source() handle into LiveState until stop or hang-up."""
    framer = LineFramer()
    try:
        while not stop.is_set():
            chunk = src.read(256)
            if not chunk:
                if empty_is_eof:
                    break
                continue
            for line in framer.feed(chunk):
                if run_log is not None:
                    run_log.line(line)
                fix = parse_rmc(line)
                if fix:
                    state.update_rmc(fix, source=source_name)
                    continue
                gga = parse_gga(line)
                if gga:
                    state.update_gga(gga)
    finally:
        try:
            src.close()
        except Exception:
            pass


def reader_replay(path, state: LiveState, stop: threading.Event, speed=1.0):
    """Replay a gps_capture.txt (t_s\\t sentence) in wall time (speed multiplier)."""
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "\t" not in line:
                continue
            ts, sentence = line.split("\t", 1)
            try:
                rows.append((float(ts), sentence.strip()))
            except ValueError:
                continue
    if not rows:
        print(f"replay: no usable rows in {path}", file=sys.stderr)
        return
    print(f"replay: {len(rows)} lines from {path} at {speed:g}x")
    t0_wall = time.monotonic()
    t0_log = rows[0][0]
    i = 0
    while not stop.is_set():
        # Loop the file so a short capture keeps the overlay alive for testing.
        if i >= len(rows):
            i = 0
            t0_wall = time.monotonic()
            t0_log = rows[0][0]
            time.sleep(0.2)
            continue
        t_log, sentence = rows[i]
        due = t0_wall + (t_log - t0_log) / max(speed, 1e-6)
        delay = due - time.monotonic()
        if delay > 0:
            # Wake often so Ctrl-C / stop is responsive.
            stop.wait(min(delay, 0.05))
            continue
        fix = parse_rmc(sentence)
        if fix:
            state.update_rmc(fix, source=os.path.basename(path))
        else:
            gga = parse_gga(sentence)
            if gga:
                state.update_gga(gga)
        i += 1


def build_parser():
    ap = argparse.ArgumentParser(
        description="GPS live overlay server (XGPS160 -> browser / OBS).")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--port",
                     help="COM port or /dev/cu.* path (outgoing XGPS)")
    src.add_argument("--replay", metavar="CAPTURE.txt",
                     help="replay a gps_capture timestamped log instead of live")
    ap.add_argument("--http-host", default="127.0.0.1",
                    help="HTTP bind address (default 127.0.0.1)")
    ap.add_argument("--http-port", type=int, default=8765,
                    help="HTTP port (default 8765)")
    ap.add_argument("--replay-speed", type=float, default=1.0,
                    help="replay speed multiplier (default 1)")
    ap.add_argument("--log-dir", default="runs",
                    help="run-log directory (default: runs/)")
    ap.add_argument("--run-log", choices=["full", "tail", "off"],
                    default="tail",
                    help="raw NMEA log written during live serial runs: "
                         "'tail' (default) keeps gps-last.txt plus one "
                         "previous run, size-capped; 'full' keeps a "
                         "timestamped file; 'off' writes nothing. Replay "
                         "input is never logged again")
    ap.add_argument("--list-ports", action="store_true",
                    help="list serial ports and exit").per_run = True
    return ap


def main(argv=None):
    args = parse_with_config(build_parser(), "gps_overlay", argv=argv)

    if args.list_ports:
        if list_ports is None:
            sys.exit("COM ports need pyserial: py -m pip install pyserial")
        ports = list(list_ports.comports())
        if not ports:
            print("No serial ports found.")
        for p in ports:
            device, label = describe_port(p)
            print(f"{device:12s} {label}")
        print("\nUse the OUTGOING port whose name is the XGPS.")
        return 0

    if not args.port and not args.replay:
        sys.exit("--port or --replay is required (or --list-ports)")
    if not os.path.isdir(OVERLAY_DIR):
        sys.exit(f"overlay assets missing: {OVERLAY_DIR}")

    if args.port and list_ports is not None:
        for p in list_ports.comports():
            if p.device.upper() == args.port.upper():
                mac = bt_remote_mac(p.hwid or "")
                if mac is None and "BTHENUM" in (p.hwid or "").upper():
                    print(f"note: {args.port} looks like an INCOMING Bluetooth "
                          f"port — prefer the outgoing XGPS port from "
                          f"--list-ports.", file=sys.stderr)
                break

    state = LiveState()
    stop = threading.Event()
    src = None
    run_log = None
    if args.replay:
        worker = threading.Thread(
            target=reader_replay,
            args=(args.replay, state, stop, args.replay_speed),
            daemon=True)
    else:
        src, empty_is_eof = open_source(args.port)
        run_log = GpsRunLog(args.log_dir, args.run_log)
        worker = threading.Thread(
            target=reader_serial,
            args=(src, empty_is_eof, args.port, state, stop, run_log),
            daemon=True)

    handler = make_handler(state, OVERLAY_DIR)
    try:
        httpd = ThreadingHTTPServer((args.http_host, args.http_port), handler)
    except OSError as e:
        stop.set()
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        sys.exit(f"cannot bind http://{args.http_host}:{args.http_port}/: {e}")

    worker.start()

    url = f"http://{args.http_host}:{args.http_port}/"
    print(f"Overlay  -> {url}", flush=True)
    print(f"Live JSON -> {url}live", flush=True)
    print(f"Source   -> "
          + (f"replay {args.replay}" if args.replay else args.port),
          flush=True)
    print("Run log  -> "
          + ("(replay input is not recorded)"
             if run_log is None else run_log.describe()),
          flush=True)
    print("Ctrl-C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        httpd.server_close()
        worker.join(timeout=1.0)
        if run_log is not None:
            print(run_log.kept(), flush=True)
            run_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
