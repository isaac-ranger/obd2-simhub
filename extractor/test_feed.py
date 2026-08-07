"""Tests for the phase 2 feed. Run: python extractor/test_feed.py

Covers the scheduler, gear inference (including a full replay of Kris's real
drive_01 log — the log asserts its own story back), the packer, the
interpolating state, units, the run log, and — when pyserial is present —
the whole pipeline end to end against fake_car.py, batched multi-frame
replies and all. Stdlib only apart from that optional leg.
"""
import json
import os
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "probe"))

from obd_feed import (Scheduler, GearWatch, GearDisplay, CarState, Packer,
                      Sender, RunLog, decode_sample, display_units, fmt_speed,
                      fmt_temp, repo_path, MIN_RPM, MIN_SPEED)
import fake_car

REPO = os.path.join(HERE, os.pardir)
DRIVE = os.path.join(REPO, "reports", "2026-07-31-kris-drive_01.csv")

with open(os.path.join(REPO, "calibration.json"), encoding="utf-8") as f:
    CAL = json.load(f)
# The SHIPPED calibration.json deliberately carries no learned values — a
# stranger's clone must not inherit one car's gear ratios and pedal span.
# So the suite brings its own constants instead of reading whatever the repo
# happens to ship. It used to read them, which meant these tests quietly
# depended on us shipping a fully-learned config; emptying the seed file
# broke seven of them and that coupling was the reason.
CONSTANTS = CAL.get("gears", {}).get(
    "rpm_per_kmh", [103.0, 60.4, 43.3, 35.0, 29.2, 25.2])
TOL = CAL.get("gears", {}).get("tolerance_pct", 7)

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


# --- scheduler -------------------------------------------------------------

sup = {0x0C, 0x0D, 0x11, 0x04, 0x05, 0x5C, 0x2F, 0x33, 0x42}
s = Scheduler(sup)
reqs = [s.next_pids() for _ in range(4)]
ok("scheduler: every request carries the fast three",
   all(set(r) >= {0x0C, 0x0D, 0x11} for r in reqs), f"{reqs}")
ok("scheduler: never more than one CAN message worth",
   all(len(r) <= 6 for r in reqs), f"{reqs}")
covered = set().union(*[set(r) for r in reqs[:2]])
ok("scheduler: all six slow channels inside two requests",
   covered >= sup, f"two requests covered {sorted(hex(p) for p in covered)}")
ok("scheduler: slow refresh estimate sane",
   0.3 < s.slow_refresh_s(4.5) < 0.6, f"{s.slow_refresh_s(4.5)}")

s2 = Scheduler({0x0C, 0x0D, 0x11})
ok("scheduler: no slow channels — fast only, no crash",
   s2.next_pids() == [0x0C, 0x0D, 0x11])

try:
    Scheduler({0x04, 0x05})
    ok("scheduler: refuses a car with no fast channels", False)
except Exception:
    ok("scheduler: refuses a car with no fast channels", True)

# --- gear inference ---------------------------------------------------------

DT = 0.213                                 # the drive log's native pacing


class Feeder:
    """Feed a GearWatch with auto-advancing timestamps."""

    def __init__(self, gw, dt=DT):
        self.gw, self.dt, self.t = gw, dt, 0.0

    def __call__(self, rpm, spd, n=1):
        out = None
        for _ in range(n):
            self.t += self.dt
            out = self.gw.feed(rpm, spd, self.t)
        return out


for gear_n, c in enumerate(CONSTANTS, start=1):
    f2 = Feeder(GearWatch(CONSTANTS, TOL))
    shown = f2(c * 50.0, 50.0, n=3)        # ~0.64 s of steady driving
    ok(f"gear: exact constant reads gear {gear_n}", shown == gear_n,
       f"got {shown}")

noman = (CONSTANTS[0] + CONSTANTS[1]) / 2   # 81.7, between 1st and 2nd bands
ok("gear: ratio in no-man's-land reads neutral",
   Feeder(GearWatch(CONSTANTS, TOL))(noman * 50.0, 50.0, n=4) == 0)
ok("gear: below MIN_SPEED reads neutral",
   Feeder(GearWatch(CONSTANTS, TOL))(3000.0, MIN_SPEED - 1, n=4) == 0)
ok("gear: engine off reads neutral instantly",
   Feeder(GearWatch(CONSTANTS, TOL))(0.0, 40.0) == 0)

fd = Feeder(GearWatch(CONSTANTS, TOL))
fd(CONSTANTS[2] * 40, 40.0, n=3)          # 3rd confirmed
blip = fd(CONSTANTS[1] * 40, 40.0)        # one 2nd-gear-shaped sample
ok("gear: single blip does not change the readout", blip == 3, f"got {blip}")
ok("gear: 0.35s of steady agreement does change it",
   fd(CONSTANTS[1] * 40, 40.0, n=2) == 2)
one_out = fd(noman * 40.0, 40.0)          # single out-of-band outlier
ok("gear: one out-of-band sample does not drop the readout (hysteresis)",
   one_out == 2, f"got {one_out}")
ok("gear: 0.35s out of band does drop it",
   fd(noman * 40.0, 40.0, n=2) == 0)
fd(CONSTANTS[3] * 60, 60.0, n=3)          # 4th confirmed
ok("gear: engine cut drops a confirmed gear with no dwell",
   fd(0.0, 60.0) == 0)

