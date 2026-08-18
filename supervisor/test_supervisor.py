"""Tests for the phase 3 supervisor. Run: python supervisor/test_supervisor.py

Covers the liveness pattern against the feed's real print format, the
speakable summary lines, exit-reason extraction, and — the part that
matters — four end-to-end runs against stub feeds that fail in the four
ways a real one does: never starts, runs clean, dies mid-drive, dies
instantly and repeatedly.

The point of the supervisor is recovery from failures we did not predict,
so the tests inject failures rather than assert on internal state. Stdlib
only, no car, no adapter.

The two-leg block at the end runs the supervisor with --gps against stub
overlays that print the REAL status line (built by gps_overlay.status_line
itself, so a format change breaks a test rather than blinding the
supervisor): both up, GPS lost, GPS crawling, GPS wedged, OBD wedged
beside a healthy GPS. The per-child policy is the thing under test — the
MX+ gets killed for wedging, the GPS gets believed when it says LOST.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from supervisor import (SAMPLE_LINE, GPS_LINE, GPS_FIELDS, Status, GpsStatus,
                        FeedProcess, LogSink, human_duration)
sys.path.insert(0, os.path.join(HERE, os.pardir, "gps"))
from gps_overlay import status_line as gps_status_line   # the REAL line shape

REPO = os.path.join(HERE, os.pardir)
SUP = os.path.join(HERE, "supervisor.py")
WINDOWS = os.name == "nt"

FAILED = []


def ok(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(name)


# --- stub feeds -------------------------------------------------------------------
# Each stands in for obd_feed.py failing a particular way.

STUB_ALIVE = """
import sys, time
n = 0
while True:
    n += 1
    print(f"  t {n:5.0f}s  RPM {2000:5.0f}  speed 40 km/h (true)  gear 3  "
          f"poll  5.0 Hz  udp {n*60} pkts", flush=True)
    time.sleep(0.1)
"""

STUB_DIES_MID_DRIVE = """
import sys, time
for n in range(1, 4):
    print(f"  t {n:5.0f}s  RPM {2000:5.0f}  speed 40 km/h (true)  gear 3  "
          f"poll  5.0 Hz  udp {n*60} pkts", flush=True)
    time.sleep(0.1)
print("")
print("25 consecutive failed samples - the car has left the conversation. "
      "Log kept: runs/x.csv", flush=True)
print("Power-cycle the adapter (unplug/replug), re-pair if needed, and rerun.",
      flush=True)
sys.exit(1)
"""

STUB_NO_ADAPTER = """
import sys
sys.exit("could not open port 'COM3': FileNotFoundError")
"""

STUB_SILENT = """
import time
time.sleep(30)
"""

STUB_GOES_QUIET = """
import time
for n in range(1, 4):
    print(f"  t {n:5.0f}s  RPM {2000:5.0f}  speed 40 km/h (true)  gear 3  "
          f"poll  5.0 Hz  udp {n*60} pkts", flush=True)
    time.sleep(0.1)
time.sleep(30)
"""


# --- stub overlays ------------------------------------------------------------------
# Each prints the REAL status line: gps_overlay.status_line() from a snapshot,
# exactly as the ticker does, once per 0.1 s. What differs is the snapshot.
GPS_STUB_HEAD = """
import os, sys, time
sys.path.insert(0, %r)
from gps_overlay import status_line
t0 = time.monotonic()
def tick(snap, n):
    now = time.time()
    if snap.get("t") == "fresh":
        snap = dict(snap, t=now - 0.1)
    print(status_line(snap, now, time.monotonic() - t0), flush=True)
    time.sleep(0.1)
""" % os.path.join(HERE, os.pardir, "gps")

GPS_STUB_OK = GPS_STUB_HEAD + """
n = 0
while True:
    n += 1
    tick({"ok": True, "t": "fresh", "speed_kmh": 45.2, "crawl": False,
          "sats": 9, "accuracy_m": 3.1}, n)
"""

GPS_STUB_CRAWL = GPS_STUB_HEAD + """
n = 0
while True:
    n += 1
    tick({"ok": True, "t": "fresh", "speed_kmh": 0.8, "crawl": True,
          "sats": 10, "accuracy_m": 2.4}, n)
"""

# Two good seconds, then the receiver leaves: LOST with a reason, every
# second, forever — the process is fine and says so.
GPS_STUB_LOST = GPS_STUB_HEAD + """
n = 0
lost_at = None
while True:
    n += 1
    if n <= 20:
        tick({"ok": True, "t": "fresh", "speed_kmh": 45.2, "crawl": False,
              "sats": 9, "accuracy_m": 3.1}, n)
    else:
        lost_at = lost_at or time.time()
        tick({"ok": False, "t": lost_at - 0.1, "speed_kmh": 45.2, "crawl": False,
              "sats": 9, "accuracy_m": 3.1, "reason": "signal lost: ClearCommError"}, n)
"""

# The silent-port shape the line documents: ok, and an age that only climbs.
GPS_STUB_STALE_OK = GPS_STUB_HEAD + """
n = 0
fixed_at = time.time() - 120
while True:
    n += 1
    tick({"ok": True, "t": fixed_at, "speed_kmh": 45.2, "crawl": False,
          "sats": 9, "accuracy_m": 3.1}, n)
"""

GPS_STUB_WAITING = GPS_STUB_HEAD + """
n = 0
while True:
    n += 1
    tick({"ok": False, "t": 0.0}, n)
"""

# Two good seconds, then nothing at all: this is what a wedged overlay
# looks like from outside, and the one shape the GPS leg IS killed for.
GPS_STUB_GOES_SILENT = GPS_STUB_HEAD + """
n = 0
while n < 20:
    n += 1
    tick({"ok": True, "t": "fresh", "speed_kmh": 45.2, "crawl": False,
          "sats": 9, "accuracy_m": 3.1}, n)
