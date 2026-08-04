#!/usr/bin/env python3
"""
obd_feed.py — phase 2 extractor: the car -> SimHub UDP telemetry feed
=====================================================================

Polls the car over the OBD adapter (same serial stack as the phase 1 probe),
keeps a live vehicle state, and streams it to SimHub's External Sim
Integration as a 60 Hz binary UDP feed. The car answers at ~5 Hz; the send
loop runs at 60 Hz off last-known values with interpolation on RPM and speed
only — a needle should sweep, a coolant gauge should not invent data.

Usage (Windows, OBDLink MX+ over Bluetooth):
  py extractor\\obd_feed.py --port COM3

Usage (no car needed — replay a recorded drive through the whole pipeline):
  py extractor\\obd_feed.py --replay reports\\2026-07-31-kris-drive_01.csv

Usage (no car, no log — against the fake car in another terminal):
  python extractor/fake_car.py --tcp 35000
  python extractor/obd_feed.py --port socket://127.0.0.1:35000

The wire contract lives in extractor/feed_layout.json. Until the constants
from SimHub's definition editor ("copy demo code") are transcribed into it,
the feed speaks a provisional dialect only our own tools understand — the
plumbing is real, the accent is a placeholder.

Requires: python 3.9+, pyserial for --port mode (replay mode is stdlib only).
Safety: read-only, Mode 01 data requests and ELM config commands, nothing
else. Never writes to the ECU, never clears codes.
"""

import argparse
import csv
import json
import math
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "probe"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir))
from obd_probe import (Elm, ElmError, parse_mode01, adapter_init,
                       get_supported_pids, DASH_PIDS)
from obd_config import parse_with_config

# The three channels a sim overlay lives or dies by. Pinned into every
# request; everything else rotates through the remaining slots.
FAST_PIDS = [0x0C, 0x0D, 0x11]          # RPM, speed, throttle
SLOW_ORDER = [0x04, 0x05, 0x5C, 0x2F, 0x33, 0x42, 0x0E, 0x0F, 0x0B, 0x10, 0x5E]
BATCH_SIZE = 6                           # one CAN message holds six PIDs
SEND_HZ = 60.0                           # SimHub wants >= 60 Hz
MIN_RPM = 900.0                          # below this: idling/off, no gear math
MIN_SPEED = 8.0                          # km/h; below this the ratio is noise

C_TYPES = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
           "u32": "I", "i32": "i", "u64": "Q", "i64": "q",
           "f32": "f", "f64": "d", "str8": "8s"}

KMH_PER_MPH = 1.609344


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        sys.exit(f"cannot read {what} at {path}: {e}")
    except json.JSONDecodeError as e:
        sys.exit(f"{what} at {path} is not valid JSON: {e}")