fs = Feeder(GearWatch(CONSTANTS, TOL))
fs(CONSTANTS[5] * 60, 60.0, n=3)          # cruising 6th
got = [fs(CONSTANTS[2] * 0.96 * 60, 60.0), fs(CONSTANTS[2] * 1.04 * 60, 60.0)]
ok("gear: an unsteady sweep through a band never confirms it",
   3 not in got, f"got {got}")

# QA's regression: the guards must NOT rescale with poll rate (they are
# time-based for exactly this reason — the read-loop fix made the live rate
# unknown). 25 Hz clutch-in braking stop from 50 km/h in 3rd: rpm decays
# 800 rpm/s to idle, speed bleeds 3.6 km/h/s; the ratio sweeps down through
# 5th's and 6th's bands on its way. Per-sample guards flashed 4-5-6 here.
gw25 = GearWatch(CONSTANTS, TOL)
t25 = 0.0
seen_drive = set()
for _ in range(25):                        # one settled second in 3rd
    t25 += 0.04
    seen_drive.add(gw25.feed(CONSTANTS[2] * 50.0, 50.0, t25))
ok("gear @25Hz: 3rd confirms while actually driving", 3 in seen_drive,
   f"saw {sorted(seen_drive)}")
rpm25, spd25 = CONSTANTS[2] * 50.0, 50.0
seen_brake = set()
for _ in range(250):                       # ten seconds of braking to idle
    t25 += 0.04
    rpm25 = max(800.0, rpm25 - 800.0 * 0.04)
    spd25 = max(0.0, spd25 - 3.6 * 0.04)
    seen_brake.add(gw25.feed(rpm25, spd25, t25))
ok("gear @25Hz: clutch-in braking never flashes a phantom gear",
   seen_brake <= {3, 0}, f"saw {sorted(seen_brake)}")

# --- the real drive: Kris's log asserts its own story back ------------------

import csv as _csv
rows = []
with open(DRIVE, newline="") as f:
    for row in _csv.DictReader(f):
        try:
            rows.append((float(row["t_s"]), float(row["rpm"]),
                         float(row["speed_kmh"])))
        except (KeyError, ValueError):
            continue
ok("drive_01: log loads", len(rows) > 500, f"{len(rows)} rows")

gw = GearWatch(CONSTANTS, TOL)
trace = []                                 # (t, rpm, shown)
for t, rpm, spd in rows:
    shown = gw.feed(rpm, spd, t)
    trace.append((t, rpm, shown))
    if rpm == 0.0 and shown != 0:
        ok("drive_01: engine off never shows a gear", False, f"t={t}")
        break
else:
    ok("drive_01: engine off never shows a gear", True)

seen = {g for _t, _r, g in trace if g}
ok("drive_01: all six gears recovered", seen == {1, 2, 3, 4, 5, 6},
   f"saw {sorted(seen)}")

first = {}
for i, (_t, _r, g_) in enumerate(trace):
    if g_ and g_ not in first:
        first[g_] = i
ok("drive_01: gears first appear in driving order 1..6",
   [k for k, _v in sorted(first.items(), key=lambda kv: kv[1])] ==
   [1, 2, 3, 4, 5, 6],
   f"{sorted(first.items(), key=lambda kv: kv[1])}")

pos = first[6]
descend_ok = True
detail = ""
for want in (5, 4, 3, 2, 1):
    nxt = next((i for i in range(pos + 1, len(trace))
                if trace[i][2] == want), None)
    if nxt is None:
        descend_ok, detail = False, f"no {want} after index {pos}"
        break
    pos = nxt
ok("drive_01: sequential downshift 5-4-3-2-1 recovered", descend_ok, detail)

pull = [(t, r, g_) for t, r, g_ in trace if r > 6500]
pull_gears = {g_ for _t, _r, g_ in pull}
ok("drive_01: the redline pull reads 1st gear and nothing else",
   pull_gears <= {0, 1} and sum(1 for *_x, g_ in pull if g_ == 1) >= 5,
   f"{len(pull)} samples >6500rpm, gears {sorted(pull_gears)}")

changes = [g_ for i, (_t, _r, g_) in enumerate(trace)
           if g_ and (i == 0 or trace[i - 1][2] != g_)]
ok("drive_01: the readout tells the drive's exact story, no phantoms",
   changes == [1, 1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1],
   f"engagements: {changes}")

# --- gear display hold -------------------------------------------------------

gd = GearDisplay()
ok("display: engaged gear passes through", gd.feed(2, 3000.0, 40.0) == 2)
ok("display: moving neutral holds the last gear",
   gd.feed(0, 1500.0, 35.0) == 2)
ok("display: still holding through a long coast",
   all(gd.feed(0, 900.0, s) == 2 for s in (30.0, 20.0, 10.0, 5.0)))
ok("display: standing neutral clears to N", gd.feed(0, 800.0, 0.0) == 0)
gd.feed(3, 3000.0, 40.0)
ok("display: engine off clears a held gear", gd.feed(0, 0.0, 20.0) == 0)

