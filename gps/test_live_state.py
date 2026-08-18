"""LiveState smoothing tests. Run: python gps/test_live_state.py"""
import io
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gps_overlay import (CRAWL_KMH, GpsRunLog, LiveState, reader_serial,
                         status_line, status_ticker)
from nmea import parse_gga, parse_rmc

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


# True stop: the receiver already pins, and the freeze seconds it.
s = LiveState()
a = rmc("3248.6613", "11715.0748", "0.0", "10.0")
b = rmc("3248.6700", "11715.0748", "0.0", "10.0")  # ~16 m north if applied raw
s.update_rmc(a)
snap0 = s.snapshot()
ok("first fix: raw_lat matches the published (unsmoothed) position",
   snap0["raw_lat"] == snap0["lat"] == a["lat"],
   f"raw={snap0['raw_lat']} lat={snap0['lat']} src={a['lat']}")
lat0 = snap0["lat"]
s.update_rmc(b)
snap1 = s.snapshot()
ok("parked: freeze holds (the receiver would have pinned anyway)",
   abs(snap1["lat"] - lat0) < 1e-12, f"{snap1['lat']} vs {lat0}")
ok("parked: raw_lat still reports the wander the freeze rejected",
   snap1["raw_lat"] == b["lat"] and snap1["raw_lon"] == b["lon"],
   f"raw=({snap1['raw_lat']},{snap1['raw_lon']})")

# Crawl (~0.9 km/h): the receiver releases the pin and scribbles.
# This is the band the freeze actually earns.
c = LiveState()
c.update_rmc(rmc("3248.6613", "11715.0748", "0.5", "10.0"))
clat0 = c.snapshot()["lat"]
c.update_rmc(rmc("3248.6700", "11715.0748", "0.5", "10.0"))
csnap = c.snapshot()
ok("crawl: wander below 2 km/h does not move smoothed position",
   abs(csnap["lat"] - clat0) < 1e-12, f"{csnap['lat']} vs {clat0}")
ok("crawl: raw_lat still reports the scribble",
   csnap["raw_lat"] != clat0, f"raw={csnap['raw_lat']} frozen={clat0}")

# Moving: EMA pulls toward new fix but not all the way in one sample
s2 = LiveState()
s2.update_rmc(rmc("3248.6613", "11715.0748", "3.0", "0.0"))  # ~5.5 km/h
lat_a = s2.snapshot()["lat"]
raw_b_fix = rmc("3248.6700", "11715.0748", "3.0", "0.0")
s2.update_rmc(raw_b_fix)
snap_m = s2.snapshot()
lat_b = snap_m["lat"]
raw_b = raw_b_fix["lat"]
ok("moving: EMA lands between previous and raw",
   lat_a < lat_b < raw_b, f"{lat_a} < {lat_b} < {raw_b}")
ok("moving: raw_lat is the receiver, not the EMA",
   snap_m["raw_lat"] == raw_b and snap_m["raw_lon"] == raw_b_fix["lon"],
   f"raw={snap_m['raw_lat']} expected={raw_b}")

# A dropout must stop claiming the last speed is live, without erasing it.
lost_t = snap_m["t"]
s2.mark_lost("signal lost: ClearCommError")
lost = s2.snapshot()
ok("dropout: ok becomes false", lost["ok"] is False)
ok("dropout: reason is published",
   lost["reason"] == "signal lost: ClearCommError", lost["reason"])
ok("dropout: last pose stays on the payload",
   lost["lat"] == lat_b and lost["raw_lat"] == raw_b,
   f"lat={lost['lat']} raw={lost['raw_lat']}")
ok("dropout: t stays at the last real fix so the browser can age it",
   lost["t"] == lost_t, f"{lost['t']} vs {lost_t}")
s2.update_rmc(rmc("3248.6800", "11715.0748", "3.0", "0.0"))
ok("dropout: a later fix restores ok and clears the reason",
   s2.snapshot()["ok"] is True and s2.snapshot()["reason"] is None,
   f"{s2.snapshot()}")

# crawl: the freeze decision, published. One comparison in the server;
# the browser reads this instead of keeping a threshold of its own.
ok("crawl: null before the first fix (no opinion yet, like sats)",
   LiveState().snapshot()["crawl"] is None,
   f"{LiveState().snapshot()['crawl']!r}")
ok("crawl: parked (0 km/h) publishes true", snap1["crawl"] is True,
   f"{snap1['crawl']!r}")
