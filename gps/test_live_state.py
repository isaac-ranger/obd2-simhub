"""LiveState smoothing tests. Run: python gps/test_live_state.py"""
import io
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gps_overlay import GpsRunLog, LiveState, reader_serial
from nmea import parse_rmc

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


def rmc(lat_dm, lon_dm, sog_kn, cog, hemi_ns="N", hemi_ew="W"):
    # Build a checksummed RMC roughly — use parse after fixing CS.
    body = (f"$GPRMC,120000.000,A,{lat_dm},{hemi_ns},{lon_dm},{hemi_ew},"
            f"{sog_kn},{cog},130826,,,A")
    x = 0
    for ch in body[1:]:
        x ^= ord(ch)
    return parse_rmc(body + "*%02X" % x)


# Stationary noise should not move the published position
s = LiveState()
a = rmc("3248.6613", "11715.0748", "0.0", "10.0")
b = rmc("3248.6700", "11715.0748", "0.0", "10.0")  # ~16 m north if applied raw
s.update_rmc(a)
lat0 = s.snapshot()["lat"]
s.update_rmc(b)
ok("stopped: GPS wander does not move smoothed position",
   abs(s.snapshot()["lat"] - lat0) < 1e-12, f"{s.snapshot()['lat']} vs {lat0}")

# Moving: EMA pulls toward new fix but not all the way in one sample
s2 = LiveState()
s2.update_rmc(rmc("3248.6613", "11715.0748", "3.0", "0.0"))  # ~5.5 km/h
lat_a = s2.snapshot()["lat"]
s2.update_rmc(rmc("3248.6700", "11715.0748", "3.0", "0.0"))
lat_b = s2.snapshot()["lat"]
raw_b = rmc("3248.6700", "11715.0748", "3.0", "0.0")["lat"]
ok("moving: EMA lands between previous and raw",
   lat_a < lat_b < raw_b, f"{lat_a} < {lat_b} < {raw_b}")

# GPS run logs use capture's replay-ready format and OBD's tail/full/off policy.
class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        value = self.now
        self.now += 0.125
        return value


with tempfile.TemporaryDirectory() as td:
    clock = Clock()
    a_log = GpsRunLog(td, "tail", clock=clock)
    ok("runlog tail: nothing touches disk before the first sentence",
       os.listdir(td) == [], f"{os.listdir(td)}")
    a_log.line("$GPRMC,first*00")
    a_log.line("$GPGGA,second*00")
    a_log.close()
    with open(a_log.path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    ok("runlog: capture-compatible timestamp-tab-sentence format",
       lines == ["0.000\t$GPRMC,first*00",
                 "0.125\t$GPGGA,second*00"], f"{lines}")

    ghost = GpsRunLog(td, "tail", clock=clock)
    ghost.close()
    ok("runlog tail: a sampleless run leaves the last log untouched",
       os.listdir(td) == [GpsRunLog.TAIL_NAME], f"{os.listdir(td)}")

    b_log = GpsRunLog(td, "tail", clock=clock)
    b_log.line("$GPRMC,new*00")
    b_log.close()
    ok("runlog tail: previous and current runs both survive",
       sorted(os.listdir(td)) == [GpsRunLog.TAIL_NAME, GpsRunLog.PREV_NAME],
       f"{os.listdir(td)}")
    with open(os.path.join(td, GpsRunLog.PREV_NAME), encoding="utf-8") as f:
        previous = f.read()
    ok("runlog tail: previous contains the old run",
       "$GPRMC,first*00" in previous, previous)

    size_before = os.path.getsize(b_log.path)
    off = GpsRunLog(td, "off", clock=clock)
    off.line("$GPRMC,discard*00")
    off.close()
    ok("runlog off: writes nothing",
       os.path.getsize(b_log.path) == size_before, f"{os.listdir(td)}")

with tempfile.TemporaryDirectory() as td:
    clock = Clock()
    capped = GpsRunLog(td, "tail", clock=clock)
    capped.TAIL_CAP = 180
    for i in range(30):
        capped.line(f"$GPRMC,{i:02d}*00")
    capped.close()
    with open(capped.path, encoding="utf-8") as f:
        kept = f.read().splitlines()
    ok("runlog tail: wrap keeps newest sentences",
       kept[-1].endswith("$GPRMC,29*00"), f"{kept[-3:]}")
    first_kept = int(kept[0].split(",")[1].split("*")[0])
    ok("runlog tail: wrap drops old sentences",
       first_kept > 0, f"first kept={first_kept}")

# The live reader records every framed sentence before parsing, including
# device/vendor messages that the overlay itself does not understand.
with tempfile.TemporaryDirectory() as td:
    raw = (b"$GPPWR,vendor*00\r\n"
           b"$GPRMC,120000.000,A,3248.6613,N,11715.0748,W,"
           b"3.0,0.0,130826,,,A*73\r\n")
    run_log = GpsRunLog(td, "full", clock=Clock())
    reader_serial(io.BytesIO(raw), True, "test", LiveState(),
                  threading.Event(), run_log)
    run_log.close()
    with open(run_log.path, encoding="utf-8") as f:
        recorded = f.read()
    ok("runlog reader: records parsed and unparsed NMEA",
       "$GPPWR,vendor*00" in recorded and "$GPRMC,120000" in recorded,
       recorded)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