# the real drive through the hold. The promise is scoped: while rolling
# WITH an engagement since the last standstill (or engine cut), the dash
# never wears N. Launch windows — rolling away from a stop before 1st
# confirms — honestly read N, and that is correct, not a hold failure.
drive2 = os.path.join(REPO, "reports", "2026-07-31-kris-drive_02.csv")
gd2, honest_n, dash_n, viol = GearDisplay(), 0, 0, 0
have_gear_since_stop = False
with open(drive2, newline="") as f:
    for row in _csv.DictReader(f):
        g, rpm, spd = int(row["gear"]), float(row["rpm"]), \
            float(row["speed_kmh"])
        d = gd2.feed(g, rpm, spd)
        if spd <= GearDisplay.STAND_KMH or rpm < GearWatch.ENGINE_OFF_RPM:
            have_gear_since_stop = False
        if g > 0:
            have_gear_since_stop = True
        honest_n += g == 0
        dash_n += d == 0
        if have_gear_since_stop and spd > GearDisplay.STAND_KMH and d == 0:
            viol += 1
ok("display drive_02: held gear never drops to N mid-roll",
   viol == 0, f"{viol} violations")
ok("display drive_02: the hold absorbs the clutch-in coasting",
   honest_n - dash_n >= 100 and 0 < dash_n < honest_n,
   f"honest N {honest_n}, dash N {dash_n}, absorbed {honest_n - dash_n}")

# CarState honors --dash-gear honest. Clutch-in samples sit BELOW MIN_RPM
# on purpose: mid-band rpm/speed pairs exist (1200 rpm at 35 km/h IS 4th's
# ratio) and with a zero dwell the judge would rightly take them.
cs = CarState(GearWatch(CONSTANTS, TOL, dwell_s=0.0), display_hold=False)
for i in range(4):
    cs.update({0x0C: CONSTANTS[1] * 40.0, 0x0D: 40.0}, float(i),
              gear_t=float(i))
ok("display: honest-mode judge confirms 2nd first", cs.gear == 2,
   f"{cs.gear}")
cs.update({0x0C: 850.0, 0x0D: 35.0}, 4.0, gear_t=4.0)
cs.update({0x0C: 840.0, 0x0D: 34.0}, 5.0, gear_t=5.0)
snap_h = cs.snapshot(5.0)
ok("display: honest mode never holds",
   snap_h["gear"] == 0 and snap_h["gear_display"] == 0,
   f"{snap_h['gear']} / {snap_h['gear_display']}")

# THE headline promise, pinned end to end: the run log receives the judge's
# gear, never the display hold. This drives the real poll_replay loop — the
# actual run_log.row call site — so swapping state.gear for
# state.gear_display there fails HERE, not in some future drive report.
from obd_feed import poll_replay
import types as _types

_mini = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="")
_mini.write("t_s,rpm,speed_kmh,throttle_pct\n")
_t = 0.0
for _i in range(10):                       # 2nd gear, judge confirms
    _mini.write(f"{_t:.3f},{CONSTANTS[1] * 40.0:.1f},40,20\n")
    _t += 0.2
for _i in range(10):                       # clutch-in coast: honest N rolls
    _mini.write(f"{_t:.3f},850.0,30,0\n")
    _t += 0.2
_mini.close()

_logdir = tempfile.mkdtemp()
_st = CarState(GearWatch(CONSTANTS, TOL), display_hold=True)
_rl = RunLog(_logdir)
_snd = _types.SimpleNamespace(fatal=None, sent=0, errors=0)  # what poll_replay reads
_args = _types.SimpleNamespace(replay=_mini.name, speed=1000.0,
                               resolved_units="metric")
poll_replay(_args, _st, _rl, _snd)
_rl.close()
with open(_rl.path, newline="") as _f:
    _logged = [(float(r["t_s"]), int(r["gear"]))
               for r in _csv.DictReader(_f)]
# the judge's symmetric dwell keeps honest 2nd for ~0.35s into the coast;
# assert the settled window, where honest N and the held dash must differ
_coast = [g for t, g in _logged if t >= 2.6]
ok("honest log: replay wrote every sample", len(_logged) == 20,
   f"{len(_logged)} rows")
ok("honest log: judge confirmed 2nd before the coast",
   any(g == 2 for _t2, g in _logged), f"{_logged}")
ok("honest log: settled coast logs N while the dash holds 2nd",
   _coast and all(g == 0 for g in _coast) and _st.gear_display == 2,
   f"coast rows {_coast}, dash {_st.gear_display}")

# A wrapped tail log starts mid-drive; replay must pace from its first row,
# not sleep until the recording's own clock catches up. Unrebased, this
# file sleeps toward t=5000s (5s wall even at 1000x) before its first
# packet; rebased it finishes in well under a second. The 3s bound leaves
# room for a slow bench and none for the bug.
_late = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="")
_late.write("t_s,rpm,speed_kmh,throttle_pct\n")
_t = 5000.0
for _i in range(10):
    _late.write(f"{_t:.3f},{CONSTANTS[1] * 40.0:.1f},40,20\n")
    _t += 0.2
_late.close()
_st3 = CarState(GearWatch(CONSTANTS, TOL))
_rl3 = RunLog(tempfile.mkdtemp(), "off")
_snd3 = _types.SimpleNamespace(fatal=None, sent=0, errors=0)
_args3 = _types.SimpleNamespace(replay=_late.name, speed=1000.0,
                                resolved_units="metric")
