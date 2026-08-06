"""Fixture tests for learn_throttle. Run: python probe/test_learn_throttle.py
No hardware, no files needed beyond this script — drives synthetic logs through
the same functions the CLI uses, plus one round-trip against the real feed-side
mapping in extractor/obd_feed.py so the two can never drift apart silently."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir))
from learn_throttle import (MIN_WALL, ceiling_from_wall, coast_samples,
                            find_wall, idle_samples, load_samples,
                            nearest_rank, residual, rpm_bins,
                            write_calibration)

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


# --- the wall: repetition is what tells a plate stop from a glitch ---------
# The plate PARKS on its top byte under WOT, so the wall is the highest value
# that repeats. A lone higher sample is noise and must not become the ceiling.
walled = [88.6] * MIN_WALL + [50.0] * 20 + [91.0]
wall, n = find_wall(walled)
ok("wall is the highest REPEATED value, not the max", wall == 88.6, f"got {wall}")
ok("wall reports its own sample count", n == MIN_WALL, f"got {n}")

ok("one sample short of MIN_WALL is not a wall",
   find_wall([88.6] * (MIN_WALL - 1) + [50.0] * 20)[0] == 50.0)
ok("nothing repeating enough yields no wall at all",
   find_wall([1.0, 2.0, 3.0])[0] is None)
ok("empty log yields no wall", find_wall([])[0] is None)

# --- the ceiling: margin below the wall, and rounding goes the same way ----
ok("ceiling backs off the wall by the margin",
   ceiling_from_wall(88.6, 4.0) == 85.0, f"got {ceiling_from_wall(88.6, 4.0)}")
ok("rounding is DOWN — margin's direction, not nearest",
   ceiling_from_wall(100.0, 4.0) == 96.0 and ceiling_from_wall(88.69, 4.0) == 85.1)
ok("zero margin sits exactly on the wall", ceiling_from_wall(88.6, 0.0) == 88.6)
ok("a bigger margin gives a lower ceiling",
   ceiling_from_wall(88.6, 8.0) < ceiling_from_wall(88.6, 4.0))


# --- selectors: they key on load/speed/rpm and NEVER on throttle -----------
def row(rpm, speed, thr, load):
    return (rpm, speed, thr, load)


mixed = [
    row(1200, 0.0, 12.5, 20.0),     # idle, stopped
    row(1300, 0.0, 13.0, None),     # idle, load not yet polled
    row(300, 0.0, 99.0, 20.0),      # engine off/Auto-Stop: not idle
    row(2500, 90.0, 16.5, 2.0),     # coasting at speed
    row(2600, 90.0, 88.6, 2.0),     # coasting by load, WOT by throttle
    row(2500, 30.0, 16.0, 2.0),     # unloaded but too slow to count
    row(3000, 90.0, 60.0, 40.0),    # moving under load: neither regime
    row(3000, 90.0, 16.0, None),    # load missing: cannot judge coast
]
coast = coast_samples(mixed)
ok("coast selector takes unloaded-and-moving only", len(coast) == 2, f"got {len(coast)}")
ok("coast selector does NOT read throttle — the 88.6%% row is in",
   88.6 in [t for t, _r in coast])
ok("coast selector skips rows with no load_pct", 16.0 not in [t for t, _r in coast])
ok("coast pairs carry rpm for the per-rpm table",
   sorted(r for _t, r in coast) == [2500, 2600])

idle = idle_samples(mixed)
ok("idle selector takes stopped-and-running only", idle == [12.5, 13.0], f"got {idle}")
ok("idle selector keeps rows with no load_pct (it never asks)", 13.0 in idle)
ok("idle selector drops a stopped engine below IDLE_MIN_RPM", 99.0 not in idle)

# --- percentiles are readings the car produced, not interpolations ---------
vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
ok("nearest-rank p90 is a real sample", nearest_rank(vals, 90) == 9.0)
ok("nearest-rank p10 is a real sample", nearest_rank(vals, 10) == 1.0)
ok("nearest-rank p100 is the max", nearest_rank(vals, 100) == 10.0)
ok("nearest-rank of nothing is None", nearest_rank([], 50) is None)

# --- rpm bins: the limit this model cannot represent, made visible --------
climbing = [(14.0, 1200.0), (14.5, 1400.0), (23.0, 7100.0), (23.5, 7300.0)]
bins = rpm_bins(climbing)
ok("rpm bins are ascending by band", [lo for lo, _m, _n in bins] == [1000, 7000])
ok("rpm bins carry their own sample counts", [n for _lo, _m, n in bins] == [2, 2])
ok("rpm bins show the floor climbing with engine speed",
   bins[-1][1] > bins[0][1])

# --- residual: does the recommendation actually zero the coasting? --------
med, p90, zeros = residual([(16.5, 2000.0), (16.0, 2000.0), (20.0, 2000.0)],
                           16.5, 85.0)
ok("residual zeroes samples at or below the floor", zeros == 2, f"got {zeros}")
ok("residual never goes negative", med == 0.0 and p90 >= 0.0)
top = residual([(200.0, 2000.0)], 16.5, 85.0)
ok("residual clamps above the ceiling to 100", top[0] == 100.0, f"got {top[0]}")

# --- the log reader tolerates real obd_probe output -----------------------
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "log.csv")
    with open(p, "w") as f:
        f.write("t_s,gear,rpm,speed_kmh,throttle_pct,load_pct,coolant_c\n")
        f.write("0.013,0,1204.5,0.0,15.3,,\n")            # slow PIDs not in yet
        f.write("0.5,3,2500.0,90.0,16.5,2.0,88\n")
        f.write("\n")                                      # blank line
        f.write("1.0,3,,90.0,16.5,2.0,88\n")               # dropped rpm read
    s = load_samples(p)
    ok("reader keeps rows whose load_pct is blank", len(s) == 2, f"got {len(s)}")
    ok("reader marks a blank load_pct as None", s[0][3] is None)
    ok("reader drops rows missing a required column value",
       all(r is not None for r, _v, _t, _l in s))

    # --- --write preserves the file it is handed --------------------------
    cal = os.path.join(d, "calibration.json")
    json.dump({"active_set": "street_18", "gears": {"rpm_per_kmh": [103.0]}},
              open(cal, "w"))
    write_calibration(cal, 16.5, 85.0, "some/log.csv", 88.6)
    c = json.load(open(cal))
    ok("write: unrelated sections preserved",
       c["active_set"] == "street_18" and c["gears"]["rpm_per_kmh"] == [103.0])
    ok("write: throttle constants and provenance land",
       c["throttle"]["floor_pct"] == 16.5
       and c["throttle"]["ceiling_pct"] == 85.0
       and c["throttle"]["measured_wall_pct"] == 88.6
       and c["throttle"]["learned_from"] == "some/log.csv")

    bad = os.path.join(d, "bad.json")
    open(bad, "w").write("{not json")
    try:
        write_calibration(bad, 16.5, 85.0, "x.csv", 88.6)
        ok("invalid-JSON target refuses", False, "no SystemExit raised")
    except SystemExit:
        ok("invalid-JSON target refuses and is not clobbered",
           open(bad).read() == "{not json")

# --- the feed applies the same map this tool recommends -------------------
# The learner is only worth running if the feed consumes what it writes. This
# drives the real Packer branch rather than re-implementing the arithmetic.
from extractor.obd_feed import LayoutError, Packer

LAYOUT = {"header": [], "fields": [{"name": "thr", "type": "f32",
                                    "source": "derived:throttle_01"}]}


def fed(raw, cal):
    p = Packer(LAYOUT, cal=cal)
    return p._value(p.fields[0], {"pids": {0x11: raw}, "gear": 0,
                                  "true_speed_kmh": 0.0}, {})


CAL = {"throttle_floor_pct": 16.5, "throttle_ceiling_pct": 85.0}
ok("feed maps the floor to 0", fed(16.5, CAL) == 0.0)
ok("feed maps the ceiling to 1", fed(85.0, CAL) == 1.0)
ok("feed clamps below the floor", fed(0.0, CAL) == 0.0)
ok("feed clamps above the ceiling", fed(88.6, CAL) == 1.0)
ok("feed maps the midpoint to ~0.5", abs(fed(50.75, CAL) - 0.5) < 1e-9)
ok("no throttle section = the identity map it always was",
   fed(37.0, {}) == 0.37 and fed(0.0, {}) == 0.0 and fed(100.0, {}) == 1.0)
# A bad map dies at Packer CONSTRUCTION, not on the first packet: the
# constructor's fail-fast self-test resolves every source once against dummy
# data and turns a LayoutError into a loud exit. obd_feed's main() checks the
# same thing one step earlier so the operator sees a calibration message
# rather than a layout one; this is the backstop under it.
for label, bad_cal in (("inverted", {"throttle_floor_pct": 85.0,
                                     "throttle_ceiling_pct": 16.5}),
                       ("zero-span", {"throttle_floor_pct": 50.0,
                                      "throttle_ceiling_pct": 50.0})):
    try:
        fed(50.0, bad_cal)
        ok(f"a {label} map refuses at construction", False, "no exit raised")
    except (SystemExit, LayoutError):
        ok(f"a {label} map refuses at construction", True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