ok("crawl: the scribble band (~0.9 km/h) publishes true",
   csnap["crawl"] is True, f"{csnap['crawl']!r}")
ok("crawl: moving (~5.6 km/h) publishes false", snap_m["crawl"] is False,
   f"{snap_m['crawl']!r}")
ok("crawl: survives a dropout with the last value, like the pose",
   lost["crawl"] is False, f"{lost['crawl']!r}")

# The boundary, both sides, and the promise that matters: crawl is the SAME
# decision that freezes the EMA, not a second one that could drift from it.
# Feed a fix ~16 m away at each speed; frozen <=> the EMA did not move.
here = rmc("3248.6613", "11715.0748", "0.0", "10.0")
there = rmc("3248.6700", "11715.0748", "0.0", "10.0")


def at_kmh(base, kmh):
    f = dict(base)
    f["speed_kmh"] = kmh
    return f


for kmh in (CRAWL_KMH - 0.5, CRAWL_KMH - 0.01, CRAWL_KMH,
            CRAWL_KMH + 0.01, CRAWL_KMH + 0.5):
    b = LiveState()
    b.update_rmc(at_kmh(here, kmh))
    lat_before = b.snapshot()["lat"]
    b.update_rmc(at_kmh(there, kmh))
    bs = b.snapshot()
    frozen = abs(bs["lat"] - lat_before) < 1e-12
    expect_crawl = kmh < CRAWL_KMH
    ok(f"crawl@{kmh:.2f} km/h: published {str(expect_crawl).lower()}",
       bs["crawl"] is expect_crawl, f"crawl={bs['crawl']!r}")
    ok(f"crawl@{kmh:.2f} km/h: the EMA agrees (frozen iff crawl)",
       frozen == bs["crawl"], f"frozen={frozen} crawl={bs['crawl']!r}")

# The heading hold reads the same decision: seeded from the first COG even
# when parked, then only followed while not crawling.
h = LiveState()
h.update_rmc(at_kmh(here, 0.0))
ok("heading: seeded from the first COG even while crawling",
   h.snapshot()["heading_deg"] == 10.0, f"{h.snapshot()['heading_deg']}")
turned = dict(there)
turned["course_deg"] = 200.0
h.update_rmc(at_kmh(turned, CRAWL_KMH - 0.01))
ok("heading: held while crawl (COG 200 ignored below the line)",
   h.snapshot()["heading_deg"] == 10.0, f"{h.snapshot()['heading_deg']}")
h.update_rmc(at_kmh(turned, CRAWL_KMH))
ok("heading: follows COG at the line and above (crawl false)",
   h.snapshot()["heading_deg"] == 200.0, f"{h.snapshot()['heading_deg']}")

# What actually goes over the wire: the field is named crawl and is a JSON
# boolean, not a string or a number the page would have to interpret.
wire = json.loads(json.dumps(snap_m))
ok("crawl: /live payload carries a JSON boolean named crawl",
   wire.get("crawl") is False and "crawl" in wire, f"{wire.get('crawl')!r}")
wire_parked = json.loads(json.dumps(snap1))
ok("crawl: /live payload says true when parked",
   wire_parked.get("crawl") is True, f"{wire_parked.get('crawl')!r}")

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

# The Windows Bluetooth goodbye: read() raises, the thread used to die
# with a traceback, and /live kept serving ok:true at the last speed.
def sentence_bytes(body):
    x = 0
    for ch in body[1:]:
        x ^= ord(ch)
    return (body + "*%02X\r\n" % x).encode("ascii")


RMC_LINE = sentence_bytes(
    "$GPRMC,120000.000,A,3248.6613,N,11715.0748,W,3.0,0.0,130826,,,A")


class DropAfter:
    def __init__(self, payload):
        self.payload = payload
        self.n = 0

    def read(self, _n):
        self.n += 1
        if self.n == 1:
            return self.payload
        raise OSError("ClearCommError failed (Access is denied.)")

    def close(self):
        pass


drop_state = LiveState()
reader_serial(DropAfter(RMC_LINE), False, "COM5", drop_state,
              threading.Event())
drop = drop_state.snapshot()
ok("reader: a SerialException-shaped OSError marks the fix lost",
   drop["ok"] is False and "signal lost" in (drop["reason"] or ""),
   f"{drop}")
ok("reader: the last pose survives the drop",
   drop["lat"] is not None and drop["speed_kmh"] > 0,
   f"lat={drop['lat']} speed={drop['speed_kmh']}")