_t0 = time.time()
poll_replay(_args3, _st3, _rl3, _snd3)
_elapsed = time.time() - _t0
ok("replay: a mid-drive log plays immediately (t rebased to first row)",
   _elapsed < 3.0,
   f"{_elapsed:.1f}s — sleeping toward t=5000 means the rebase is gone")
os.unlink(_mini.name)

# --- packer: the real SimHub contract ----------------------------------------

with open(os.path.join(HERE, "feed_layout.json"), encoding="utf-8") as f:
    LAYOUT = json.load(f)

ENGINE_CAL = {"max_gears": 6, "max_rpm": 8000.0, "fuel_tank_l": 54.0}
pk = Packer(LAYOUT, cal=ENGINE_CAL)
ok("packer: struct packs to the contract's exact 101 bytes",
   pk.size == 101, f"{pk.size}")

snap = {"pids": {0x0C: 4321.0, 0x0D: 88.0, 0x11: 42.0, 0x04: 30.0,
                 0x05: 90.0, 0x5C: 104.0, 0x2F: 62.0, 0x33: 101.0,
                 0x42: 13.98},
        "gear": 4, "true_speed_kmh": 88.0 * 0.938}
RT = {"emitter_id": 0xDEADBEEF, "session_id": 42, "counter": 7,
      "session_time": 1.5, "session_running": 1, "is_replay": 0}
blob = pk.pack(snap, RT)
ok("packer: packet is exactly struct-sized", len(blob) == pk.size)
ok("packer: signatures open the packet, little-endian",
   blob[:8] == bytes.fromhex("03399651e70e3f8a"), blob[:8].hex())
back = pk.unpack(blob)
ok("packer: EngineRpm survives the round trip",
   abs(back["EngineRpm"] - 4321.0) < 0.5, f"{back['EngineRpm']}")
ok("packer: Gear is the string '4'", back["Gear"] == "4",
   f"{back['Gear']!r}")
held = pk.unpack(pk.pack({**snap, "gear": 0, "gear_display": 2}, RT))
ok("packer: Gear wears the display hold, not the honest N",
   held["Gear"] == "2", f"{held['Gear']!r}")
ok("packer: PacketsCounter lands in the header",
   back["PacketsCounter"] == 7)
ok("packer: SpeedKmh is true speed, not raw OBD speed",
   abs(back["SpeedKmh"] - 88.0 * 0.938) < 0.01, f"{back['SpeedKmh']}")
ok("packer: Throttle rescaled to SimHub's 0-1",
   abs(back["Throttle"] - 0.42) < 0.001, f"{back['Throttle']}")
ok("packer: Fuel converted percent -> liters",
   abs(back["Fuel"] - 0.62 * 54.0) < 0.05, f"{back['Fuel']}")
ok("packer: MaxFuel/MaxGears/MaxRpm ride from calibration",
   back["MaxFuel"] == 54.0 and back["MaxGears"] == 6
   and back["EngineMaxRpm"] == 8000.0)
ok("packer: engine running flags set while driving",
   back["EngineStarted"] == 1 and back["EngineIgnitionOn"] == 1)
ok("packer: session header carried",
   back["EmitterInstanceId"] == 0xDEADBEEF and back["SessionId"] == 42
   and back["IsReplay"] == 0 and back["IsSessionRunning"] == 1
   and abs(back["SessionTimeSeconds"] - 1.5) < 1e-9)

off_snap = {"pids": {0x0C: 0.0, 0x0D: 0.0}, "gear": 0,
            "true_speed_kmh": 0.0}
off = pk.unpack(pk.pack(off_snap, RT))
ok("packer: Auto-Stop reads engine off, gear N, ignition still on",
   off["EngineStarted"] == 0 and off["Gear"] == "N"
   and off["EngineIgnitionOn"] == 1,
   f"started={off['EngineStarted']} gear={off['Gear']!r}")

tiny = Packer({"endian": "little",
               "header": [{"name": "m", "type": "u16", "value": 0x0102}],
               "fields": []})
ok("packer: little-endian on the wire",
   tiny.pack({"pids": {}, "gear": 0, "true_speed_kmh": 0}, {}) ==
   b"\x02\x01")

# --- interpolating state ------------------------------------------------------

st = CarState(GearWatch(CONSTANTS, TOL), speed_factor=0.938)
t0 = time.monotonic()
st.update({0x0C: 2000.0, 0x0D: 50.0, 0x05: 90.0}, t0)
st.update({0x0C: 3000.0, 0x0D: 60.0, 0x05: 91.0}, t0 + 0.2)
st.poll_period = 0.1                       # render 0.1s back from "now"
mid = st.snapshot(t0 + 0.2)                # render_t = t0+0.1 = halfway
ok("state: rpm interpolates midway between samples",
   2400.0 < mid["pids"][0x0C] < 2600.0, f"{mid['pids'][0x0C]}")
late = st.snapshot(t0 + 1.0)               # past newest sample, not yet stale
ok("state: beyond newest sample the needle holds, never extrapolates",
   late["pids"][0x0C] == 3000.0, f"{late['pids'][0x0C]}")
ok("state: slow tier never interpolates (coolant is last-known, full stop)",
   mid["pids"][0x05] == 91.0, f"{mid['pids'][0x05]}")
ok("state: true speed = raw x tire factor",
   abs(mid["true_speed_kmh"] - mid["pids"][0x0D] * 0.938) < 1e-9)
