"""Tests for the phase 3 supervisor. Run: python supervisor/test_supervisor.py

Covers the liveness pattern against the feed's real print format, the
speakable summary lines, exit-reason extraction, and — the part that
matters — four end-to-end runs against stub feeds that fail in the four
ways a real one does: never starts, runs clean, dies mid-drive, dies
instantly and repeatedly.

The point of the supervisor is recovery from failures we did not predict,
so the tests inject failures rather than assert on internal state. Stdlib
only, no car, no adapter.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from supervisor import SAMPLE_LINE, Status, FeedProcess, human_duration

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


def write_stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(body)
    return p


def run_supervisor(tmp, stub, extra=(), settle=2.0):
    """Launch the supervisor on a stub feed, let it settle, return (proc, status_path)."""
    status_path = os.path.join(tmp, "obd2_status.json")
    argv = [sys.executable, "-u", SUP,
            "--status-file", status_path,
            "--status-interval", "0.15",
            "--stall-seconds", "1",
            "--backoff-start", "0.3",
            "--backoff-max", "0.5",
            "--healthy-seconds", "0.4",
            "--log-dir", os.path.join(tmp, "runs"),
            "--feed", stub,
            "--quiet"] + list(extra)
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

# --- the staleness contract -------------------------------------------------------
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

# ---------------------------------------------------------------------------------

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