class QuietThenStop:
    """A COM timeout: empty read is a quiet quarter-second, not goodbye."""

    def __init__(self, stop):
        self.stop = stop

    def read(self, _n):
        self.stop.set()
        return b""

    def close(self):
        pass


quiet_stop = threading.Event()
quiet_state = LiveState()
quiet_state.update_rmc(rmc("3248.6613", "11715.0748", "3.0", "0.0"))
reader_serial(QuietThenStop(quiet_stop), False, "COM5", quiet_state,
              quiet_stop)
ok("reader: an empty timeout does not mark the fix lost",
   quiet_state.snapshot()["ok"] is True
   and quiet_state.snapshot()["reason"] is None,
   f"{quiet_state.snapshot()}")

eof_state = LiveState()
reader_serial(io.BytesIO(RMC_LINE), True, "test", eof_state,
              threading.Event())
ok("reader: a file-door EOF marks the fix lost",
   eof_state.snapshot()["ok"] is False
   and eof_state.snapshot()["reason"] == "signal lost: source closed"
   and eof_state.snapshot()["lat"] is not None,
   f"{eof_state.snapshot()}")

# The status line: one per second on stdout, for a human in the paddock and
# for the supervisor, whose liveness rule is "the child printed a line this
# second". Every data field on it must come from the SAME snapshot /live
# serves. A ticker keeping its own fix counter or last-fix stamp is a second
# opinion about whether the GPS is alive; these tests are built to catch one.
class WallClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def gga(sats, hdop):
    body = (f"$GPGGA,120000.000,3248.6613,N,11715.0748,W,1,{sats:02d},{hdop},"
            f"15.0,M,,M,,")
    x = 0
    for ch in body[1:]:
        x ^= ord(ch)
    return parse_gga(body + "*%02X" % x)


wall = WallClock(1000.0)
sl = LiveState(clock=wall)
line0 = status_line(sl.snapshot(), now=1003.0, uptime_s=3.0)
ok("status: before the first fix the line still prints, and says waiting",
   line0.startswith("  t     3s  GPS waiting") and "age      -" in line0, line0)
ok("status: no fix means no crawl opinion (- not no)",
   "crawl -" in line0, line0)
ok("status: greppable columns share the feed's leading t-column shape",
   line0.split()[:2] == ["t", "3s"], line0)

# The age is now minus the fix's OWN stamp: the ticker never saw an update
# event here, only the state — a mutant that timestamps updates it observes
# cannot print 12.5 from a fix it did not witness.
sl.update_rmc(rmc("3248.6613", "11715.0748", "24.5", "187.0"), source="COM5")
sl.update_gga(gga(9, "1.2"))
sl.update_rmc(rmc("3248.6613", "11715.0748", "24.5", "187.0"), source="COM5")
line1 = status_line(sl.snapshot(), now=1012.5, uptime_s=17.0)
ok("status: ok state names itself", "GPS ok" in line1, line1)
ok("status: age is now minus the fix's own stamp (12.5 s here)",
   "age  12.5s" in line1, line1)
ok("status: speed is the published speed_kmh, one decimal",
   "speed  45.4 km/h" in line1, line1)
ok("status: crawl no while moving, sats and accuracy from the same snapshot",
   "crawl no " in line1 and "sats  9" in line1 and "acc  6.0 m" in line1,
   line1)
ok("status: no reason means no trailing parenthesis",
   not line1.rstrip().endswith(")"), line1)

# ok:false shows in the line, with the reason, and the age keeps climbing.
sl.mark_lost("signal lost: ClearCommError")
lost1 = status_line(sl.snapshot(), now=1012.5, uptime_s=17.0)
lost2 = status_line(sl.snapshot(), now=1131.7, uptime_s=136.0)
ok("status: a dropout prints LOST, not ok", "GPS LOST" in lost1
   and "GPS ok" not in lost1, lost1)
ok("status: the reason rides on the line",
   lost1.endswith("(signal lost: ClearCommError)"), lost1)
ok("status: LOST keeps the last speed on the line, like /live keeps the pose",
   "speed  45.4 km/h" in lost1, lost1)
ok("status: after the loss the age keeps climbing (silence is not quiet)",
   "age 131.7s" in lost2, lost2)