gone = st.snapshot(t0 + 3.5)               # STALE_S exceeded: car left the call
ok("state: a silent car goes honestly dark, not frozen mid-rev",
   gone["pids"] == {} and gone["gear"] == 0
   and gone["true_speed_kmh"] == 0.0, f"{gone}")

# Fast replay: wall time is compressed but the gear judge must run on DATA
# time — at --speed 25 a wall-clock dwell would judge a different drive.
stf = CarState(GearWatch(CONSTANTS, TOL), speed_factor=1.0)
w0 = time.monotonic()
for i in range(4):
    stf.update({0x0C: CONSTANTS[2] * 50.0, 0x0D: 50.0},
               w0 + i * 0.01, gear_t=i * 0.213)
ok("state: replay gear judgment runs on data time, not wall time",
   stf.gear == 3, f"got {stf.gear}")

# --- units ---------------------------------------------------------------------

ok("units: calibration default honored",
   display_units({"units": {"display": "imperial"}}) == "imperial")
ok("units: override wins",
   display_units({"units": {"display": "imperial"}}, "metric") == "metric")
ok("units: missing block means metric", display_units({}) == "metric")
ok("units: 100 km/h is 62 mph", fmt_speed(100.0, "imperial").strip()
   == "62 mph", fmt_speed(100.0, "imperial"))
ok("units: 90 C is 194 F", fmt_temp(90.0, "imperial") == "194F",
   fmt_temp(90.0, "imperial"))

# --- decode + run log ------------------------------------------------------------

dec = decode_sample({0x0C: b"\x1a\xf8", 0x0D: b"\x58", 0xEE: b"\x00"})
ok("decode: rpm formula", abs(dec[0x0C] - 0x1AF8 / 4.0) < 0.01)
ok("decode: unknown PIDs are dropped, not fatal", 0xEE not in dec)

with tempfile.TemporaryDirectory() as td:
    rl = RunLog(td)
    rl.row(1.234, 3, {0x0C: 3000.0, 0x0D: 70.0})
    rl.close()
    with open(rl.path) as f:
        lines = f.read().splitlines()
    ok("runlog: header + one row", len(lines) == 2, f"{lines}")
    ok("runlog: row carries t, gear, rpm",
       lines[1].startswith("1.234,3,3000.0,70.0,"), lines[1])

# The three --run-log sizes. 'tail' is the shipped default; its promises:
# lazy (a run that never hears the car costs nothing), one generation of
# history (feed-prev.csv), capped, newest-half-wins on wrap. The wrap
# assertions here are written to FAIL on a keep-oldest implementation —
# an adversarial mutation run proved the previous ones could not.
with tempfile.TemporaryDirectory() as td:
    a = RunLog(td, "tail")
    ok("runlog tail: nothing touches disk before the first sample",
       os.listdir(td) == [], f"{os.listdir(td)}")
    a.row(1.0, 2, {0x0C: 2000.0})
    a.close()
    ok("runlog tail: the first sample creates the file",
       os.listdir(td) == ["feed-last.csv"], f"{os.listdir(td)}")

    ghost = RunLog(td, "tail")           # wrong port, adapter unplugged...
    ghost.close()                        # ...a run with zero samples
    with open(os.path.join(td, RunLog.TAIL_NAME)) as f:
        lines = f.read().splitlines()
    ok("runlog tail: a sampleless run leaves the last log untouched",
       os.listdir(td) == ["feed-last.csv"] and lines[1].startswith("1.000,"),
       f"{os.listdir(td)} {lines}")
    ok("runlog tail: ...and its exit line says so",
       ghost.kept().startswith("No samples"), ghost.kept())

    b = RunLog(td, "tail")
    b.row(2.0, 3, {0x0C: 3000.0})
    b.close()
    ok("runlog tail: the previous run survives as feed-prev.csv",
       sorted(os.listdir(td)) == ["feed-last.csv", "feed-prev.csv"],
       f"{os.listdir(td)}")
    with open(b.path) as f:
        last = f.read().splitlines()
    with open(os.path.join(td, RunLog.PREV_NAME)) as f:
        prevl = f.read().splitlines()
    ok("runlog tail: last holds the new run", last[1].startswith("2.000,"))
    ok("runlog tail: prev holds the old run", prevl[1].startswith("1.000,"))

    c = RunLog(td, "tail")
    c.TAIL_CAP = 600                     # shrink the cap to test the wrap
    for i in range(50):
        c.row(float(i), 1, {0x0C: 1000.0})
    c.close()
    with open(c.path) as f:
        lines = f.read().splitlines()
    data = [ln for ln in lines if not ln.startswith("t_s")]
    first_t = float(data[0].split(",")[0])
    ok("runlog tail: the wrap drops the OLD half",
       first_t >= 10.0, f"first surviving row t={first_t} (0.0 = keep-oldest)")
    ok("runlog tail: the newest rows survive the wrap",
       data[-1].startswith("49.000,"), data[-1])
    ok("runlog tail: exactly one header, and it is line one",
       lines[0].startswith("t_s,gear,")
       and sum(1 for ln in lines if ln.startswith("t_s")) == 1, f"{lines[:2]}")
    ok("runlog tail: the cap bounds the file",
       os.path.getsize(c.path) <= 600 + 100, f"{os.path.getsize(c.path)}B")

    size_before = os.path.getsize(os.path.join(td, RunLog.TAIL_NAME))
    off = RunLog(td, "off")
    off.row(1.0, 1, {0x0C: 1000.0})      # must be a no-op, not a crash
    off.close()
    ok("runlog off: writes nothing anywhere",
       sorted(os.listdir(td)) == ["feed-last.csv", "feed-prev.csv"]
       and os.path.getsize(os.path.join(td, RunLog.TAIL_NAME)) == size_before,
       f"{os.listdir(td)}")
    ok("runlog off: exit message doesn't point at a ghost",
       off.kept() == "Run log was off.", off.kept())