def repo_path(*parts):
    """Default config locations relative to this file, so cwd never matters."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, *parts)


# --------------------------------------------------------------------------
# Poll scheduling: every request is FAST_PIDS + a rotating window over the
# slow channels. Batching is free (measured, phase 1), so a request never
# carries fewer than six PIDs when six are available.
# --------------------------------------------------------------------------

class Scheduler:
    def __init__(self, supported):
        self.fast = [p for p in FAST_PIDS if p in supported]
        self.slow = [p for p in SLOW_ORDER if p in supported]
        self.pos = 0
        if not self.fast:
            raise ElmError("car offers none of RPM/speed/throttle — "
                           "there is no dashboard to feed")

    def next_pids(self):
        take = min(BATCH_SIZE - len(self.fast), len(self.slow))
        if take <= 0:
            return list(self.fast)
        picked = [self.slow[(self.pos + i) % len(self.slow)] for i in range(take)]
        self.pos = (self.pos + take) % len(self.slow)
        return self.fast + picked

    def slow_refresh_s(self, poll_hz):
        """How stale can a slow channel get, worst case?"""
        if not self.slow or poll_hz <= 0:
            return 0.0
        take = max(1, BATCH_SIZE - len(self.fast))
        return math.ceil(len(self.slow) / take) / poll_hz


# --------------------------------------------------------------------------
# Gear inference. The six constants come from calibration.json, learned from
# a real drive log (probe/learn_gears.py). The ratio uses RAW OBD speed —
# the constants were learned from raw OBD speed, and both rpm and OBD speed
# live upstream of tire size, so this math never sees a wheel swap.
# --------------------------------------------------------------------------

class GearWatch:
    """Snap rpm/speed ratio to the nearest learned constant, with two guards
    proven against Kris's real drive log:

    * steady gate — a candidate gear only accumulates dwell while the RAW
      ratio is changing slower than STEADY_PCT_PER_S. A rev-matched
      downshift sweeps the ratio through other gears' bands, but a sweep is
      never slow, so it never confirms.
    * symmetric dwell — the readout changes (in OR out of gear) only after
      DWELL_S seconds (and >= 2 samples) of agreement. Integer-quantized
      speed at low km/h jitters single samples out of band; one outlier is
      not a shift.

    Both guards are TIME-based, deliberately: this same change removed the
    read-loop quantum that pinned polling near 4.7 Hz, so the live rate is
    now unknown and possibly 5-10x higher. Per-sample guards would have
    silently rescaled with it (QA reproduced phantom 4-5-6 cascades during
    braking at >= 15 Hz simulated). Time doesn't rescale.

    Engine off (Auto-Stop) drops the readout immediately — that one is not
    a debate. 0 means neutral / shifting / unknowable."""

    ENGINE_OFF_RPM = 400.0
    STEADY_PCT_PER_S = 14.0    # the learner's 3%-per-sample at the log's 4.7 Hz
    DWELL_S = 0.35             # ~2 samples at the log rate
    MIN_DT = 0.18              # evaluate at most ~5.5 Hz. OBD speed is an
                               # integer: over 0.2s, rpm growth cancels the
                               # 1 km/h step and the ratio is meaningful; at
                               # kHz poll rates the same step is an instant
                               # cliff and NOTHING ever reads steady. Gear
                               # judgment gets the cadence it was validated
                               # at, however fast the poll loop gets.

    def __init__(self, constants, tolerance_pct, dwell_s=DWELL_S):
        self.constants = list(constants)        # index 0 = 1st gear
        self.tol = tolerance_pct / 100.0
        self.dwell_s = dwell_s
        self.shown = 0
        self._candidate = 0
        self._count = 0
        self._since = None
        self._prev_ratio = None
        self._prev_t = None

    def _band(self, ratio):
        best, best_err = 0, None
        for i, c in enumerate(self.constants):
            err = abs(ratio - c) / c
            if best_err is None or err < best_err:
                best, best_err = i + 1, err
        return best if best_err is not None and best_err <= self.tol else 0

    def feed(self, rpm, speed_kmh, t):
        if rpm < self.ENGINE_OFF_RPM:
            self.shown = self._candidate = 0
            self._count = 0
            self._since = None
            self._prev_ratio = self._prev_t = None
            return 0
        if self._prev_t is not None and t - self._prev_t < self.MIN_DT:
            return self.shown              # decimate: too soon to judge
        if rpm < MIN_RPM or speed_kmh < MIN_SPEED:
            ratio, g = None, 0
        else:
            ratio = rpm / speed_kmh
            g = self._band(ratio)
        steady = False
        if (ratio is not None and self._prev_ratio is not None
                and self._prev_t is not None and t > self._prev_t):
            rate = (abs(ratio - self._prev_ratio) / self._prev_ratio
                    / (t - self._prev_t) * 100.0)
            steady = rate < self.STEADY_PCT_PER_S
        self._prev_ratio, self._prev_t = ratio, t
        if g == self._candidate:
            # neutral has no ratio to hold steady; a gear claim does
            if g == 0 or steady:
                self._count += 1
            else:
                self._count, self._since = 1, t   # still sweeping: new clock
        else:
            self._candidate, self._count, self._since = g, 1, t
        if (self._count >= 2 and self._since is not None
                and t - self._since >= self.dwell_s):
            self.shown = self._candidate
        return self.shown


class GearDisplay:
    """What the dashboard shows, as opposed to what the judge knows.

    GearWatch is honest: the moment the clutch breaks the ratio it reads 0,
    and the run log records that truth (57% of Kris's drive_02 samples are
    honest N — 1:36 of it standing still, 57s of it clutch-in coasting).
    Correct in the log, broken-looking on a stream overlay: viewers see the
    gear widget flash N through every shift and coast.

    So the dash gets a held view: keep showing the last engaged gear while
    the car is still rolling, N when actually standing or the engine is
    off. The hold is display-only — the log and the judge never see it.

    Known soft spot, accepted: OBD speed is unsigned, so backing up at
    walking pace can wear the last forward gear for a moment (below
    STAND_KMH it clears). A learned-ratio gearbox cannot see reverse at
    all — that line is already in the layout contract."""

    STAND_KMH = 3.0     # at or below: actually stopped, show N

    def __init__(self):
        self.held = 0

    def feed(self, honest, rpm, speed_kmh):
        if rpm < GearWatch.ENGINE_OFF_RPM:
            self.held = 0                  # engine off is not a debate here
        elif honest > 0:
            self.held = honest             # the judge is sure: follow it
        elif speed_kmh <= self.STAND_KMH:
            self.held = 0                  # standing in neutral: honest N
        # else: rolling with the clutch in — keep wearing the last gear
        return self.held


# --------------------------------------------------------------------------
# Live vehicle state, shared between the poll loop (writer) and the UDP
# sender (reader). RPM and speed keep their previous sample too, so the
# sender can interpolate; everything else is last-known-value.
# --------------------------------------------------------------------------

class CarState:
    INTERP = (0x0C, 0x0D)
    STALE_S = 2.0     # newest sample older than this = the car left the call

    def __init__(self, gear_watch, speed_factor=1.0, display_hold=True):
        self.lock = threading.Lock()
        self.values = {}                 # pid -> (value, t)
        self.prev = {}                   # pid -> (value, t) one sample older
        self.gear_watch = gear_watch
        self.speed_factor = speed_factor
        self.gear = 0
        self.display_hold = display_hold
        self.gear_display = 0
        self._display = GearDisplay()
        self.poll_period = 0.25          # EMA of sample spacing, seeded at 4 Hz
        self._last_t = None

    def update(self, decoded, t, gear_t=None):
        """decoded: {pid: float}. Called once per poll sample. t is wall
        (monotonic) time and drives interpolation/staleness; gear_t, when
        given, is DATA time for the gear judge — replay at --speed N
        compresses wall time N-fold, and gear dwell measured against a
        compressed clock would judge a different drive than the one
        recorded."""
        with self.lock:
            for pid, val in decoded.items():
                if pid in self.INTERP and pid in self.values:
                    self.prev[pid] = self.values[pid]
                self.values[pid] = (val, t)
            rpm = self.values.get(0x0C, (0.0, t))[0]
            spd = self.values.get(0x0D, (0.0, t))[0]
            self.gear = self.gear_watch.feed(
                rpm, spd, t if gear_t is None else gear_t)
            self.gear_display = (self._display.feed(self.gear, rpm, spd)
                                 if self.display_hold else self.gear)
            if self._last_t is not None:
                dt = t - self._last_t
                if 0.0 < dt < 2.0:
                    self.poll_period += 0.2 * (dt - self.poll_period)
            self._last_t = t

    def _interp(self, pid, render_t):
        cur = self.values.get(pid)
        if cur is None:
            return 0.0
        v1, t1 = cur
        old = self.prev.get(pid)
        if old is None or render_t >= t1:
            return v1
        v0, t0 = old
        if render_t <= t0 or t1 <= t0:
            return v0
        f = (render_t - t0) / (t1 - t0)
        return v0 + (v1 - v0) * f

    def snapshot(self, now):
        """Values for one outgoing packet. RPM/speed are rendered one poll
        period in the past and interpolated between the two samples that
        bracket that moment — a needle that sweeps, at the price of ~200 ms
        latency the Bluetooth link already made us pay anyway. Extrapolating
        into the future instead would overshoot every shift and redline.

        A snapshot older than STALE_S reports honestly empty instead: no
        pids, no gear, zero speed. Otherwise a car that stopped answering
        (key off, adapter nap) would broadcast a frozen 'engine running'
        dashboard for as long as the poll loop kept hoping."""
        with self.lock:
            if self._last_t is not None and now - self._last_t > self.STALE_S:
                return {"pids": {}, "gear": 0, "gear_display": 0,
                        "true_speed_kmh": 0.0}
            render_t = now - self.poll_period
            rpm = self._interp(0x0C, render_t)
            speed_raw = self._interp(0x0D, render_t)
            out = {pid: v for pid, (v, _t) in self.values.items()}
            gear, gear_display = self.gear, self.gear_display
        out[0x0C] = rpm
        out[0x0D] = speed_raw
        return {
            "pids": out,
            "gear": gear,
            "gear_display": gear_display,
            "true_speed_kmh": speed_raw * self.speed_factor,
        }


# --------------------------------------------------------------------------
# The wire format, driven entirely by feed_layout.json — which is the SimHub
# generator's struct transcribed (see extractor/demo_code_01.cs). Pack=1,
# little-endian, 101 bytes, and the receiving end checks the signatures.
# --------------------------------------------------------------------------

class LayoutError(Exception):
    pass


class Packer:
    RUNTIME_KEYS = ("emitter_id", "session_id", "counter", "session_time",
                    "session_running", "is_replay")

    def __init__(self, layout, cal=None):
        self.endian = "<" if layout.get("endian", "little") == "little" else ">"
        self.header = layout.get("header", [])
        self.fields = layout.get("fields", [])
        self.cal = cal or {}
        fmt = self.endian
        for item in self.header + self.fields:
            ctype = item.get("type")
            if ctype not in C_TYPES:
                sys.exit(f"feed_layout: unknown type {ctype!r} on "
                         f"{item.get('name', '?')!r}")
            fmt += C_TYPES[ctype]
        self.fmt = fmt
        self.size = struct.calcsize(fmt)
        expected = layout.get("expected_packet_length")
        if expected and self.size != expected:
            sys.exit(f"feed_layout: struct packs to {self.size} bytes but "
                     f"the contract says {expected} — a field is missing, "
                     f"extra, or mistyped. SimHub would bin every packet.")
        # Fail fast, in THIS thread: resolve every source once against dummy
        # data. A source typo must die loudly here, not silently kill the
        # sender daemon later while the console keeps saying 'udp 0 pkts'.
        try:
            self.pack({"pids": {}, "gear": 0, "true_speed_kmh": 0.0},
                      {k: 0 for k in self.RUNTIME_KEYS})
        except (LayoutError, KeyError) as e:
            sys.exit(f"feed_layout: {e}")

    def _value(self, item, snap, runtime):
        if "value" in item:
            return item["value"]
        src = item.get("source", "")
        if src.startswith("runtime:"):
            key = src[8:]
            if key not in runtime:
                raise LayoutError(f"unknown runtime source {key!r} on "
                                  f"{item.get('name', '?')!r}")
            return runtime[key]
        if src.startswith("cal:"):
            key = src[4:]
            if key not in self.cal:
                raise LayoutError(
                    f"wants calibration value {key!r} — add it to "
                    "calibration.json (engine section)")
            return self.cal[key]
        if src.startswith("pid:"):
            return float(snap["pids"].get(int(src[4:], 16), 0.0))
        if src == "derived:true_speed_kmh":
            return float(snap["true_speed_kmh"])
        if src == "derived:gear_text":
            return gear_char(snap.get("gear_display", snap["gear"]))
        if src == "derived:throttle_01":
            return float(snap["pids"].get(0x11, 0.0)) / 100.0
        if src == "derived:ignition_on":
            return 1 if snap["pids"] else 0
        if src == "derived:engine_started":
            return 1 if snap["pids"].get(0x0C, 0.0) >= \
                GearWatch.ENGINE_OFF_RPM else 0
        if src == "derived:fuel_liters":
            pct = float(snap["pids"].get(0x2F, 0.0))
            return pct / 100.0 * float(self.cal.get("fuel_tank_l", 0.0))
        raise LayoutError(f"unknown source {src!r} on "
                          f"{item.get('name', '?')!r}")

    def pack(self, snap, runtime):
        vals = []
        for item in self.header + self.fields:
            v = self._value(item, snap, runtime)
            kind = C_TYPES[item["type"]]
            if kind == "8s":
                vals.append(str(v).encode("utf-8")[:7] + b"\x00")
            elif kind in "fd":
                vals.append(float(v))
            else:
                vals.append(int(v))
        return struct.pack(self.fmt, *vals)

    def unpack(self, blob):
        """Inverse of pack, for tests and the receiver tool: {name: value}.
        str8 fields come back as str with the NUL padding stripped."""
        vals = struct.unpack(self.fmt, blob)
        out = {}
        for item, v in zip(self.header + self.fields, vals):
            if isinstance(v, bytes):
                v = v.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            out[item.get("name", "?")] = v
        return out


def _random_u64():
    """Non-zero random 64-bit id (SimHub: 'should be non-zero when possible')."""
    return int.from_bytes(os.urandom(8), "little") | 1


class Sender(threading.Thread):
    """Fixed-rate UDP send loop, decoupled from the poll loop entirely.
    Owns the SimHub session bookkeeping: emitter/session ids minted once,
    a packet counter that never resets, session time in seconds."""

    def __init__(self, state, packer, host, port, hz=SEND_HZ,
                 is_replay=False):
        super().__init__(daemon=True)
        self.state, self.packer = state, packer
        self.addr = (host, port)
        self.period = 1.0 / hz
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.stop = threading.Event()
        self.sent = 0
        self.errors = 0
        self.fatal = None
        self.runtime = {
            "emitter_id": _random_u64(),
            "session_id": _random_u64(),
            "counter": 0,
            "session_time": 0.0,
            "session_running": 1,
            "is_replay": 1 if is_replay else 0,
        }

    def run(self):
        t0 = time.monotonic()
        next_t = t0
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                self.runtime["counter"] += 1
                self.runtime["session_time"] = now - t0
                try:
                    self.sock.sendto(
                        self.packer.pack(self.state.snapshot(now),
                                         self.runtime),
                        self.addr)
                    self.sent += 1
                except OSError:
                    self.errors += 1
                next_t += self.period
                delay = next_t - time.monotonic()
                if delay > 0:
                    # Sleep most of it, spin the last 2 ms. Windows' 15.6 ms
                    # sleep quantum (Python <= 3.10) would otherwise round
                    # every 16.7 ms nap up and underpace the feed to ~40 Hz.
                    if delay > 0.002:
                        time.sleep(delay - 0.002)
                    while (time.monotonic() < next_t
                           and not self.stop.is_set()):
                        pass
                else:
                    next_t = time.monotonic()   # fell behind; don't burst
        except Exception as e:   # pragma: no cover — belt for edited layouts
            self.fatal = e
            print(f"\nUDP sender died: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Display + run log
# --------------------------------------------------------------------------

def display_units(calibration, override=None):
    mode = override or calibration.get("units", {}).get("display", "metric")
    if mode not in ("metric", "imperial"):
        sys.exit(f"units must be 'metric' or 'imperial', not {mode!r}")
    return mode


def fmt_speed(kmh, units):
    return f"{kmh / KMH_PER_MPH:4.0f} mph" if units == "imperial" \
        else f"{kmh:4.0f} km/h"


def fmt_temp(c, units):
    return f"{c * 9 / 5 + 32:.0f}F" if units == "imperial" else f"{c:.0f}C"


GEAR_CHARS = {0: "N"}


def gear_char(g):
    return GEAR_CHARS.get(g, str(g))


def gear_status(snap):
    """Console form: honest gear, plus what the dash is wearing when the
    display hold has them disagreeing — 'N (dash 2)' during a coast."""
    honest = gear_char(snap["gear"])
    dash = gear_char(snap.get("gear_display", snap["gear"]))
    return honest if dash == honest else f"{honest} (dash {dash})"


class RunLog:
    """Every run logs its samples to CSV as a side effect — free drive data.
    Same format as probe --log plus whatever slow channels each sample has.

    Three sizes (--run-log). 'full' is the original: a timestamped file
    per run, kept forever — development, and any drive you mean to keep.
    'tail' is the daily driver: ONE file, feed-last.csv, overwritten at
    every start and size-capped, so "it did something strange an hour in"
    stays diagnosable while nothing ever accumulates in runs/. If a run
    was worth keeping, copy the file — the next start eats it. 'off' is
    off: overlays are the product, some rigs want no side effects at all.
    """

    COLS = [(0x0C, "rpm"), (0x0D, "speed_kmh"), (0x11, "throttle_pct"),
            (0x04, "load_pct"), (0x05, "coolant_c"), (0x5C, "oil_c"),
            (0x2F, "fuel_pct"), (0x33, "baro_kpa"), (0x42, "voltage_v"),
            (0x0E, "timing_deg")]

    TAIL_NAME = "feed-last.csv"
    PREV_NAME = "feed-prev.csv"
    # ~6.5 hours of samples at real-car rates (drive_02 measured: 4.78 Hz,
    # ~45 B/row with the rotating channels partly empty), floor ~3.2h right
    # after a wrap. When the cap trips, the OLDEST half goes and the newest
    # half stays. A plain truncate would be simpler and wrong: it drops
    # everything at the boundary, including the row that tripped it, and
    # the moment just before a wrap is a moment like any other — the funny
    # thing is allowed to happen there too.
    TAIL_CAP = 5 * 1024 * 1024

    def __init__(self, directory, mode="full"):
        self.mode = mode
        self.dir = directory
        self.note = ""
        self.f = None
        self.failed = None
        # Nothing is created, rotated, or truncated until the first sample
        # actually lands (_open, from row). A run that never hears the car
        # — wrong port, adapter unplugged, a supervisor crash-loop retrying
        # every few seconds — must not cost you the log of the run that
        # did. The QA pass caught the old eager open doing exactly that:
        # truncating the evidence of a failure two seconds after the
        # supervisor restarted the feed to recover from it.
        if mode == "tail":
            self.path = os.path.join(directory, self.TAIL_NAME)
        elif mode == "full":
            self.path = os.path.join(directory,
                                     time.strftime("feed-%Y%m%d-%H%M%S.csv"))
        else:
            self.path = None

    def _open(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            if self.mode == "tail":
                # One generation back instead of gone: the previous run
                # survives as feed-prev.csv until the NEXT data-bearing
                # run starts. Bounded at two files, nothing accumulates.
                # Rotation and open share the fallback: a locked file
                # refuses either one, and both deserve the same answer.
                prev = os.path.join(self.dir, self.PREV_NAME)
                try:
                    if os.path.exists(self.path):
                        os.replace(self.path, prev)
                    # w+ because the wrap below reads back what it keeps
                    self.f = open(self.path, "w+", encoding="ascii",
                                  newline="")
                except OSError:
                    # Windows: a viewer holding feed-last.csv (Excel does)
                    # locks it. Losing the run over a spreadsheet is the
                    # wrong trade at an event — fall back to a timestamped
                    # file and say so.
                    self.mode = "full"
                    self.path = os.path.join(
                        self.dir, time.strftime("feed-%Y%m%d-%H%M%S.csv"))
                    self.note = (f"({self.TAIL_NAME} is locked by another "
                                 f"program — keeping a full log at "
                                 f"{self.path} this run)")
                    print(f"run log: {self.note}", flush=True)
            if self.f is None:
                self.f = open(self.path, "w", encoding="ascii", newline="")
            self._header()
        except OSError as e:
            # The log is a diagnostic; the feed is the product. A full
            # disk or a bad log_dir must never take the overlays down.
            self.failed = str(e)
            self.f = None
            print(f"run log unavailable ({e}) — continuing without one",
                  flush=True)

    def _header(self):
        self.f.write("t_s,gear," + ",".join(c for _p, c in self.COLS) + "\n")

    def row(self, t, gear, decoded):
        if self.mode == "off" or self.failed:
            return
        if self.f is None:
            self._open()
            if self.f is None:
                return
        cells = [f"{decoded[p]:.1f}" if p in decoded else ""
                 for p, _c in self.COLS]
        try:
            self.f.write(f"{t:.3f},{gear}," + ",".join(cells) + "\n")
            self.f.flush()
            if self.mode == "tail" and self.f.tell() > self.TAIL_CAP:
                self._wrap()
        except OSError as e:
            self.failed = str(e)
            print(f"run log write failed ({e}) — continuing without one",
                  flush=True)
            self.close()

    def _wrap(self):
        self.f.seek(0)
        tail = self.f.read()
        tail = tail[len(tail) // 2:]
        tail = tail[tail.find("\n") + 1:]      # start on a row boundary
        self.f.seek(0)
        self.f.truncate()
        self._header()
        self.f.write(tail)
        self.f.flush()

    def describe(self):
        if not self.path:
            return "(off)"
        if self.mode == "tail":
            return (f"{self.path}  (tail of this run; previous run kept at "
                    f"{self.PREV_NAME}; --run-log full to keep everything)")
        return self.path + ("  " + self.note if self.note else "")

    def kept(self):
        """For exit messages: where the data went, if anywhere. In tail
        mode the honest verb is 'at', not 'kept' — the next data-bearing
        start rotates it to feed-prev.csv and the one after that eats it."""
        if self.mode == "off":
            return "Run log was off."
        if self.failed:
            return "Run log was unavailable this run."
        if self.f is None:
            return "No samples arrived, so no log was written (previous log untouched)."
        if self.mode == "tail":
            return (f"Log at {self.path} — one more run keeps it as "
                    f"{self.PREV_NAME}, two overwrite it; copy it out to "
                    f"keep it for good.")
        return f"Log kept: {self.path}"

    def close(self):
        try:
            if self.f:
                self.f.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Poll loops: live car, or replay of a recorded drive
# --------------------------------------------------------------------------

def decode_sample(byte_map):
    out = {}
    for pid, data in byte_map.items():
        if pid in DASH_PIDS:
            _n, _u, fn = DASH_PIDS[pid]
            try:
                out[pid] = fn(data)
            except Exception:
                pass
    return out


def query_batch(elm, pids, digit, timeout=3.0):
    """One batched Mode 01 request, optionally with the expected-response-
    count digit appended — the ELM then returns as soon as that many
    responses arrived instead of waiting out its timer."""
    req = "01" + "".join(f"{p:02X}" for p in pids)
    if digit is not None:
        req += f" {digit}"
    return parse_mode01(elm.cmd(req, timeout=timeout))


def autotune_digit(elm, pids, seconds=2.0):
    """Phase 1 left this as an experiment: does the response-count digit
    help, and does the ELM count a multi-frame reply as one response or
    three frames? Try the candidates, demand completeness, keep the fastest.
    Returns (digit_or_None, hz)."""
    print("\nAuto-tune: response-count digit (each candidate "
          f"{seconds:.0f}s)...")
    best = (None, 0.0)
    for digit in (None, 1, 2, 3):
        end = time.perf_counter() + seconds
        n, complete = 0, True
        try:
            while time.perf_counter() < end:
                res, _ = query_batch(elm, pids, digit)
                n += 1
                if any(p not in res for p in pids):
                    complete = False
                    break
        except ElmError:
            complete = False
        hz = n / seconds
        label = "no digit" if digit is None else f"digit {digit}"
        if not complete:
            print(f"  {label:9s}: incomplete replies — rejected")
            continue
        print(f"  {label:9s}: {hz:4.1f} req/s, all {len(pids)} PIDs answered")
        if hz > best[1]:
            best = (digit, hz)
    label = "no digit" if best[0] is None else f"digit {best[0]}"
    print(f"  keeping: {label} ({best[1]:.1f} req/s)")
    return best


def poll_car(args, state, run_log, sender, sched, elm, digit):
    misses = 0
    t0 = time.perf_counter()
    last_status = 0.0
    n = 0
    units = args.resolved_units
    while True:
        if sender.fatal is not None:
            print(f"\nfeed stopped (sender): {sender.fatal}")
            return 1
        pids = sched.next_pids()
        try:
            res, _ = query_batch(elm, pids, digit)
        except (ElmError, OSError):
            # ElmError: the adapter said something unhelpful. OSError (and
            # pyserial's SerialException, which subclasses it): the link
            # itself died — Bluetooth drop, adapter nap. Either way, a
            # hiccup is not a reason to lose the drive; 25 in a row is.
            misses += 1
            if misses >= 25:
                print(f"\n{misses} consecutive failed samples — the car has "
                      f"left the conversation. {run_log.kept()}")
                print("Power-cycle the adapter (unplug/replug), re-pair if "
                      "needed, and rerun.")
                return 1
            continue
        misses = 0
        t = time.perf_counter() - t0
        decoded = decode_sample(res)
        state.update(decoded, time.monotonic())
        run_log.row(t, state.gear, decoded)
        n += 1
        if t - last_status >= 1.0:
            last_status = t
            snap = state.snapshot(time.monotonic())
            rpm = snap["pids"].get(0x0C, 0.0)
            print(f"  t {t:5.0f}s  RPM {rpm:5.0f}  "
                  f"speed {fmt_speed(snap['true_speed_kmh'], units)} (true)  "
                  f"gear {gear_status(snap)}  "
                  f"poll {n / t if t else 0.0:4.1f} Hz  "
                  f"udp {sender.sent} pkts", flush=True)


def poll_replay(args, state, run_log, sender):
    """Feed the pipeline from a recorded drive log instead of a car. Same
    state updates, same sender — the only fiction is the clock, and even
    that is paced to the recording."""
    rows = []
    with open(args.replay, newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append((float(row["t_s"]),
                             {0x0C: float(row["rpm"]),
                              0x0D: float(row["speed_kmh"]),
                              **({0x11: float(row["throttle_pct"])}
                                 if row.get("throttle_pct") else {})}))
            except (KeyError, ValueError):
                continue
    if not rows:
        sys.exit(f"{args.replay}: no usable samples (need t_s,rpm,speed_kmh)")
    print(f"Replaying {len(rows)} samples from {args.replay} "
          f"at {args.speed:g}x...")
    # A wrapped tail log starts mid-drive; pace from its first row rather
    # than sleeping until the recording's own clock catches up (at the
    # 5MB cap that silence would be measured in hours).
    t_base = rows[0][0]
    if t_base > 1.0:
        print(f"  (log begins at t={t_base:.0f}s — earlier samples were "
              f"dropped by the tail cap; pacing from there)")
    units = args.resolved_units
    t_start = time.perf_counter()
    last_status = 0.0
    for t_s, decoded in rows:
        if sender.fatal is not None:
            print(f"\nfeed stopped (sender): {sender.fatal}")
            return 1
        target = t_start + (t_s - t_base) / args.speed
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        state.update(decoded, time.monotonic(), gear_t=t_s)
        run_log.row(t_s, state.gear, decoded)
        if t_s - last_status >= 1.0 or t_s == rows[-1][0]:
            last_status = t_s
            snap = state.snapshot(time.monotonic())
            print(f"  t {t_s:5.0f}s  RPM {decoded.get(0x0C, 0.0):5.0f}  "
                  f"speed {fmt_speed(snap['true_speed_kmh'], units)} (true)  "
                  f"gear {gear_status(snap)}  udp {sender.sent} pkts",
                  flush=True)
    print(f"\nReplay done. {sender.sent} packets sent, "
          f"{sender.errors} send errors.")
    return 0


# --------------------------------------------------------------------------
# SimHub registration (.simlink — the generated code's own extension; the
# online manual says .shlink, the generator disagrees, the generator wins).
# Writes %LOCALAPPDATA%\SimHub\ExternalSims\Registrations\{UniqueId}.simlink
# containing the absolute path of the repo's .simdef. Safe to re-run.
# --------------------------------------------------------------------------

def register_simdef(layout, layout_path):
    uid = layout.get("unique_id")
    simdef = layout.get("simdef_path")
    if not uid or not simdef:
        sys.exit(f"--register needs unique_id and simdef_path in "
                 f"{layout_path}.")
    simdef_abs = os.path.abspath(repo_path(*simdef.split("/")))
    if not os.path.exists(simdef_abs):
        sys.exit(f"definition not found at {simdef_abs} — git pull?")
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        sys.exit("--register is for the SimHub machine (Windows): "
                 "%LOCALAPPDATA% not set here.")
    reg_dir = os.path.join(base, "SimHub", "ExternalSims", "Registrations")
    os.makedirs(reg_dir, exist_ok=True)
    target = os.path.join(reg_dir, f"{uid}.simlink")
    with open(target, "w", encoding="utf-8") as f:
        f.write(simdef_abs)
    print(f"registered: {target}\n -> {simdef_abs}")
    return 0


# --------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="OBD2 -> SimHub UDP telemetry feed (phase 2)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--port",
                     help="COM port (COM3), device, or socket://host:port")
    src.add_argument("--replay", metavar="DRIVE.csv",
                     help="feed from a recorded drive log instead of a car")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="replay pace multiplier (default 1.0 = real time)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--udp", metavar="HOST:PORT",
                    help="feed target (default from feed_layout.json)")
    ap.add_argument("--calibration", default=repo_path("calibration.json"))
    ap.add_argument("--layout",
                    default=repo_path("extractor", "feed_layout.json"))
    ap.add_argument("--units", choices=["metric", "imperial"],
                    help="override display units from calibration.json")
    ap.add_argument("--digit", type=int, choices=range(0, 10),
                    help="pin the response-count digit; skips auto-tune")
    ap.add_argument("--no-tune", action="store_true",
                    help="skip the response-count auto-tune (no digit)")
    ap.add_argument("--tune-seconds", type=float, default=2.0)
    ap.add_argument("--dwell", type=float, default=GearWatch.DWELL_S,
                    help="seconds of steady agreement before the gear "
                         "readout changes (default %(default)s)")
    ap.add_argument("--dash-gear", choices=["hold", "honest"], default="hold",
                    help="what the SimHub gear widget wears while the clutch "
                         "is in: 'hold' keeps the last engaged gear while "
                         "rolling (default, overlay-friendly); 'honest' "
                         "shows the judge's N. The run log always logs "
                         "honest.")
    ap.add_argument("--log-dir", default="runs",
                    help="run-log directory (default: runs/)")
    ap.add_argument("--run-log", choices=["full", "tail", "off"],
                    default="tail",
                    help="the CSV each run writes as a side effect: 'tail' "
                         "(default) keeps ONE size-capped file, "
                         "feed-last.csv, overwritten every start — nothing "
                         "accumulates, the last run stays diagnosable; "
                         "'full' keeps a timestamped file per run (drives "
                         "you mean to keep, gear learning, development); "
                         "'off' writes nothing")
    # Verbs, not settings: each of these is an action you take once, and a
    # config file that performed it on every start would be a haunting
    # (register-and-exit in a supervisor restart loop, most notably).
    # per_run marks them un-configurable; the reasoning is the same one
    # that excludes positionals.
    ap.add_argument("--register", action="store_true",
                    help="write the SimHub .simlink registration and exit"
                    ).per_run = True
    ap.add_argument("--list-ports", action="store_true").per_run = True
    ap.add_argument("--debug", action="store_true",
                    help="dump raw adapter traffic on exit")
    return ap


def main():
    ap = build_parser()
    args = parse_with_config(ap, "obd_feed")

    layout = load_json(args.layout, "feed layout")
    if args.register:
        return register_simdef(layout, args.layout)

    if args.list_ports:
        try:
            from serial.tools import list_ports
        except ImportError:
            sys.exit("pyserial is required:  pip install pyserial")
        for p in list_ports.comports():
            print(f"{p.device:12s} {p.description}")
        print("\nBluetooth pairing creates TWO ports; use the OUTGOING one")
        print("(Windows: Bluetooth settings -> More Bluetooth options -> "
              "COM Ports).")
        return 0

    if not args.port and not args.replay:
        ap.error("--port or --replay is required (or --list-ports)")
    if args.replay and args.speed <= 0:
        ap.error("--speed must be > 0 (time still only runs forwards here)")

    calibration = load_json(args.calibration, "calibration")
    args.resolved_units = display_units(calibration, args.units)

    gears = calibration.get("gears", {})
    constants = gears.get("rpm_per_kmh")
    if not constants:
        sys.exit(f"{args.calibration} has no gears.rpm_per_kmh — run "
                 "probe/learn_gears.py on a drive log first (README: "
                 "'the drive protocol').")
    tol = float(gears.get("tolerance_pct", 7))

    sets = calibration.get("tire_sets", {})
    active = calibration.get("active_set")
    speed_factor = float(sets.get(active, {}).get("speed_factor", 1.0))

    state = CarState(GearWatch(constants, tol, dwell_s=args.dwell),
                     speed_factor=speed_factor,
                     display_hold=(args.dash_gear == "hold"))
    engine = calibration.get("engine", {})
    packer = Packer(layout, cal={
        "max_gears": len(constants),
        "max_rpm": float(engine.get("max_rpm", 0.0)),
        "fuel_tank_l": float(engine.get("fuel_tank_l", 0.0)),
    })

    udp_cfg = layout.get("udp", {})
    if args.udp:
        host, _sep, port_s = args.udp.rpartition(":")
        try:
            host, port = host or "127.0.0.1", int(port_s)
        except ValueError:
            ap.error(f"--udp wants HOST:PORT, got {args.udp!r}")
    else:
        host = udp_cfg.get("host", "127.0.0.1")
        port = int(udp_cfg.get("port", 0))
    if not port:
        ap.error("no UDP port: give --udp HOST:PORT or set it in "
                 "feed_layout.json")

    sender = Sender(state, packer, host, port,
                    is_replay=bool(args.replay))
    run_log = RunLog(args.log_dir, args.run_log)
    print(f"Source    -> "
          + (f"replay {args.replay}" if args.replay else args.port))
    print(f"UDP feed  -> {host}:{port}  ({packer.size} bytes/packet at "
          f"{SEND_HZ:.0f} Hz)")
    print(f"Run log   -> {run_log.describe()}")
    if args.run_log == "tail":
        # The default used to be a timestamped file per run, kept forever.
        # Anyone upgrading with a runs/ full of them was relying on that —
        # say so while the evidence of the old habit is still on disk.
        try:
            old = [n for n in os.listdir(args.log_dir)
                   if n.startswith("feed-2") and n.endswith(".csv")]
        except OSError:
            old = []
        if old:
            n = len(old)
            print(f"note: run logs no longer accumulate — this run "
                  f"overwrites {RunLog.TAIL_NAME} (one previous run kept "
                  f"as {RunLog.PREV_NAME}). Your {n} older "
                  f"feed-*.csv file{'' if n == 1 else 's'} "
                  f"{'is' if n == 1 else 'are'} untouched; "
                  f"\"run_log\": \"full\" in config.json restores the old "
                  f"behavior.")
    print(f"Units     -> {args.resolved_units}   "
          f"tire set -> {active} (speed factor {speed_factor:g})")
    print(f"Dash gear -> {args.dash_gear}"
          + ("  (holds last gear while rolling; log stays honest)"
             if args.dash_gear == "hold" else ""))
    print(f"Contract  -> SimHub definition {layout.get('unique_id')} "
          f"(layout v{layout['header'][2].get('value', '?')}."
          f"{layout['header'][3].get('value', '?')})")

    sender.start()
    try:
        if args.replay:
            return poll_replay(args, state, run_log, sender)

        try:
            elm = Elm(args.port, baud=args.baud)
        except Exception as e:
            print(f"Could not open {args.port}: {e}")
            print("Check --list-ports, and make sure nothing else is "
                  "holding the port.")
            return 1
        try:
            ident = adapter_init(elm)
            print(f"\nAdapter reset:     {ident}")
            print("Contacting vehicle (0100 supported-PID request)...")
            supported = get_supported_pids(elm)
            if not supported:
                print("  no answer. Ignition on? Engine running? Adapter "
                      "seated?")
                return 1
            sched = Scheduler(supported)
            print(f"  {len(supported)} PIDs; polling "
                  f"{len(sched.fast)} fast + {len(sched.slow)} rotating "
                  f"(slow tier refreshes ~every "
                  f"{sched.slow_refresh_s(4.5):.1f}s at 4.5 Hz)")
            if args.digit is not None:
                digit = args.digit
            elif args.no_tune:
                digit = None
            else:
                digit, _hz = autotune_digit(elm, sched.next_pids(),
                                            seconds=args.tune_seconds)
            print()
            return poll_car(args, state, run_log, sender, sched, elm, digit)
        except OSError as e:
            # A link death during init/auto-tune lands here; during the poll
            # loop the miss counter absorbs it first. Same advice either way.
            print(f"\nAborted mid-run: {e}")
            print("Power-cycle the adapter (unplug/replug), re-pair if "
                  f"needed, and rerun. {run_log.kept()}")
            return 1
        finally:
            if args.debug:
                print("\n--- raw traffic log ---")
                for line in elm.log:
                    print(line)
            elm.close()
    except KeyboardInterrupt:
        print(f"\nStopped. {sender.sent} packets sent, {sender.errors} "
              f"send errors. {run_log.kept()}")
        return 0
    finally:
        sender.stop.set()
        run_log.close()


if __name__ == "__main__":
    sys.exit(main())