# A goodbye before any fix (source closes on the first read) is LOST too —
# reason is the authority, not the stamp.
early = LiveState(clock=wall)
early.mark_lost("signal lost: source closed")
line_e = status_line(early.snapshot(), now=1002.0, uptime_s=2.0)
ok("status: lost before the first fix says LOST with no age",
   "GPS LOST" in line_e and "age      -" in line_e, line_e)

# The ticker: rate is the interval, not the fix rate. 200 fixes in a burst
# during a short window must yield a handful of lines, not 200 — and each
# line is read from the state at tick time, so a change made by another
# thread with no signal to the ticker shows on the next line.
tick_state = LiveState()
tick_stop = threading.Event()
tick_out = io.StringIO()
tick_thread = threading.Thread(
    target=status_ticker,
    args=(tick_state, tick_stop, tick_out, 0.02), daemon=True)
tick_thread.start()
fast = rmc("3248.6613", "11715.0748", "3.0", "0.0")      # 5.6 km/h
for _ in range(200):
    tick_state.update_rmc(fast)
time.sleep(0.15)
tick_state.update_rmc(rmc("3248.6700", "11715.0748", "30.0", "0.0"))  # 55.6
time.sleep(0.15)
tick_state.mark_lost("signal lost: bench")
time.sleep(0.15)
tick_stop.set()
tick_thread.join(timeout=2.0)
tick_lines = tick_out.getvalue().splitlines()
n_lines = len(tick_lines)
ok("ticker: rate is per interval, not per fix (200 fixes, a handful of lines)",
   3 <= n_lines <= 60, f"{n_lines} lines")
ok("ticker: every line is a status line",
   all(ln.startswith("  t ") and " GPS " in ln for ln in tick_lines),
   f"{tick_lines[:2]}")
ok("ticker: an update it was never told about shows on a later line",
   any("speed   5.6 km/h" in ln for ln in tick_lines)
   and any("speed  55.6 km/h" in ln for ln in tick_lines),
   f"{tick_lines}")
ok("ticker: a dropout it was never told about shows as LOST",
   any("GPS LOST" in ln and "(signal lost: bench)" in ln for ln in tick_lines),
   f"{tick_lines[-3:]}")
ok("ticker: after the drop it keeps printing (silence is what gets punished)",
   sum("GPS LOST" in ln for ln in tick_lines) >= 2, f"{tick_lines[-4:]}")
ok("ticker: stop ends it", not tick_thread.is_alive())

# The ticker's age is the snapshot's stamp against the injected wall clock,
# not a stamp the ticker took when it first noticed the fix. Fix at 1000,
# ticker's wall at 1030 -> 30.0 s, on the ticker's own line.
age_wall = WallClock(1000.0)
age_state = LiveState(clock=age_wall)
age_state.update_rmc(rmc("3248.6613", "11715.0748", "0.5", "10.0"))  # crawl
age_wall.now = 1030.0
age_stop = threading.Event()
age_out = io.StringIO()
age_thread = threading.Thread(
    target=status_ticker,
    args=(age_state, age_stop, age_out, 0.02, age_wall), daemon=True)
age_thread.start()
time.sleep(0.12)
age_stop.set()
age_thread.join(timeout=2.0)
age_lines = age_out.getvalue().splitlines()
ok("ticker: the age on its line is the fix stamp against the wall clock (30.0 s)",
   age_lines and all("age  30.0s" in ln for ln in age_lines), f"{age_lines[:2]}")
ok("ticker: crawl yes rides the line while parked",
   age_lines and all("crawl yes" in ln for ln in age_lines), f"{age_lines[:1]}")


# stdout gone: the pulse must not fail toward silence. The ticker says so on
# stderr and hands main() its Ctrl-C path, instead of dying quietly while
# the process keeps serving.
class DeadPipe(io.StringIO):
    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")


import _thread
interrupted = []
_real_interrupt = _thread.interrupt_main
_thread.interrupt_main = lambda *a, **k: interrupted.append(True)
try:
    dead_stop = threading.Event()
    dead_err = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = dead_err
    try:
        status_ticker(LiveState(), dead_stop, DeadPipe(), 0.01)
    finally:
        sys.stderr = real_stderr
finally:
    _thread.interrupt_main = _real_interrupt
ok("ticker: a dead stdout ends the ticker instead of raising through it",
   True)
ok("ticker: a dead stdout interrupts main (fails loud, not silent)",
   interrupted == [True], f"{interrupted}")
ok("ticker: a dead stdout is named on stderr",
   "stdout failed" in dead_err.getvalue(), dead_err.getvalue())

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