# Windows reality: a viewer holding these files open (Excel does this)
# locks them, and the next start must not crash over a spreadsheet. A
# directory squatting on the rotation target refuses the os.replace the
# same way a lock does, on every platform this test runs on.
with tempfile.TemporaryDirectory() as td:
    with open(os.path.join(td, RunLog.TAIL_NAME), "w") as f:
        f.write("t_s,gear\n0.5,1\n")
    os.mkdir(os.path.join(td, RunLog.PREV_NAME))
    locked = RunLog(td, "tail")
    locked.row(1.0, 2, {0x0C: 2000.0})
    locked.close()
    ok("runlog tail: a locked rotation falls back, run stays alive",
       locked.mode == "full"
       and os.path.basename(locked.path).startswith("feed-2")
       and "locked" in locked.describe(),
       f"path={locked.path!r} describe={locked.describe()!r}")

# A log_dir that cannot exist must never take the feed down — the log is
# the diagnostic, the feed is the product.
with tempfile.TemporaryDirectory() as td:
    blocker = os.path.join(td, "not-a-dir")
    with open(blocker, "w") as f:
        f.write("a file where the path needs a directory")
    hurt = RunLog(os.path.join(blocker, "runs"), "tail")
    hurt.row(1.0, 1, {0x0C: 1000.0})     # must not raise
    hurt.row(2.0, 1, {0x0C: 1000.0})     # and must stay quiet after
    hurt.close()
    ok("runlog: an impossible log_dir disables logging, never the feed",
       hurt.failed is not None and "unavailable" in hurt.kept(),
       f"failed={hurt.failed!r} kept={hurt.kept()!r}")

# --- end to end against the fake car (needs pyserial) -----------------------------

try:
    import serial  # noqa: F401
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False
    print("SKIP  e2e fake-car leg (pyserial not installed here — the leg "
          "runs in CI-of-one on the Windows side)")

if HAVE_SERIAL:
    from obd_probe import Elm, get_supported_pids, adapter_init
    from obd_feed import query_batch, autotune_digit

    port, _th = fake_car.serve_background()
    elm = Elm(f"socket://127.0.0.1:{port}", timeout=4.0)
    ident = adapter_init(elm)
    ok("e2e: fake adapter identifies", "fake car" in ident, ident)
    supported = get_supported_pids(elm)
    ok("e2e: supported-PID walk finds the whole fake inventory",
       supported >= set(fake_car.SUPPORTED),
       f"missing {set(fake_car.SUPPORTED) - supported}")
    sched = Scheduler(supported)
    pids = sched.next_pids()
    res, resp = query_batch(elm, pids, None)
    ok("e2e: 6-PID batch answered complete via ISO-TP multi-frame",
       all(p in res for p in pids), f"asked {pids}, got {sorted(res)}")
    res1, _ = query_batch(elm, pids, 1)
    ok("e2e: response-count digit variant also parses complete",
       all(p in res1 for p in pids), f"got {sorted(res1)}")
    digit, hz = autotune_digit(elm, pids, seconds=0.3)
    ok("e2e: auto-tune completes and picks something sane",
       hz > 0, f"digit={digit} hz={hz}")
    elm.close()

# --- end to end: sender + receiver over real UDP -----------------------------------

rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.bind(("127.0.0.1", 0))
rx.settimeout(2.0)
rx_port = rx.getsockname()[1]

st2 = CarState(GearWatch(CONSTANTS, TOL), speed_factor=1.0)
now = time.monotonic()
st2.update({0x0C: 3000.0, 0x0D: 69.0, 0x11: 20.0}, now - 0.1)
st2.update({0x0C: 3100.0, 0x0D: 71.0, 0x11: 22.0}, now)
snd = Sender(st2, pk, "127.0.0.1", rx_port, hz=120.0, is_replay=True)
snd.start()
pkts = []
try:
    while len(pkts) < 8:
        pkts.append(rx.recvfrom(4096)[0])
except socket.timeout:
    pass
snd.stop.set()
rx.close()
ok("udp: packets arrive", len(pkts) >= 8, f"{len(pkts)} packets")
if pkts:
    d0, d1 = pk.unpack(pkts[0]), pk.unpack(pkts[-1])
    ok("udp: payload decodes to live rpm",
       2900.0 <= d0["EngineRpm"] <= 3100.0, f"{d0['EngineRpm']}")
    ok("udp: packet counter increments and never resets",
       d1["PacketsCounter"] > d0["PacketsCounter"] >= 1,
       f"{d0['PacketsCounter']} -> {d1['PacketsCounter']}")
    ok("udp: emitter id is non-zero and stable across packets",
       d0["EmitterInstanceId"] == d1["EmitterInstanceId"] != 0)
    ok("udp: session time advances",
       d1["SessionTimeSeconds"] > d0["SessionTimeSeconds"] >= 0.0)
    ok("udp: replay mode flagged honestly", d0["IsReplay"] == 1)
    ok("udp: gear rides along as text", d0["Gear"] in ("N", "3"),
       f"{d0['Gear']!r}")