time.sleep(30)
"""


def write_stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(body)
    return p


# Every spawned supervisor gets this fixture so the suite never reads a
# real config.json in the repo root — a user's own settings (say,
# max_restarts) would otherwise fail assertions that have nothing to do
# with the code. Found by an adversarial QA pass, kept as a rule: tests
# pin their config.
CFG_FIXTURE = os.path.join(tempfile.mkdtemp(prefix="sup_test_cfg_"), "config.json")
with open(CFG_FIXTURE, "w", encoding="utf-8") as _f:
    _f.write("{}\n")


def run_supervisor(tmp, stub, extra=(), settle=2.0, config=None, gps_stub=None):
    """Launch the supervisor on a stub feed (and, with gps_stub, a stub overlay
    as the second child), let it settle, return (proc, status_path)."""
    status_path = os.path.join(tmp, "obd2_status.json")
    argv = [sys.executable, "-u", SUP,
            "--config", config or CFG_FIXTURE,
            "--status-file", status_path,
            "--status-interval", "0.15",
            "--stall-seconds", "1",
            "--backoff-start", "0.3",
            "--backoff-max", "0.5",
            "--healthy-seconds", "0.4",
            "--log-dir", os.path.join(tmp, "runs"),
            "--feed", stub,
            "--quiet"] + list(extra)
    if gps_stub:
        argv += ["--gps", "--gps-overlay", gps_stub]
    # Windows: a parent cannot deliver SIGINT. The only graceful stop it has is
    # CTRL_BREAK_EVENT, and that requires the child to own a process group.
    extra_kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, **extra_kw)
    time.sleep(settle)
    return proc, status_path


def read_status(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def stop(proc):
    # Ask for the graceful path, not a kill. terminate() would end the supervisor
    # without running its shutdown — leaving the stub feed it spawned orphaned, and
    # leaving these four e2e cases testing nothing but that a process can be killed.
    if proc.poll() is None:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        except (ValueError, OSError):
            proc.terminate()
    try:
        return proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()[0]


# --- unit: the liveness pattern ---------------------------------------------------
print("\nliveness pattern")

# Built exactly as obd_feed.poll_car builds it, so a change to that format
# breaks this test rather than silently blinding the supervisor.
t, rpm, n = 42.0, 2100.0, 210
real = (f"  t {t:5.0f}s  RPM {rpm:5.0f}  speed 40 km/h (true)  gear 3  "
        f"poll {n / t:4.1f} Hz  udp {n} pkts")
ok("matches the feed's real status line", bool(SAMPLE_LINE.match(real)), real.strip()[:40])
ok("matches at t=0", bool(SAMPLE_LINE.match("  t     0s  RPM     0  speed ...")))
ok("matches a long run (5-digit t)",
   bool(SAMPLE_LINE.match("  t 10800s  RPM  3400  speed ...")))
ok("ignores the 25-miss banner",
   not SAMPLE_LINE.match("25 consecutive failed samples - the car has left"))
ok("ignores the tune output",
   not SAMPLE_LINE.match("  keeping: batch-4 (5.1 req/s)"))
ok("ignores the replay banner",
   not SAMPLE_LINE.match("Replaying 1200 samples from drive.csv at 1x..."))

# --- unit: speakable durations ----------------------------------------------------
print("\nspoken durations")
ok("singular second", human_duration(1) == "1 second", human_duration(1))
ok("seconds", human_duration(42) == "42 seconds", human_duration(42))
ok("singular minute", human_duration(60) == "1 minute", human_duration(60))
ok("minutes", human_duration(742) == "12 minutes", human_duration(742))
ok("whole hour has no dangling minutes", human_duration(3600) == "1 hour",
   human_duration(3600))
ok("hours and minutes", human_duration(3600 * 2 + 660) == "2h 11m",
   human_duration(3600 * 2 + 660))

# --- unit: the status payload -----------------------------------------------------
print("\nstatus payload")
st = Status(os.path.join(tempfile.mkdtemp(), "s.json"), stale_after_s=10, replay=False)
snap = st.snapshot()
for key in ("schema", "state", "healthy", "summary", "updated_at",
            "updated_unix", "stale_after_s", "detail"):
    ok(f"carries {key}", key in snap)
ok("starts STARTING", snap["state"] == "STARTING", snap["state"])
ok("starting is not healthy", snap["healthy"] is False)
ok("stale_after_s is present and positive", snap["stale_after_s"] > 0,
   str(snap["stale_after_s"]))

st.set_state("LIVE")
st.saw_sample()
live = st.snapshot()
ok("live is healthy", live["healthy"] is True)
ok("live summary names the car, not a state code",
   "car is talking" in live["summary"].lower(), live["summary"])
ok("live summary is one speakable sentence",
   live["summary"].endswith(".") and "\n" not in live["summary"])
ok("seconds_since_data is populated once data arrives",
   live["detail"]["seconds_since_data"] is not None)

st.restarts = 2
ok("summary mentions recovery when there was any",
   "2 interruptions" in st.snapshot()["summary"], st.snapshot()["summary"])

rep = Status("/tmp/x.json", stale_after_s=10, replay=True)
rep.set_state("LIVE")
ok("replay mode says replay, not 'the car'",
   "replaying" in rep.summary().lower(), rep.summary())

for state in ("STARTING", "STALLED", "NO_ADAPTER", "RECONNECTING", "STOPPED"):
    st.set_state(state)
    s = st.summary()
    ok(f"{state} has a spoken sentence", len(s) > 15 and s.endswith((".", "!")), s[:48])
    ok(f"{state} is not healthy", st.healthy() is False)

# --- unit: exit reasons -----------------------------------------------------------
print("\nexit reasons")
fp = FeedProcess([], None, st)
fp.tail = ["  t    12s  RPM  2000  ...",
           "",
           "25 consecutive failed samples - the car has left the conversation. "
           "Log kept: runs/2026-08-01.csv",
           "Power-cycle the adapter (unplug/replug), re-pair if needed, and rerun."]
ok("prefers the reason over the last line",
   "consecutive failed samples" in fp.exit_reason(), fp.exit_reason()[:44])
ok("25-miss is not read as a missing adapter", not fp.looks_like_no_adapter())

fp2 = FeedProcess([], None, st)
fp2.tail = ["could not open port 'COM3': FileNotFoundError"]
ok("spots a missing adapter", fp2.looks_like_no_adapter())
ok("quotes the adapter error", "COM3" in fp2.exit_reason(), fp2.exit_reason())

fp3 = FeedProcess([], None, st)
fp3.tail = ["  t    12s  RPM  2000  ...", ""]
ok("falls back to the last real line when nothing explains itself",
   fp3.exit_reason().startswith("t "), fp3.exit_reason())
ok("no tail at all is survivable", FeedProcess([], None, st).exit_reason() == "")

# --- end to end: a feed that just works -------------------------------------------
print("\nend to end: healthy feed")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE))
s = read_status(sp)
ok("reaches LIVE against a working feed", s["state"] == "LIVE", s["state"])
ok("reports healthy", s["healthy"] is True)
ok("counts samples", s["detail"]["samples_seen"] > 0, str(s["detail"]["samples_seen"]))
ok("no restarts on a clean run", s["detail"]["restarts"] == 0)
ok("records the feed pid while running", s["detail"]["feed_pid"] is not None)
ok("summary would mean something spoken aloud",
   "flowing" in s["summary"], s["summary"])
t0 = s["updated_unix"]
time.sleep(0.6)
ok("keeps updating (the file is a heartbeat, not a snapshot)",
   read_status(sp)["updated_unix"] >= t0)

stop(proc)
final = read_status(sp)
ok("says STOPPED after a deliberate shutdown", final["state"] == "STOPPED", final["state"])
ok("STOPPED is not healthy", final["healthy"] is False)
ok("clears the pid on the way out", final["detail"]["feed_pid"] is None)
ok("leaves no half-written temp file behind",
   not os.path.exists(sp + ".tmp"))

# --- end to end: dies mid-drive, comes back ---------------------------------------
print("\nend to end: dies mid-drive")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "dies.py", STUB_DIES_MID_DRIVE),
                          settle=2.5)
s = read_status(sp)
ok("restarts a feed that exited", s["detail"]["restarts"] >= 1,
   f"{s['detail']['restarts']} restarts")
ok("remembers why it died", s["detail"]["last_exit"] is not None)
ok("the recorded reason is the feed's own words",
   "consecutive failed samples" in (s["detail"]["last_exit"] or {}).get("reason", ""),
   (s["detail"]["last_exit"] or {}).get("reason", "")[:44])
ok("a 25-miss exit is NOT reported as a missing adapter",
   s["state"] != "NO_ADAPTER", s["state"])
ok("records the restart time", s["detail"]["last_restart_at"] is not None)
ok("state is one the driver can act on",
   s["state"] in ("LIVE", "RECONNECTING", "STARTING", "STALLED"), s["state"])
time.sleep(1.5)
s2 = read_status(sp)
ok("keeps restarting — it does not give up after one",
   s2["detail"]["restarts"] > s["detail"]["restarts"],
   f"{s['detail']['restarts']} -> {s2['detail']['restarts']}")
ok("recovers to LIVE between deaths",
   s2["detail"]["samples_seen"] > s["detail"]["samples_seen"] or s2["state"] == "LIVE",
   s2["state"])
stop(proc)

# --- end to end: no adapter -------------------------------------------------------
print("\nend to end: no adapter")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "noad.py", STUB_NO_ADAPTER), settle=2.0)
# The state cycles STARTING -> NO_ADAPTER every backoff; one read on a loaded
# machine can land on the STARTING half. Poll to a deadline, as the wedge
# test does, so a slow box proves the same thing a fast one does.
deadline = time.time() + 5
s = read_status(sp)
while time.time() < deadline and s["state"] != "NO_ADAPTER":
    time.sleep(0.1)
    s = read_status(sp)
ok("reports NO_ADAPTER, not a generic reconnect",
   s["state"] == "NO_ADAPTER", s["state"])
ok("tells the driver what to physically do",
   "plugged in" in s["summary"], s["summary"])
ok("still retrying — a missing adapter is not fatal",
   s["detail"]["restarts"] >= 1, str(s["detail"]["restarts"]))
before = s["detail"]["restarts"]
time.sleep(1.2)
ok("keeps retrying so a later plug-in is picked up with no keyboard",
   read_status(sp)["detail"]["restarts"] > before)
stop(proc)

# --- end to end: alive but silent -------------------------------------------------
print("\nend to end: running but wedged")
tmp = tempfile.mkdtemp()
# --stall-restart-seconds 0 is the report-only contract from before the
# stall-kill existed; this run is also its only coverage. The wedged e2e
# below kills an identical silence at 2 seconds, so surviving 2.5s here is
# proof the 0 means "never", not "instantly".
proc, sp = run_supervisor(tmp, write_stub(tmp, "silent.py", STUB_SILENT),
                          extra=("--stall-restart-seconds", "0"), settle=2.5)
s = read_status(sp)
ok("a live-but-silent feed reads STALLED, not LIVE", s["state"] == "STALLED", s["state"])
ok("STALLED is not healthy", s["healthy"] is False)
ok("never-answered is worded differently from went-quiet",
   "not answered yet" in s["summary"], s["summary"])
ok("and it gives the right advice for that case",
   "ignition" in s["summary"], s["summary"])
ok("stall-restart 0 = report only: the process is watched, never killed",
   s["detail"]["feed_pid"] is not None and s["detail"]["restarts"] == 0,
   f"pid {s['detail']['feed_pid']}, {s['detail']['restarts']} restarts")
stop(proc)

# The other STALLED shape: data flowed, then stopped, process still up.
# That is a Bluetooth drop, and it needs different words from "never started".
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "quiet.py", STUB_GOES_QUIET), settle=2.5)
s = read_status(sp)
ok("data-then-silence also reads STALLED", s["state"] == "STALLED", s["state"])
ok("went-quiet is worded as a loss, not a no-show",
   "no data has arrived for" in s["summary"], s["summary"])
ok("it says how long the data has been gone",
   s["detail"]["seconds_since_data"] > 1, str(s["detail"]["seconds_since_data"]))
ok("it remembers data did once flow", s["detail"]["samples_seen"] == 3,
   str(s["detail"]["samples_seen"]))
ok("last_data_at survives the silence", s["detail"]["last_data_at"] is not None)
stop(proc)

# --- end to end: wedged past the restart budget -----------------------------------
# The field failure of 2026-08-02: the MX+ lost power mid-drive, Windows kept
# the COM handle alive, and the feed blocked in a serial write that never
# returns and never raises. Report-only leaves that state forever — the wedged
# process owns the dead port, so nothing recovers until something reopens it.
# Past --stall-restart-seconds the supervisor must be that something.
print("\nend to end: wedged past the restart budget")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "wedged.py", STUB_GOES_QUIET),
                          extra=("--stall-restart-seconds", "2"), settle=5.0)
# The kill itself is deterministic; the RECOVERY is at the mercy of the
# machine's process-spawn latency (a cold Windows box with an antivirus can
# spend seconds just starting Python). Poll up to a deadline rather than
# trusting one fixed settle, so a slow machine proves the same thing a fast
# one does instead of failing the last assertion.
deadline = time.time() + 20
s = read_status(sp)
while time.time() < deadline and not (
        s["detail"]["restarts"] >= 1 and s["detail"]["samples_seen"] > 3):
    time.sleep(0.3)
    s = read_status(sp)
ok("a wedged feed is killed and restarted, not watched forever",
   s["detail"]["restarts"] >= 1, f"{s['detail']['restarts']} restarts")
ok("the recorded reason is the supervisor's own judgement",
   "wedged" in ((s["detail"]["last_exit"] or {}).get("reason") or ""),
   (s["detail"]["last_exit"] or {}).get("reason", "")[:60])
ok("the fresh run reached data again after the kill",
   s["detail"]["samples_seen"] > 3, str(s["detail"]["samples_seen"]))
stop(proc)

# --- a fresh run is judged from its own birth -------------------------------------
# The per-run guard: after a stall-kill, the fresh feed's FIRST answer takes a
# while (a real reconnect spends ~12 silent seconds in adapter reset and
# autotune). Judged from its own birth it reaches data; judged from its
# ancestor's last words it inherits the silence and is killed at the starting
# line, every time. The ship-qa pass found the guard had no witness — a mutant
# without it stayed green — so this run says whether it is still there.
print("\nend to end: a slow-starting fresh run is not killed at the starting line")
STUB_SLOW_THEN_WEDGE = """
import time
time.sleep(1.5)                       # the reconnect: adapter reset, autotune
for n in range(1, 4):
    print(f"  t {n:5.0f}s  RPM  2000  speed 40 km/h (true)  gear 3  "
          f"poll  5.0 Hz  udp {n*60} pkts", flush=True)
    time.sleep(0.1)