# ── the gears refusal itself ──────────────────────────────────────────────
# A calibration with no gears must refuse to start, loudly, by name — that
# is the shipped seed's whole first-run contract. Mutation testing proved
# every suite stayed green with the refusal deleted (a fresh clone then died
# with a bare TypeError instead), so this case drives the real main() the
# way a fresh clone does: subprocess, gearless file, no fixture constants.
import subprocess
with tempfile.TemporaryDirectory() as td:
    bare = os.path.join(td, "calibration.json")
    with open(bare, "w", encoding="utf-8") as f:
        json.dump({}, f)
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "obd_feed.py"),
         "--replay", DRIVE, "--calibration", bare],
        capture_output=True, text=True, timeout=60)
    ok("bare calibration: feed refuses to start", proc.returncode != 0,
       f"rc={proc.returncode}")
    ok("bare calibration: refusal is named, not a traceback",
       "has no gears.rpm_per_kmh" in proc.stderr
       and "learn_gears" in proc.stderr
       and "Traceback" not in proc.stderr,
       proc.stderr.strip()[:120] or proc.stdout.strip()[:120])

# ── wrong-typed calibration OUTSIDE the throttle section ──────────────────
# The throttle section earned a full named-refusal vocabulary across the QA
# passes; the rest of the file used to traceback — a top-level array died as
# AttributeError in display_units, a lone float in rpm_per_kmh as TypeError
# inside GearWatch, a string tolerance as ValueError mid-float(). The seed's
# own _notes invite hand-editing every one of these sections, so a typo here
# is expected traffic and gets the same treatment: refuse by name, through
# the real main(), the way a hand-editor meets it.
GOOD_GEARS = {"rpm_per_kmh": [103.0, 60.4, 43.3, 35.0, 29.2, 25.2],
              "tolerance_pct": 7}
BAD_CALS = [
    ("top-level array", [1, 2], "not an object"),
    ("top-level string", "calibrate me", "not an object"),
    ("top-level number", 7, "not an object"),
    ("gears as array", {"gears": [103.0]}, '"gears"'),
    ("rpm_per_kmh as lone float", {"gears": {"rpm_per_kmh": 5.0}},
     "rpm_per_kmh"),
    ("rpm_per_kmh as string", {"gears": {"rpm_per_kmh": "abc"}},
     "rpm_per_kmh"),
    ("rpm_per_kmh with a string element",
     {"gears": {"rpm_per_kmh": [103.0, "x"]}}, "rpm_per_kmh"),
    ("rpm_per_kmh with a NaN element",
     {"gears": {"rpm_per_kmh": [103.0, float("nan")]}}, "rpm_per_kmh"),
    ("rpm_per_kmh with a zero element",
     {"gears": {"rpm_per_kmh": [103.0, 0.0]}}, "rpm_per_kmh"),
    ("tolerance_pct as string",
     {"gears": {"rpm_per_kmh": [103.0], "tolerance_pct": "x"}},
     "tolerance_pct"),
    ("tolerance_pct as NaN",
     {"gears": {"rpm_per_kmh": [103.0], "tolerance_pct": float("nan")}},
     "tolerance_pct"),
    ("tolerance_pct as zero",
     {"gears": {"rpm_per_kmh": [103.0], "tolerance_pct": 0}},
     "tolerance_pct"),
    ("engine as array", {"gears": GOOD_GEARS, "engine": [8000]}, '"engine"'),
    ("max_rpm as string",
     {"gears": GOOD_GEARS, "engine": {"max_rpm": "high"}}, "max_rpm"),
    ("tire_sets as array",
     {"gears": GOOD_GEARS, "tire_sets": ["street_18"]}, '"tire_sets"'),
    ("active_set as array",
     {"gears": GOOD_GEARS, "tire_sets": {}, "active_set": ["street_18"]},
     "active_set"),
    ("active_set naming no set (typo -> silent factor 1.0)",
     {"gears": GOOD_GEARS, "active_set": "stret_18",
      "tire_sets": {"street_18": {"speed_factor": 1.0}}}, "street_18"),
    ("tire set entry as array",
     {"gears": GOOD_GEARS, "active_set": "s", "tire_sets": {"s": [1.0]}},
     "not an object"),
    ("speed_factor as string",
     {"gears": GOOD_GEARS, "active_set": "s",
      "tire_sets": {"s": {"speed_factor": "fast"}}}, "speed_factor"),
    ("speed_factor as zero",
     {"gears": GOOD_GEARS, "active_set": "s",
      "tire_sets": {"s": {"speed_factor": 0}}}, "speed_factor"),
    ("units as array", {"gears": GOOD_GEARS, "units": ["imperial"]},
     '"units"'),
]
with tempfile.TemporaryDirectory() as td:
    for label, cal, token in BAD_CALS:
        p = os.path.join(td, "cal.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cal, f)
        # --speed 1000: a refusal never reaches the replay, but a MUTANT that
        # waves the bad file through would replay all 127 s of drive_01 at
        # 1x and surface as TimeoutExpired mid-table instead of a FAIL line.
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "obd_feed.py"),
             "--replay", DRIVE, "--calibration", p,
             "--speed", "1000", "--run-log", "off"],
            capture_output=True, text=True, timeout=60)
        ok(f"calibration {label}: named refusal, no traceback",
           proc.returncode != 0 and token in proc.stderr
           and "Traceback" not in proc.stderr,
           (proc.stderr or proc.stdout).strip()[:160])

# The seed file must sail through every gate above — the checks exist to
# catch typos, not to make the shipped repo refuse itself. (The bare-cal
# test already proves the gears refusal; this proves nothing ELSE fires
# first on the real file.)
proc = subprocess.run(
    [sys.executable, os.path.join(HERE, "obd_feed.py"),
     "--replay", DRIVE],
    capture_output=True, text=True, timeout=60)
ok("shipped calibration.json: the only refusal is the gears one",
   proc.returncode != 0 and "has no gears.rpm_per_kmh" in proc.stderr
   and "Traceback" not in proc.stderr,
   (proc.stderr or proc.stdout).strip()[:160])

# ── the replay file surface ───────────────────────────────────────────────
# Same class as the learners' log readers: strict utf-8 plus a named refusal,
# so an Excel "Unicode Text" (UTF-16) save or a wrong path dies by name on
# every platform instead of a UnicodeDecodeError traceback mid-iteration.
with tempfile.TemporaryDirectory() as td:
    cal = os.path.join(td, "cal.json")
    with open(cal, "w", encoding="utf-8") as f:
        json.dump({"gears": GOOD_GEARS}, f)
    u16 = os.path.join(td, "drive.csv")
    with open(u16, "wb") as f:
        f.write("t_s,rpm,speed_kmh\n1.0,2000,50\n".encode("utf-16"))
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "obd_feed.py"),
         "--replay", u16, "--calibration", cal, "--run-log", "off"],
        capture_output=True, text=True, timeout=60)
    ok("replay: a UTF-16 drive log refuses by name (re-save as CSV UTF-8)",
       proc.returncode != 0 and "CSV UTF-8" in proc.stderr
       and "Traceback" not in proc.stderr,
       (proc.stderr or proc.stdout).strip()[:160])
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "obd_feed.py"),
         "--replay", os.path.join(td, "no-such.csv"), "--calibration", cal,
         "--run-log", "off"],
        capture_output=True, text=True, timeout=60)
    ok("replay: a missing drive log refuses by name",
       proc.returncode != 0 and "cannot read" in proc.stderr
       and "Traceback" not in proc.stderr,
       (proc.stderr or proc.stdout).strip()[:160])

    # The UTF-16 refusal's remedy is Excel's "CSV UTF-8" — which writes a
    # BOM. The remedy has to actually work, or the refusal is a loop:
    # refused for UTF-16, re-saved as told, refused again for a column name
    # nobody can see the BOM in. utf-8-sig makes the round trip real.
    park = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    park.bind(("127.0.0.1", 0))
    sink = f"127.0.0.1:{park.getsockname()[1]}"
    bom = os.path.join(td, "resaved.csv")
    with open(bom, "w", encoding="utf-8-sig", newline="") as f:
        f.write("t_s,rpm,speed_kmh\n0.0,2000,50\n0.2,2010,50\n")
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "obd_feed.py"),
         "--replay", bom, "--calibration", cal, "--udp", sink,
         "--speed", "1000", "--run-log", "off"],
        capture_output=True, text=True, timeout=60)
    ok("replay: a BOM'd \"CSV UTF-8\" file loads — the remedy round-trips",
       proc.returncode == 0 and "Replaying 2 samples" in proc.stdout
       and "Traceback" not in proc.stderr,
       (proc.stderr or proc.stdout).strip()[:160])

    # A tail log the previous run killed mid-write ends in a truncated row —
    # and feed-last.csv is this README's own replay suggestion. DictReader
    # hands the missing cells over as None; the row must be skipped, not
    # become a TypeError at startup. (Mutation-tested: without TypeError in
    # the replay reader's except, this file kills the feed.)
    cut = os.path.join(td, "cut-mid-write.csv")
    with open(cut, "w", encoding="utf-8", newline="") as f:
        f.write("t_s,rpm,speed_kmh\n0.0,2000,50\n0.1\n0.2,2010,50\n")
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "obd_feed.py"),
         "--replay", cut, "--calibration", cal, "--udp", sink,
         "--speed", "1000", "--run-log", "off"],
        capture_output=True, text=True, timeout=60)
    ok("replay: a truncated row is skipped, not a startup TypeError",
       proc.returncode == 0 and "Replaying 2 samples" in proc.stdout
       and "Traceback" not in proc.stderr,
       (proc.stderr or proc.stdout).strip()[:160])
    park.close()

# "units": null is a hand-editor disabling the section; every other section
# reads null as absent, and this one used to be the lone AttributeError.
ok("units: a null section reads as absent (metric), not a traceback",
   display_units({"units": None}) == "metric")

# Refusals quote paths, and the README quotes refusals — so the default path
# has to arrive without its extractor/../ scaffolding still attached.
ok("repo_path hands out normalized paths",
   os.pardir not in repo_path("calibration.json").split(os.sep),
   repo_path("calibration.json"))

# ---------------------------------------------------------------------------------

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