time.sleep(60)                        # then it wedges
"""
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "slow_wedge.py", STUB_SLOW_THEN_WEDGE),
                          extra=("--stall-restart-seconds", "2.5"), settle=10.5)
s = read_status(sp)
ok("each fresh run gets its own stall budget and reaches data",
   s["detail"]["samples_seen"] >= 6, f"{s['detail']['samples_seen']} samples")
ok("so it is restarted for wedging, not for being born slow",
   s["detail"]["restarts"] <= 2, f"{s['detail']['restarts']} restarts in 10.5 s")
stop(proc)

# --- the staleness contract -------------------------------------------------------
print("\nlog sink — the disk log in three sizes")
tmp = tempfile.mkdtemp()
a = LogSink(tmp, "tail")
a.write("first session\n")
a.close()
b = LogSink(tmp, "tail")
b.write("second session\n")
b.close()
ok("tail: the previous session survives one generation",
   sorted(os.listdir(tmp)) == ["supervisor-last.log", "supervisor-prev.log"],
   f"{os.listdir(tmp)}")
with open(b.path) as f:
    content = f.read()
ok("tail: last holds the new session", content == "second session\n",
   repr(content))
with open(os.path.join(tmp, LogSink.PREV)) as f:
    prev_content = f.read()
ok("tail: prev holds the one before — a restart no longer erases its "
   "own reason", prev_content == "first session\n", repr(prev_content))

c = LogSink(tmp, "tail")
c.CAP = 600                          # shrink the cap to test the wrap
for i in range(60):
    c.write(f"line {i:02d}: something the feed said\n")
c.close()
with open(c.path) as f:
    lines = f.read().splitlines()
data = [ln for ln in lines if ln.startswith("line ")]
first_n = int(data[0].split()[1].rstrip(":"))
ok("tail: the wrap drops the OLD half", first_n >= 10,
   f"first surviving line is {first_n} (0 = keep-oldest)")
ok("tail: the wrap says so out loud",
   any("wrapped" in ln for ln in lines), f"{lines[:1]}")
ok("tail: the newest lines survive", data[-1].startswith("line 59"),
   data[-1])
ok("tail: the cap bounds the file",
   os.path.getsize(c.path) <= 600 + 200, f"{os.path.getsize(c.path)}B")

before = sorted(os.listdir(tmp))
off = LogSink(tmp, "off")
off.write("into the void\n")         # must be a no-op, not a crash
off.flush()
off.close()
ok("off: writes nothing, breaks nothing",
   sorted(os.listdir(tmp)) == before and off.path is None,
   f"{os.listdir(tmp)}")

full = LogSink(tmp, "full")
full.write("kept\n")
full.close()
ok("full: timestamped file per start, kept",
   any(n.startswith("supervisor-2") for n in os.listdir(tmp)),
   f"{os.listdir(tmp)}")

lockdir = tempfile.mkdtemp()
with open(os.path.join(lockdir, LogSink.NAME), "w") as f:
    f.write("held by a viewer\n")
os.mkdir(os.path.join(lockdir, LogSink.PREV))   # refuses rotation like a lock
locked = LogSink(lockdir, "tail")
locked.write("still alive\n")
locked.close()
ok("tail: a locked rotation falls back to a timestamped file, session "
   "stays alive",
   locked.mode == "full" and "locked" in locked.note
   and os.path.basename(locked.path).startswith("supervisor-2"),
   f"path={locked.path!r} note={locked.note!r}")

hostile = os.path.join(lockdir, "blocker")
with open(hostile, "w") as f:
    f.write("a file where a directory must go")
dead = LogSink(os.path.join(hostile, "logs"), "tail")
dead.write("nowhere to put this\n")  # must not raise
dead.close()
ok("an impossible log dir disables the disk log, never the session",
   dead.f is None and dead.failed is not None and "unavailable" in dead.note,
   f"failed={dead.failed!r} note={dead.note!r}")

print("\nlog sink — the failure race the gate caught")
# The OSError handler nulls self.f under the lock; a writer that checked
# f OUTSIDE the lock would pass a stale check and crash on None. This
# drives that interleaving deterministically: hold the lock, let a writer
# queue up behind it, null f, release. The fixed code exits quietly; the
# buggy code dies with AttributeError in whichever thread lost the race —
# and from the pump thread that death wedges the whole feed.
tmp = tempfile.mkdtemp()
sink = LogSink(tmp, "tail")
sink.write("alive\n")
_crashes = []
def _late_writer():
    try:
        sink.write("late writer\n")
    except BaseException as e:
        _crashes.append(repr(e))
sink._lock.acquire()
_t = threading.Thread(target=_late_writer)
_t.start()
time.sleep(0.3)                      # writer is now queued behind the lock
_held = sink.f
sink.f = None                        # what the failure handler does, locked
sink._lock.release()
_t.join(timeout=5)
ok("a writer that raced the failure handler exits quietly, never crashes",
   not _crashes and not _t.is_alive(), f"{_crashes}")
_held.close()

print("\nreplay honesty — the supervisor asks the feed's own parser")
# A replay can arrive from config.json with an empty command line; the
# status file (and the stall-kill exemption) must see it anyway. This is
# the integration the mutation run proved untested: resolved_defaults as
# a function passed while a reverted string-match in main went unnoticed.
tmp = tempfile.mkdtemp()
replay_cfg = os.path.join(tmp, "config.json")
with open(replay_cfg, "w", encoding="utf-8") as f:
    json.dump({"obd_feed": {"replay": "reports/x.csv"}}, f)
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          settle=1.5, config=replay_cfg)
s = read_status(sp)
ok("a config-file replay is reported as replay mode, empty CLI and all",
   s["detail"]["mode"] == "replay", s["detail"]["mode"])
proc.kill()
proc.wait()

print("\nstaleness contract")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE), settle=1.5)
s = read_status(sp)
age = time.time() - s["updated_unix"]
ok("a live file is well inside its own stale window",
   age < s["stale_after_s"], f"age {age:.1f}s < {s['stale_after_s']}s")
proc.kill()
proc.wait()
time.sleep(1.0)
frozen = read_status(sp)
ok("a killed supervisor leaves the file claiming LIVE — which is exactly why "
   "the reader must check the clock",
   frozen["state"] == "LIVE", frozen["state"])
ok("and the clock is what gives it away",
   time.time() - frozen["updated_unix"] > 0.5,
   f"{time.time() - frozen['updated_unix']:.1f}s old")

# =================================================================================
# The second leg. Everything above ran the supervisor exactly as it has always
# been run — one child, no --gps — and it all still holds; that is the
# "single-child invocation unchanged" contract. From here down, both legs.
# =================================================================================

print("\ngps liveness pattern — the overlay's real line")
def _gline(snap, now=1000.0, up=12):
    return gps_status_line(snap, now, up)
ok_line = _gline({"ok": True, "t": 999.9, "speed_kmh": 45.2, "crawl": False,
                  "sats": 9, "accuracy_m": 3.1})
lost_line = _gline({"ok": False, "t": 972.6, "speed_kmh": 45.2, "crawl": False,
                    "sats": 9, "accuracy_m": 3.1, "reason": "signal lost: ClearCommError"})
wait_line = _gline({"ok": False, "t": 0.0})
crawl_line = _gline({"ok": True, "t": 999.9, "speed_kmh": 0.8, "crawl": True,
                     "sats": 10, "accuracy_m": 2.4})
ok("matches the overlay's real ok line", bool(GPS_LINE.match(ok_line)), ok_line.strip()[:40])
ok("matches the overlay's real LOST line", bool(GPS_LINE.match(lost_line)))
ok("matches the overlay's real waiting line", bool(GPS_LINE.match(wait_line)))
ok("the OBD pattern does NOT match a GPS line", not SAMPLE_LINE.match(ok_line))
ok("the GPS pattern does NOT match an OBD line",
   not GPS_LINE.match("  t    42s  RPM  2100  speed 40 km/h (true)  gear 3"))
ok("ignores the overlay's banner", not GPS_LINE.match("GPS run log: runs/gps-last.txt"))
ok("ignores 'GPS source dropped'", not GPS_LINE.match("GPS source dropped (EOF)"))
m = GPS_FIELDS.match(lost_line)
ok("reads the status token", m and m.group("status") == "LOST", m and m.group("status"))
ok("reads the age", m and abs(float(m.group("age")) - 27.4) < 0.05, m and m.group("age"))
ok("reads the reason", m and m.group("reason") == "signal lost: ClearCommError",
   m and m.group("reason"))
m2 = GPS_FIELDS.match(wait_line)
ok("a waiting line with no fix parses (age is a dash)",
   m2 and m2.group("status") == "waiting" and m2.group("age") is None, wait_line.strip())
m3 = GPS_FIELDS.match(crawl_line)
ok("reads crawl yes", m3 and m3.group("crawl") == "yes", crawl_line.strip()[:60])

print("\ngps leg — what the line means")
g = GpsStatus(fix_age_seconds=30)
ok("starts STARTING", g.state == "STARTING")
g.saw_line(ok_line); g.judge(0.1, 5, 10)
ok("ok + crawl no = LIVE", g.state == "LIVE", g.state)
ok("LIVE is healthy", g.healthy() is True)
ok("counts the line as a pulse", g.pulses == 1 and g.pulse_mono is not None)
ok("keeps what the line said", g.fix["sats"] == 9 and g.fix["speed_kmh"] == 45.2, str(g.fix))
g.saw_line(crawl_line); g.judge(0.1, 5, 10)
ok("ok + crawl yes = CRAWLING", g.state == "CRAWLING", g.state)
ok("CRAWLING is healthy — parked is not broken", g.healthy() is True)
ok("crawling says so in words", "crawling" in g.summary(), g.summary())
g.saw_line(lost_line); g.judge(0.1, 5, 10)
ok("LOST on the line = LOST in the file", g.state == "LOST", g.state)
ok("LOST is not healthy", g.healthy() is False)
ok("the reason rides along", "ClearCommError" in g.summary(), g.summary())
g.saw_line(wait_line); g.judge(0.1, 5, 10)
ok("waiting on the line = WAITING", g.state == "WAITING", g.state)
ok("waiting gives sky advice", "sky" in g.summary(), g.summary())
stale = _gline({"ok": True, "t": 1000.0 - 95, "speed_kmh": 45.2, "crawl": False,
                "sats": 9, "accuracy_m": 3.1})
g.saw_line(stale); g.judge(0.1, 5, 10)
ok("ok about a 95 s old fix reads LOST (the silent-port case)", g.state == "LOST", g.state)
ok("and says the port went quiet", "quiet" in g.summary(), g.summary())
g.saw_line("  t   12s  GPS ok  age ??? something new"); g.judge(0.1, 5, 10)
ok("a status line it cannot read still counts as a pulse", g.pulses == 6, str(g.pulses))
ok("but reads LOST, not LIVE — fail toward loud", g.state == "LOST", g.state)
ok("and quotes the line", "not understood" in g.summary(), g.summary())
g.saw_line(ok_line)
g.judge(None, 12, 10)
ok("no line yet past the stall budget = STALLED", g.state == "STALLED", g.state)
g.judge(11.0, 20, 10)
ok("a line older than the stall budget = STALLED", g.state == "STALLED", g.state)
ok("stalled says silent, not lost", "printed nothing" in g.summary(), g.summary())
for state in ("STARTING", "WAITING", "LOST", "STALLED", "NO_ADAPTER", "RECONNECTING", "STOPPED"):
    g.set_state(state)
    s = g.summary()
    ok(f"gps {state} has a spoken sentence", len(s) > 15 and s.endswith((".", "!")), s[:48])
    ok(f"gps {state} is not healthy", g.healthy() is False)

print("\none sentence for two legs")
st1 = Status(os.path.join(tempfile.mkdtemp(), "s.json"), stale_after_s=10, replay=False)
st1.set_state("LIVE"); st1.saw_sample()
solo = st1.snapshot()
ok("without --gps the file has no gps stanza", "gps" not in solo)
ok("and names its one leg", solo["legs"] == ["obd"], str(solo["legs"]))
ok("and the summary is the OBD sentence, unchanged",
   solo["summary"].startswith("The car is talking to SimHub"), solo["summary"])
g2 = GpsStatus()
g2.saw_line(crawl_line); g2.judge(0.1, 5, 10)
st2 = Status(os.path.join(tempfile.mkdtemp(), "s.json"), stale_after_s=10, replay=False, gps=g2)
st2.set_state("LIVE"); st2.saw_sample()
both = st2.snapshot()
ok("with --gps the file names both legs", both["legs"] == ["obd", "gps"], str(both["legs"]))
ok("top-level state is still the OBD leg", both["state"] == "LIVE" and both["healthy"] is True)
ok("the gps stanza carries its own state", both["gps"]["state"] == "CRAWLING", both["gps"]["state"])
ok("the gps stanza carries its own healthy", both["gps"]["healthy"] is True)
ok("the gps stanza carries its own sentence", "crawling" in both["gps"]["summary"])
ok("the gps stanza carries the fix", both["gps"]["detail"]["fix"]["crawl"] is True)
ok("ONE summary sentence names both", both["summary"] == f"OBD {st2.phrase()}, GPS crawling.",
   both["summary"])
ok("it is one sentence", both["summary"].count(".") == 1 and "\n" not in both["summary"])
ok("it reads the way the design mail said it would",
   both["summary"].startswith("OBD live for") and both["summary"].endswith("GPS crawling."),
   both["summary"])
g2.saw_line(lost_line); g2.judge(0.1, 5, 10)
ok("GPS lost, the sentence says so", "GPS lost for" in st2.spoken(), st2.spoken())
st2.set_state("STALLED")
ok("OBD stalled, the sentence says so", st2.spoken().startswith("OBD stalled"), st2.spoken())
ok("the OBD detail block is untouched by the second leg",
   set(both["detail"]) == set(solo["detail"]))
st3 = Status(os.path.join(tempfile.mkdtemp(), "s.json"), stale_after_s=10, replay=True, gps=g2)
st3.set_state("LIVE")
ok("a replay feed says replaying in the two-leg sentence too, not live",
   st3.spoken().startswith("OBD replaying for"), st3.spoken())

# --- end to end: both legs up -----------------------------------------------------
print("\nend to end: both legs up")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          gps_stub=write_stub(tmp, "gps_ok.py", GPS_STUB_OK), settle=2.5)
s = read_status(sp)
ok("OBD leg reaches LIVE", s["state"] == "LIVE", s["state"])
ok("GPS leg reaches LIVE", s["gps"]["state"] == "LIVE", s["gps"]["state"])
ok("both healthy", s["healthy"] is True and s["gps"]["healthy"] is True)
ok("the summary is one sentence naming both",
   s["summary"].startswith("OBD live for") and s["summary"].endswith("GPS live."), s["summary"])
ok("the GPS child has a pid", s["gps"]["detail"]["pid"] is not None)
ok("the GPS child's lines are counted", s["gps"]["detail"]["lines_seen"] > 5,
   str(s["gps"]["detail"]["lines_seen"]))
ok("the fix rides in the file", s["gps"]["detail"]["fix"]["sats"] == 9, str(s["gps"]["detail"]["fix"]))
ok("the OBD leg's samples are its own — the GPS lines did not count as RPM",
   s["detail"]["samples_seen"] > 5 and s["gps"]["detail"]["lines_seen"] > 5)
ok("settings record the fix-age threshold",
   s["detail"]["supervisor"].get("gps_fix_age_seconds") == 30.0, str(s["detail"]["supervisor"]))
gps_pid = s["gps"]["detail"]["pid"]
out = stop(proc)
final = read_status(sp)
ok("both legs say STOPPED after a deliberate shutdown",
   final["state"] == "STOPPED" and final["gps"]["state"] == "STOPPED",
   f"{final['state']} / {final['gps']['state']}")
ok("both pids cleared", final["detail"]["feed_pid"] is None and final["gps"]["detail"]["pid"] is None)
ok("the banner names the GPS child", "gps_ok.py" in out and "believed when it says LOST" in out)
gps_banner = [ln for ln in out.splitlines() if ln.startswith("supervisor: watching") and "gps_ok.py" in ln]
ok("the GPS child is handed the supervisor's --config — one file governs every process",
   gps_banner and "--config " + CFG_FIXTURE in gps_banner[0],
   gps_banner[0][:100] if gps_banner else "(no banner line)")
try:
    os.kill(gps_pid, 0)
    orphan = True
except OSError:
    orphan = False
ok("the GPS child did not outlive the supervisor", not orphan or WINDOWS)

# --- end to end: GPS lost is believed, not killed --------------------------------
print("\nend to end: GPS says LOST — believed, not killed")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          gps_stub=write_stub(tmp, "gps_lost.py", GPS_STUB_LOST),
                          extra=("--stall-restart-seconds", "2"), settle=6.0)
s = read_status(sp)
ok("GPS leg reads LOST", s["gps"]["state"] == "LOST", s["gps"]["state"])
ok("with the overlay's own reason", "ClearCommError" in s["gps"]["summary"], s["gps"]["summary"])
ok("the GPS was NOT restarted for it", s["gps"]["detail"]["restarts"] == 0,
   f"{s['gps']['detail']['restarts']} restarts")
ok("the GPS process is still the same one, still running", s["gps"]["detail"]["pid"] is not None)
ok("its lines are still being counted — LOST is a pulse", s["gps"]["detail"]["lines_seen"] > 30,
   str(s["gps"]["detail"]["lines_seen"]))
ok("the sentence says lost", "GPS lost for" in s["summary"], s["summary"])
ok("the OBD leg is untouched", s["state"] == "LIVE" and s["detail"]["restarts"] == 0)
ok("top-level healthy is still the OBD leg's", s["healthy"] is True)
ok("the GPS stanza's healthy is false", s["gps"]["healthy"] is False)
stop(proc)

# --- end to end: GPS crawling -----------------------------------------------------
print("\nend to end: GPS crawling")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          gps_stub=write_stub(tmp, "gps_crawl.py", GPS_STUB_CRAWL), settle=2.5)
s = read_status(sp)
ok("GPS leg reads CRAWLING", s["gps"]["state"] == "CRAWLING", s["gps"]["state"])
ok("crawling is healthy", s["gps"]["healthy"] is True)
ok("the sentence: OBD live for …, GPS crawling.", s["summary"].endswith("GPS crawling."), s["summary"])
stop(proc)

# --- end to end: GPS says ok about a stale fix ------------------------------------
print("\nend to end: GPS says ok about a two-minute-old fix")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          gps_stub=write_stub(tmp, "gps_stale.py", GPS_STUB_STALE_OK), settle=2.5)
s = read_status(sp)
ok("reads LOST, not LIVE", s["gps"]["state"] == "LOST", s["gps"]["state"])
ok("names the quiet port", "quiet" in s["gps"]["summary"], s["gps"]["summary"])
ok("and is not restarted for it either", s["gps"]["detail"]["restarts"] == 0)
stop(proc)

# --- end to end: GPS waiting for a fix --------------------------------------------
print("\nend to end: GPS waiting for a fix")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          gps_stub=write_stub(tmp, "gps_wait.py", GPS_STUB_WAITING), settle=2.5)
s = read_status(sp)
ok("reads WAITING", s["gps"]["state"] == "WAITING", s["gps"]["state"])
ok("the sentence says so", "GPS waiting for a fix" in s["summary"], s["summary"])
stop(proc)

# --- end to end: GPS goes silent — STALLED for that leg only, then killed --------
print("\nend to end: GPS goes silent — this leg only")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          gps_stub=write_stub(tmp, "gps_silent.py", GPS_STUB_GOES_SILENT),
                          extra=("--stall-restart-seconds", "0"), settle=4.5)
s = read_status(sp)
ok("GPS leg reads STALLED", s["gps"]["state"] == "STALLED", s["gps"]["state"])
ok("stalled is worded as silence, not loss", "printed nothing" in s["gps"]["summary"],
   s["gps"]["summary"])
ok("the OBD leg is LIVE beside it", s["state"] == "LIVE", s["state"])
ok("the sentence keeps both", s["summary"].startswith("OBD live for") and "GPS stalled" in s["summary"],
   s["summary"])
ok("report-only: not killed", s["gps"]["detail"]["restarts"] == 0)
ok("it remembers the lines that did flow", s["gps"]["detail"]["lines_seen"] == 20,
   str(s["gps"]["detail"]["lines_seen"]))
stop(proc)

tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "alive.py", STUB_ALIVE),
                          gps_stub=write_stub(tmp, "gps_wedged.py", GPS_STUB_GOES_SILENT),
                          extra=("--stall-restart-seconds", "2"), settle=5.0)
deadline = time.time() + 20
s = read_status(sp)
while time.time() < deadline and not (
        s["gps"]["detail"]["restarts"] >= 1 and s["gps"]["detail"]["lines_seen"] > 20):
    time.sleep(0.3)
    s = read_status(sp)
ok("a silent overlay IS killed and restarted — quiet is the one thing that means wedged",
   s["gps"]["detail"]["restarts"] >= 1, f"{s['gps']['detail']['restarts']} restarts")
ok("the recorded reason is the supervisor's own judgement, and names the line",
   "no status line" in ((s["gps"]["detail"]["last_exit"] or {}).get("reason") or ""),
   (s["gps"]["detail"]["last_exit"] or {}).get("reason", "")[:60])
ok("the fresh overlay printed again after the kill", s["gps"]["detail"]["lines_seen"] > 20,
   str(s["gps"]["detail"]["lines_seen"]))
ok("the OBD leg was never restarted", s["detail"]["restarts"] == 0, str(s["detail"]["restarts"]))
stop(proc)

# --- end to end: OBD wedged beside a healthy GPS ----------------------------------
print("\nend to end: OBD wedged, GPS fine — only the MX+ gets killed")
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "wedged.py", STUB_GOES_QUIET),
                          gps_stub=write_stub(tmp, "gps_ok.py", GPS_STUB_OK),
                          extra=("--stall-restart-seconds", "2"), settle=5.0)
deadline = time.time() + 20
s = read_status(sp)
while time.time() < deadline and not (
        s["detail"]["restarts"] >= 1 and s["detail"]["samples_seen"] > 3):
    time.sleep(0.3)
    s = read_status(sp)
ok("the wedged feed is killed and restarted", s["detail"]["restarts"] >= 1,
   f"{s['detail']['restarts']} restarts")
ok("the recorded reason is the wedge", "wedged" in ((s["detail"]["last_exit"] or {}).get("reason") or ""))
ok("the GPS leg was never restarted", s["gps"]["detail"]["restarts"] == 0,
   str(s["gps"]["detail"]["restarts"]))
ok("the GPS leg stayed LIVE throughout", s["gps"]["state"] == "LIVE", s["gps"]["state"])
stop(proc)

# --- the file never goes stale during a backoff -----------------------------------
print("\nthe file keeps its pulse while a leg sits in backoff")
# Before the per-leg tick, the loop slept through the backoff between runs and
# the file went unwritten for up to --backoff-max seconds — 60 by default,
# against a stale_after_s of 10. A reader following the one rule would have
# called the supervisor dead every time the feed took a long breath.
tmp = tempfile.mkdtemp()
proc, sp = run_supervisor(tmp, write_stub(tmp, "noad.py", STUB_NO_ADAPTER),
                          extra=("--backoff-start", "3", "--backoff-max", "3"), settle=1.5)
s = read_status(sp)
ok("in backoff, the file says NO_ADAPTER", s["state"] == "NO_ADAPTER", s["state"])
t0 = s["updated_unix"]
time.sleep(1.2)
s2 = read_status(sp)
ok("and the file keeps being rewritten while it waits",
   s2["updated_unix"] > t0 and s2["state"] == "NO_ADAPTER",
   f"{t0} -> {s2['updated_unix']}, {s2['state']}")
stop(proc)

# --- refusals ---------------------------------------------------------------------
print("\nrefusals")
r = subprocess.run([sys.executable, SUP, "--config", CFG_FIXTURE, "--gps",
                    "--gps-args", "--config x.json", "--feed", "nope.py"],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
ok("--config inside --gps-args is refused, like after the --",
   r.returncode != 0 and "one config file" in r.stdout, r.stdout.strip()[:80])

# ---------------------------------------------------------------------------------

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
