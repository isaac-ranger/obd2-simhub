#!/usr/bin/env python3
"""
supervisor.py — phase 3: keep the feed alive without a keyboard
===============================================================

Phase 2 answered "can the car talk to SimHub." This answers "can it keep
doing that for three hours while your hands are full." It owns one thing
only: the lifecycle of the car-facing processes — extractor/obd_feed.py
always, and gps/gps_overlay.py when asked. Start them, watch them, restart
them, forever, never exit.

Deliberately NOT in scope: OBS, VoiceAttack, SimHub itself, the router,
the MX+ pairing. Those are other people's processes with their own ideas
about startup, and a supervisor that tries to restart OBS mid-event can
do more harm than the failure it is fixing. This one babysits the
extractor (and the overlay), where it can actually detect state and
recover it.

Usage (Windows, the real thing):
  py supervisor\\supervisor.py -- --port COM3
  py supervisor\\supervisor.py --gps -- --port COM3       (both legs)

Usage (no car — against the fake car in another terminal):
  python extractor/fake_car.py --tcp 35000
  python supervisor/supervisor.py -- --port socket://127.0.0.1:35000

Everything after `--` is handed to obd_feed.py untouched, so any flag the
feed grows works here on the day it lands, with no change to this file.
The GPS overlay is opt-in with --gps and takes its own settings from
config.json's `gps_overlay` section, exactly as it does when run by hand;
--gps-args carries anything that must ride the command line instead.

WHAT IT WATCHES, AND WHY THAT WAY
The feed already prints a status line every second with flush=True. That
line is the liveness signal: seeing one means the car answered and packets
went out. So the supervisor reads the child's stdout rather than asking
the feed to grow a heartbeat channel — no change to obd_feed.py, and the
signal means "data actually moved," not "the process is still resident."
A process can be alive and wedged. A printed sample cannot.

Four failures, four responses (the OBD leg):
  - feed exits (25 misses, sender death, crash)    -> restart after backoff
  - feed alive but silent past --stall-seconds     -> report STALLED
  - no data THIS RUN past --stall-restart-seconds  -> kill the feed, restart it
  - the adapter was never there                    -> report NO_ADAPTER, keep retrying

(Replay runs are exempt from the stall-kill: a replay cannot wedge on a dead
COM handle, and a long quiet stretch in a recording is silence with a future
— killing it would restart the replay from the top, forever.)

The stall-restart exists because of a failure the field actually produced
(2026-08-02, MX+ unplugged mid-drive): the adapter loses power, Windows
keeps the COM handle alive, and the feed blocks in a serial write that
never returns and never raises. The 25-miss watchdog counts completed
failures; a call that never completes cannot be counted. The feed got a
write timeout the same day, so this path is the second layer — the
supervisor acting on what its own status file already says. Report-only
was the original philosophy ("keep watching, it might recover"), and the
field showed a wedged process holds the dead handle hostage: nothing
recovers until someone reopens the port, and that someone is here.

The 25-miss exit is the one that matters at an event: it means power-cycle
the adapter. The supervisor cannot power-cycle it for you, but it retries
forever — so when you do pull and replug the MX+, the feed comes back on
its own and you never touch the keyboard.

THE GPS LEG IS A DIFFERENT ANIMAL
gps_overlay.py prints one status line a second too ("  t    12s  GPS ok
age 0.1s ..."), from the same snapshot /live serves — and it prints it
even when the receiver is gone, saying LOST and why. So for that child
the pulse is "it printed a status line at all", and the CONTENT of the
line is the health: ok / LOST / waiting, the fix age, crawl. The GPS gets
believed when it says LOST: a receiver out of range or napping is the
process correctly reporting the world, not a wedge, and killing it for
that would be the driveway bug all over again — punishing a thing for
doing nothing while parked. It is killed only for exiting (restart after
backoff, like the feed) or for going silent — no line at all past the
stall budget, which the ticker's fail-loud stdout path makes the honest
signature of a wedged process. Same --stall-seconds and
--stall-restart-seconds numbers, different definition of quiet.

THE STATUS FILE
Written to status/obd2_status.json (atomically — a reader never sees a
half-written file). It carries a plain-English `summary` line meant to be
dropped straight into PitGirl's context, plus the structured detail
underneath for anything that wants to be precise. With --gps the file
grows a `gps` stanza and the summary becomes one sentence about both
legs — "OBD live for 12 minutes, GPS crawling." — because PitGirl reads a
sentence, not a schema. Every top-level key keeps today's meaning for the
OBD leg, so a reader written against the single-leg file keeps working.

One thing the reader MUST do: check `updated_at` against `stale_after_s`.
If the file is older than that, the supervisor itself is gone and the file
is a photograph, not a status. A stale file still says LIVE — that is the
oldest trap in monitoring, and this one is only avoided on the reading
side. Both fields are in the file so the check is possible without knowing
anything about this program. One quiet leg never makes the whole file
stale: the file is rewritten every interval by the supervisor, whatever
its children are doing.

Requires: python 3.9+, stdlib only.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

sys.path.insert(0, REPO)
from obd_config import parse_with_config, default_config_path, resolved_defaults

# The feed's per-second status line: "  t   42s  RPM  2100  speed ...".
# Replay mode prints the same shape, so one pattern covers both.
SAMPLE_LINE = re.compile(r"^\s*t\s+\d+s\s+RPM\s")

# The overlay's per-second status line: "  t   42s  GPS ok       age   0.1s
# speed  45.2 km/h  crawl no   sats  9  acc  3.1 m  (reason)". Any line of
# this shape is the pulse; the fields on it are the health. GPS_LINE is the
# pulse test; GPS_FIELDS reads the line, tolerant of any column growing.
GPS_LINE = re.compile(r"^\s*t\s+\d+s\s+GPS\s")
GPS_FIELDS = re.compile(
    r"^\s*t\s+(?P<t>\d+)s\s+GPS\s+(?P<status>ok|LOST|waiting)\s+"
    r"age\s+(?:(?P<age>[\d.]+)s|-)"
    r"(?:\s+speed\s+(?P<speed>[\d.]+)\s+km/h)?"
    r"(?:\s+crawl\s+(?P<crawl>yes|no|-))?"
    r"(?:\s+sats\s+(?P<sats>\d+|-))?"
    r"(?:\s+acc\s+(?P<acc>[\d.]+|-)\s+m)?"
    r"(?:\s+\((?P<reason>.*)\))?\s*$")

# Exit output that means "the adapter isn't there" rather than "it stopped
# answering". Different advice for the driver, so worth telling apart.
NO_ADAPTER_HINTS = (
    "could not open port",
    "cannot open",
    "no such file or directory",
    "access is denied",
    "pyserial is required",
    "port not found",
    "connection refused",
)


def now():
    """Local wall-clock time, timezone-aware.

    Local rather than UTC on purpose: everyone who reads these timestamps is
    standing near the car, comparing them against the dash clock or against a
    run log named in local time. UTC made that a subtraction, and made the
    feed log and the supervisor log for one drive look seven hours apart.
    The offset travels in the string, so a log mailed across timezones is
    still unambiguous, and `updated_unix` remains the machine-side anchor
    for anything comparing instants.
    """
    return datetime.now().astimezone()


def iso(dt):
    """ISO-8601 with the local offset: 2026-08-03T09:34:29-07:00."""
    return dt.isoformat(timespec="seconds")


def human_duration(seconds):
    """Speakable, not precise. PitGirl says this out loud."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} second{'' if s == 1 else 's'}"
    if s < 3600:
        m = s // 60
        return f"{m} minute{'' if m == 1 else 's'}"
    h, m = divmod(s // 60, 60)
    if m == 0:
        return f"{h} hour{'' if h == 1 else 's'}"
    return f"{h}h {m}m"


class LogSink:
    """The supervisor's disk log (its own notes plus the feed's output),
    in three sizes to match the feed's --run-log: 'full' keeps a
    timestamped file per start, forever — development. 'tail' (default)
    keeps ONE file, supervisor-last.log, overwritten at every start and
    size-capped, so the last session stays diagnosable and nothing
    accumulates across an autocross season. 'off' writes nothing.

    Every existing write site talks to this unchanged: when the log is
    off, writes fall through — the console echo and the status file are
    unaffected either way."""

    NAME = "supervisor-last.log"
    PREV = "supervisor-prev.log"
    # When the cap trips, the oldest half goes and the newest half stays
    # — the lines a bug report needs are the recent ones, and a plain
    # truncate would drop exactly the line that tripped it.
    CAP = 5 * 1024 * 1024

    def __init__(self, directory, mode):
        self.mode = mode
        self.path = None
        self.note = ""
        self.f = None
        self.failed = None
        # write() is called from the main loop AND from FeedProcess's pump
        # thread; the tail wrap is a read-modify-write, and an unlocked
        # writer landing inside it gets flushed to offset 0 and then
        # truncated away — the QA pass demonstrated it eating the
        # supervisor's own "feed exited" line, the one line a mailed-in
        # log exists to carry.
        self._lock = threading.Lock()
        if mode == "off":
            return
        try:
            os.makedirs(directory, exist_ok=True)
            if mode == "tail":
                self.path = os.path.join(directory, self.NAME)
                # The previous session survives one generation as
                # supervisor-prev.log: restarting the supervisor is the
                # first thing a person does when something goes wrong,
                # and it must not erase the reason they restarted it.
                # Rotation and open share the fallback: a locked file
                # refuses either one, and both deserve the same answer.
                try:
                    if os.path.exists(self.path):
                        os.replace(self.path,
                                   os.path.join(directory, self.PREV))
                    # w+ because the wrap below reads back what it keeps
                    self.f = open(self.path, "w+", encoding="utf-8")
                except OSError:
                    # Windows: a viewer holding the file locks it. Keep
                    # the session alive on a timestamped file and say so.
                    self.mode = "full"
                    self.note = (f"  ({self.NAME} is locked by another "
                                 f"program — keeping a full log this "
                                 f"session)")
            if self.f is None:
                name = f"supervisor-{now().strftime('%Y%m%d-%H%M%S')}.log"
                self.path = os.path.join(directory, name)
                self.f = open(self.path, "a", encoding="utf-8")
        except OSError as e:
            # The status file and console echo are the product here; the
            # disk log must never take the session down.
            self.failed = str(e)
            self.f = None
            self.note = f"  (unavailable: {e} — continuing without one)"

    def write(self, s):
        with self._lock:
            # The liveness check lives INSIDE the lock: the failure
            # handler below nulls self.f under this same lock, and a
            # writer that checked outside could pass a stale check and
            # crash on None — disk-full hits both writer threads at
            # once, which is exactly the shape the QA gate caught.
            if not self.f:
                return
            try:
                self.f.write(s)
                if self.mode == "tail" and self.f.tell() > self.CAP:
                    self.f.seek(0)
                    tail = self.f.read()
                    tail = tail[len(tail) // 2:]
                    tail = tail[tail.find("\n") + 1:]  # line boundary
                    self.f.seek(0)
                    self.f.truncate()
                    self.f.write(f"(log wrapped at {iso(now())} — "
                                 f"older lines dropped)\n")
                    self.f.write(tail)
                    self.f.flush()
            except OSError as e:
                self.failed = str(e)
                try:
                    self.f.close()
                except Exception:
                    pass
                self.f = None
                print(f"supervisor: disk log failed ({e}) — "
                      f"continuing without one", flush=True)

    def flush(self):
        with self._lock:
            if self.f:
                self.f.flush()

    def close(self):
        with self._lock:
            if self.f:
                self.f.close()
                self.f = None




class Status:
    """The supervisor's view of the world, and the file it writes it to.

    Every field here is either directly observed or derived from something
    observed. Nothing is assumed to persist: if the supervisor cannot see
    it this second, it does not claim it.

    This object is the OBD leg's state AND the writer of the whole status
    file. Every top-level key keeps its single-leg meaning; when a GpsStatus
    is attached, the file grows a `gps` stanza beside it and the summary
    becomes one sentence about both legs.
    """

    def __init__(self, path, stale_after_s, replay, settings=None, gps=None):
        self.path = path
        self.stale_after_s = stale_after_s
        self.replay = replay
        # What this supervisor was told to do, carried into the status file so
        # a snapshot from a driveway can be read without also asking the driver
        # what they typed. A watchdog that does not record its own threshold
        # makes every "it didn't fire" report ambiguous between a bug and a
        # setting — which is exactly the round trip this exists to prevent.
        self.settings = settings or {}
        self.gps = gps                 # GpsStatus when --gps, else None
        self.started = now()
        self.state = "STARTING"
        self.state_since = now()
        self.last_data = None          # wall clock of last sample line
        self.last_data_mono = None     # monotonic, for the staleness maths
        self.restarts = 0
        self.last_restart = None
        self.last_exit = None          # {"code": int, "reason": str}
        self.feed_pid = None
        self.total_samples = 0

    # -- what the child's output means ------------------------------------------

    def saw_line(self, line):
        """Called by the pump thread with every line the feed prints."""
        if SAMPLE_LINE.match(line):
            self.saw_sample()

    def saw_sample(self):
        self.last_data = now()
        self.last_data_mono = time.monotonic()
        self.total_samples += 1

    @property
    def pulse_mono(self):
        """Monotonic stamp of the last thing that counts as a pulse for this
        leg — for the OBD feed, the last sample line (data moved)."""
        return self.last_data_mono

    @property
    def pulses(self):
        return self.total_samples

    def judge(self, since, run_age, stall_seconds):
        """Set the state from the pulse age. `since` is seconds since this
        run's last pulse (None = none yet), `run_age` seconds since the
        child started."""
        if since is None:
            # No data yet this run. Give it the stall budget to connect
            # before calling it anything worse than STARTING.
            if run_age > stall_seconds:
                self.set_state("STALLED")
        elif since > stall_seconds:
            self.set_state("STALLED")
        else:
            self.set_state("LIVE")

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.state_since = now()

    def seconds_since_data(self):
        if self.last_data_mono is None:
            return None
        return round(time.monotonic() - self.last_data_mono, 1)

    # -- what it says -----------------------------------------------------------

    def summary(self):
        """One sentence, spoken aloud, no jargon the driver has to decode.
        This is the OBD leg's own sentence; see spoken() for the file's."""
        in_state = human_duration((now() - self.state_since).total_seconds())
        if self.state == "LIVE":
            src = "Replaying a recorded drive" if self.replay else "The car is talking to SimHub"
            line = f"{src} and data has been flowing for {in_state}."
            if self.restarts:
                line += (f" It recovered from {self.restarts} interruption"
                         f"{'' if self.restarts == 1 else 's'} along the way.")
            return line
        if self.state == "STARTING":
            return "Starting up — connecting to the adapter now."
        if self.state == "STALLED":
            gap = self.seconds_since_data()
            if gap is None:
                # Never received a single sample this run — a different
                # problem from "it stopped", and different advice.
                return ("The feed is running but the car has not answered yet. "
                        "Check the ignition is on and the adapter is paired.")
            return (f"The feed is running but no data has arrived for "
                    f"{human_duration(gap)}. The car may be off, or the "
                    f"adapter may have dropped.")
        if self.state == "NO_ADAPTER":
            return ("No OBD adapter found. Check the MX+ is plugged in, "
                    "paired, and powered — I will keep trying.")
        if self.state == "RECONNECTING":
            why = (self.last_exit or {}).get("reason", "")
            tail = f" Last error: {why}" if why else ""
            return (f"Lost the connection and I am reconnecting. "
                    f"Attempt {self.restarts + 1}.{tail}")
        if self.state == "STOPPED":
            return "The extractor was shut down deliberately. Nothing is running."
        return f"State {self.state}."

    def phrase(self):
        """The OBD leg in a few words, for the two-leg sentence: 'live for
        12 minutes', 'stalled for 40 seconds', 'reconnecting (attempt 3)'."""
        in_state = human_duration((now() - self.state_since).total_seconds())
        if self.state == "LIVE":
            return f"live for {in_state}"
        if self.state == "STALLED":
            gap = self.seconds_since_data()
            if gap is None:
                return "stalled, the car has not answered yet"
            return f"stalled for {human_duration(gap)}"
        if self.state == "RECONNECTING":
            return f"reconnecting (attempt {self.restarts + 1})"
        if self.state == "NO_ADAPTER":
            return "no adapter"
        return self.state.lower()

    def spoken(self):
        """The file's `summary`: the OBD sentence alone, or — with a GPS leg
        attached — one sentence naming both, because PitGirl reads a
        sentence, not a schema: 'OBD live for 12 minutes, GPS crawling.'"""
        if self.gps is None:
            return self.summary()
        return f"OBD {self.phrase()}, GPS {self.gps.phrase()}."

    def healthy(self):
        return self.state == "LIVE"

    def snapshot(self):
        ts = now()
        snap = {
            "schema": 1,
            "state": self.state,
            "healthy": self.healthy(),
            "summary": self.spoken(),
            "updated_at": iso(ts),
            "updated_unix": int(ts.timestamp()),
            # If updated_at is older than this, the SUPERVISOR is gone and
            # everything above is a photograph. Check it before believing it.
            "stale_after_s": self.stale_after_s,
            # Which children this supervisor is running. A reader that wants
            # the GPS stanza can check for it here rather than probing keys.
            "legs": ["obd", "gps"] if self.gps is not None else ["obd"],
            "detail": {
                "mode": "replay" if self.replay else "live",
                "state_since": iso(self.state_since),
                "seconds_in_state": int((ts - self.state_since).total_seconds()),
                "last_data_at": iso(self.last_data) if self.last_data else None,
                "seconds_since_data": self.seconds_since_data(),
                "samples_seen": self.total_samples,
                "restarts": self.restarts,
                "last_restart_at": iso(self.last_restart) if self.last_restart else None,
                "last_exit": self.last_exit,
                "feed_pid": self.feed_pid,
                "supervisor_started_at": iso(self.started),
                "supervisor_uptime_s": int((ts - self.started).total_seconds()),
                "supervisor": self.settings,
            },
        }
        if self.gps is not None:
            snap["gps"] = self.gps.stanza(ts)
        return snap

    def write(self):
        """Atomic, because PitGirl may read at any instant. A torn read of a
        status file is worse than no status file: it fails as a parse error
        in someone else's process, at the moment things are already wrong."""
        payload = json.dumps(self.snapshot(), indent=2) + "\n"
        tmp = f"{self.path}.tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


class GpsStatus:
    """The GPS leg's view of the world — the `gps` stanza of the status file.

    Same lifecycle words as the OBD leg where they mean the same thing
    (STARTING, STALLED, RECONNECTING, NO_ADAPTER, STOPPED). The live states
    come from READING the overlay's line, not from the fact that it printed:
      LIVE      the line says ok, fix is fresh, crawl no
      CRAWLING  the line says ok, fix is fresh, crawl yes
      WAITING   the line says waiting — up, no fix yet this run
      LOST      the line says LOST (its reason rides along), or it says ok
                about a fix older than fix_age_seconds — the silent-port
                case the line itself documents ("ok with an age of minutes")
    STALLED here means no line AT ALL for --stall-seconds: the ticker never
    goes quiet on purpose, so quiet is the one thing that means wedged.
    """

    def __init__(self, fix_age_seconds=30.0):
        self.fix_age_seconds = fix_age_seconds
        self.state = "STARTING"
        self.state_since = now()
        self.last_line = None          # wall clock of the last status line
        self.last_line_mono = None
        self.lines_seen = 0
        self.fix = {}                  # what the last line said, parsed
        self.restarts = 0
        self.last_restart = None
        self.last_exit = None
        self.feed_pid = None

    # -- what the child's output means ------------------------------------------

    def saw_line(self, line):
        """Called by the pump thread with every line the overlay prints. Any
        status line is a pulse; its fields become the health. Replace the
        dict rather than mutate it — the main thread reads it unlocked."""
        if not GPS_LINE.match(line):
            return
        self.last_line = now()
        self.last_line_mono = time.monotonic()
        self.lines_seen += 1
        m = GPS_FIELDS.match(line)
        if not m:
            # The pulse counts — the process is alive — but a line this
            # supervisor cannot read is not a fix it can vouch for. Fail
            # toward loud: LOST, with the line as the reason.
            self.fix = {"status": None, "age_s": None, "speed_kmh": None,
                        "crawl": None, "sats": None, "accuracy_m": None,
                        "reason": f"status line not understood: {line.strip()[:80]}"}
            return
        g = m.groupdict()
        crawl = {"yes": True, "no": False}.get(g.get("crawl"))
        self.fix = {
            "status": g["status"],
            "age_s": float(g["age"]) if g.get("age") else None,
            "speed_kmh": float(g["speed"]) if g.get("speed") else None,
            "crawl": crawl,
            "sats": int(g["sats"]) if g.get("sats") not in (None, "-") else None,
            "accuracy_m": float(g["acc"]) if g.get("acc") not in (None, "-") else None,
            "reason": g.get("reason") or None,
        }

    @property
    def pulse_mono(self):
        """For the GPS leg the pulse is any status line at all — LOST is a
        pulse. Silence is the only failure the process cannot report."""
        return self.last_line_mono

    @property
    def pulses(self):
        return self.lines_seen

    def content_state(self):
        """What the last line says the GPS is doing, in this leg's words."""
        f = self.fix
        st = f.get("status")
        if st == "LOST":
            return "LOST"
        if st == "waiting":
            return "WAITING"
        if st == "ok":
            age = f.get("age_s")
            if age is not None and age > self.fix_age_seconds:
                return "LOST"
            return "CRAWLING" if f.get("crawl") is True else "LIVE"
        return "LOST"      # a line it could not read — see saw_line

    def lost_reason(self):
        return self.fix.get("reason") or "no reason given"

    def judge(self, since, run_age, stall_seconds):
        if since is None:
            if run_age > stall_seconds:
                self.set_state("STALLED")
        elif since > stall_seconds:
            self.set_state("STALLED")
        else:
            self.set_state(self.content_state())

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.state_since = now()

    def seconds_since_line(self):
        if self.last_line_mono is None:
            return None
        return round(time.monotonic() - self.last_line_mono, 1)

    def healthy(self):
        return self.state in ("LIVE", "CRAWLING")

    # -- what it says -----------------------------------------------------------

    def summary(self):
        """The GPS leg's own sentence, spoken aloud."""
        in_state = human_duration((now() - self.state_since).total_seconds())
        if self.state == "LIVE":
            return "The GPS has a fix and the car is moving."
        if self.state == "CRAWLING":
            return "The GPS has a fix and the car is parked or crawling."
        if self.state == "WAITING":
            return ("The GPS overlay is up but has no fix yet. Give the "
                    "receiver a clear view of the sky.")
        if self.state == "LOST":
            f = self.fix
            if f.get("status") == "ok":
                # The line still says ok; only the age gives it away.
                return (f"The GPS fix is {human_duration(f.get('age_s') or 0)} old "
                        f"and the overlay has not said why — the port may have "
                        f"gone quiet.")
            if f.get("status") is None:
                return (f"The GPS overlay is up but printing a status line I "
                        f"cannot read ({f.get('reason')}).")
            return f"The GPS lost its fix {in_state} ago — {self.lost_reason()}."
        if self.state == "STARTING":
            return "Starting the GPS overlay — waiting for its first status line."
        if self.state == "STALLED":
            gap = self.seconds_since_line()
            if gap is None:
                return ("The GPS overlay is running but has not printed a "
                        "status line yet.")
            return (f"The GPS overlay is running but has printed nothing for "
                    f"{human_duration(gap)} — it may be wedged.")
        if self.state == "NO_ADAPTER":
            return ("No GPS receiver found. Check the XGPS is on, paired, and "
                    "on the right port — I will keep trying.")
        if self.state == "RECONNECTING":
            why = (self.last_exit or {}).get("reason", "")
            tail = f" Last error: {why}" if why else ""
            return (f"The GPS overlay stopped and I am restarting it. "
                    f"Attempt {self.restarts + 1}.{tail}")
        if self.state == "STOPPED":
            return "The GPS overlay was shut down deliberately."
        return f"State {self.state}."

    def phrase(self):
        """The GPS leg in a few words, for the two-leg sentence."""
        in_state = human_duration((now() - self.state_since).total_seconds())
        if self.state == "WAITING":
            return "waiting for a fix"
        if self.state == "LOST":
            return f"lost for {in_state}"
        if self.state == "STALLED":
            gap = self.seconds_since_line()
            return ("stalled, no status line yet" if gap is None
                    else f"stalled, silent for {human_duration(gap)}")
        if self.state == "RECONNECTING":
            return f"reconnecting (attempt {self.restarts + 1})"
        if self.state == "NO_ADAPTER":
            return "no receiver"
        return self.state.lower()

    def stanza(self, ts):
        return {
            "state": self.state,
            "healthy": self.healthy(),
            "summary": self.summary(),
            "detail": {
                "state_since": iso(self.state_since),
                "seconds_in_state": int((ts - self.state_since).total_seconds()),
                "last_line_at": iso(self.last_line) if self.last_line else None,
                "seconds_since_line": self.seconds_since_line(),
                "lines_seen": self.lines_seen,
                "fix": dict(self.fix),
                "restarts": self.restarts,
                "last_restart_at": iso(self.last_restart) if self.last_restart else None,
                "last_exit": self.last_exit,
                "pid": self.feed_pid,
            },
        }


# Lines a child prints on its way out that carry a reason rather than a
# log path. The feed says "25 consecutive failed samples" / "feed stopped";
# the overlay says "GPS source dropped" / "stdout failed".
REASON_HINTS = ("consecutive failed samples", "feed stopped",
                "gps source dropped", "stdout failed")


class FeedProcess:
    """One run of a child (obd_feed.py or gps_overlay.py), and the thread
    that drains its output. `status` is the leg's state object; every line
    is handed to its saw_line(), which decides what counts as a pulse."""

    def __init__(self, argv, log_file, status, echo=True):
        self.argv = argv
        self.log_file = log_file
        self.status = status
        self.echo = echo
        self.proc = None
        self.tail = []          # last few lines, for explaining an exit
        self._lock = threading.Lock()

    def start(self):
        self.proc = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # -u on the child is what makes the per-second line arrive per
            # second; without it Python block-buffers when stdout is a pipe
            # and the liveness signal shows up in 4KB clumps.
            bufsize=1,
            universal_newlines=True,
        )
        self.status.feed_pid = self.proc.pid
        t = threading.Thread(target=self._pump, daemon=True)
        t.start()
        return self.proc

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.status.saw_line(line)
            with self._lock:
                self.tail.append(line)
                if len(self.tail) > 40:
                    self.tail.pop(0)
            if self.echo:
                print(line, flush=True)
            if self.log_file:
                self.log_file.write(line + "\n")
                self.log_file.flush()

    def exit_reason(self):
        """The most useful line the child said on its way out.

        The feed is good about explaining itself — '25 consecutive failed
        samples', 'feed stopped (sender): ...'. Prefer a line that carries a
        reason over the last line, which is often just a log path."""
        with self._lock:
            tail = list(self.tail)
        for line in reversed(tail):
            low = line.lower()
            if any(h in low for h in NO_ADAPTER_HINTS):
                return line.strip()
            if any(h in low for h in REASON_HINTS):
                return line.strip()
        for line in reversed(tail):
            if line.strip():
                return line.strip()
        return ""

    def looks_like_no_adapter(self):
        with self._lock:
            tail = " ".join(self.tail).lower()
        return any(h in tail for h in NO_ADAPTER_HINTS)


class Leg:
    """One supervised child and its restart policy, advanced by tick().

    The policy is the same engine for both legs — STARTING, then LIVE-ish
    or STALLED by pulse age, kill-and-restart past --stall-restart-seconds,
    backoff between runs, healthy runs reset the backoff — and the legs
    differ in what their state object counts as a pulse: the feed's is a
    sample line (the car answered), the overlay's is any status line (the
    process spoke, whatever it said). That one difference IS the per-child
    policy: the MX+ gets killed for wedging, and the GPS gets believed when
    it says LOST.

    Nothing in here blocks. A kill is sent and collected on later ticks, so
    one leg's bad day never stalls the other's judgement or the file."""

    def __init__(self, name, argv, status, log_file, args, echo,
                 stall_kill_seconds):
        self.name = name
        self.argv = argv
        self.status = status
        self.log_file = log_file
        self.args = args
        self.echo = echo
        # 0/None = report only, never kill (the OBD replay exemption and
        # --stall-restart-seconds 0 both arrive here as a falsy value).
        self.stall_kill_seconds = stall_kill_seconds
        self.backoff = args.backoff_start
        self.current = None
        self.run_started = None
        self.stall_note = None
        self.kill_sent = None       # monotonic stamp of terminate()
        self.kill_escalated = False
        self.next_start = 0.0       # monotonic; start when now >= this
        self.stopped = False        # --max-restarts reached, or shut down

    def _say(self, msg):
        print(msg, flush=True)
        self.log_file.write(msg + "\n")
        self.log_file.flush()

    def tick(self):
        if self.stopped:
            return
        now_m = time.monotonic()
        if self.current is None:
            if now_m >= self.next_start:
                self._start(now_m)
            return
        rc = self.current.proc.poll()
        if rc is None:
            self._judge(now_m)
        else:
            self._exited(rc, now_m)

    def _start(self, now_m):
        self.status.set_state("STARTING")
        self.current = FeedProcess(self.argv, self.log_file, self.status,
                                   echo=self.echo)
        self.run_started = now_m
        self.stall_note = None
        self.kill_sent = None
        self.kill_escalated = False
        try:
            self.current.start()
        except OSError as e:
            self.status.last_exit = {"code": None,
                                     "reason": f"could not start {self.name}: {e}"}
            self.status.set_state("NO_ADAPTER")
            self.status.feed_pid = None
            self.current = None
            self.next_start = now_m + self.backoff
            self.backoff = min(self.backoff * 2, self.args.backoff_max)

    def _judge(self, now_m):
        args = self.args
        # One read of the pulse stamp, used for both the age and the
        # per-run test. The pump thread updates it concurrently; with two
        # reads, a first sample landing between them would leave `since`
        # carrying the ancestor's age while the guard saw the newborn's
        # timestamp — and the kill below would fire on a run that had just
        # proved itself alive.
        last_mono = self.status.pulse_mono
        since = (round(now_m - last_mono, 1) if last_mono is not None else None)
        if since is not None and last_mono < self.run_started:
            # That data belonged to the previous run. A fresh child must be
            # judged from its own birth, not its ancestor's last words — a
            # real reconnect spends ~12 silent seconds in adapter reset and
            # autotune, and inheriting stale age here would kill every new
            # run at the starting line.
            since = None
        self.status.judge(since, now_m - self.run_started, args.stall_seconds)

        if self.kill_sent is not None:
            # A kill is in flight; poll() collects the exit. Escalate once
            # if the polite one did not take, and say so once more if the
            # hard one did not either — never let the log claim a restart
            # that never happened.
            if now_m - self.kill_sent > 5 and not self.kill_escalated:
                self.kill_escalated = True
                self.current.proc.kill()
            elif now_m - self.kill_sent > 10 and self.kill_escalated is True:
                self.kill_escalated = "reported"
                self._say(f"supervisor: the kill did not take — the {self.name} "
                          "process is stuck in the kernel; still watching, "
                          "will collect it when the OS releases it")
            return

        # No pulse past the restart budget means the process is wedged, not
        # recovering — for the feed, typically blocked in a serial write on
        # a handle whose adapter lost power. It will never exit on its own,
        # and while it lives it owns the dead port. Reopening the port is
        # the only cure, and that takes a fresh process.
        stalled_for = (since if since is not None else now_m - self.run_started)
        if (self.stall_kill_seconds
                and self.stall_note is None
                and self.status.state == "STALLED"
                and stalled_for > self.stall_kill_seconds):
            what = (f"no {'data' if self.name == 'obd' else 'status line'} for "
                    f"{human_duration(stalled_for)}"
                    if since is not None else
                    f"no {'data' if self.name == 'obd' else 'status line'} this "
                    f"run in {human_duration(stalled_for)}")
            self.stall_note = (f"{what} while the process stayed up — "
                               f"judged wedged by the supervisor "
                               f"(--stall-restart-seconds "
                               f"{self.stall_kill_seconds:.0f}); restarting")
            self._say(f"supervisor: {self.name}: {self.stall_note}")
            self.current.proc.terminate()
            self.kill_sent = now_m

    def _exited(self, rc, now_m):
        args, status = self.args, self.status
        ran_for = now_m - self.run_started
        # A stall-kill leaves the child no chance to explain itself, and its
        # last words would be an ordinary status line — the supervisor is
        # the one who knows why this run ended.
        reason = self.stall_note or self.current.exit_reason()
        status.last_exit = {"code": rc, "reason": reason}
        status.feed_pid = None

        if ran_for >= args.healthy_seconds and status.pulses:
            # It worked for a real stretch before dying, so this is a fresh
            # failure and not a tight crash loop. Start over gently.
            self.backoff = args.backoff_start

        status.restarts += 1
        status.last_restart = now()
        if args.max_restarts and status.restarts > args.max_restarts:
            status.set_state("STOPPED")
            status.last_exit = {"code": rc,
                                "reason": f"stopped after {args.max_restarts} "
                                          f"restarts (--max-restarts)"}
            self.current = None
            self.stopped = True
            return

        status.set_state("NO_ADAPTER" if self.current.looks_like_no_adapter()
                         else "RECONNECTING")
        self._say(f"\nsupervisor: {self.name} exited (code {rc}) after "
                  f"{human_duration(ran_for)} — {reason or 'no reason given'}")
        print(f"supervisor: restarting {self.name} in {self.backoff:.0f}s "
              f"(restart #{status.restarts})\n", flush=True)
        self.current = None
        self.next_start = now_m + self.backoff
        self.backoff = min(self.backoff * 2, args.backoff_max)

    def shutdown(self):
        self.stopped = True
        if self.current and self.current.proc and self.current.proc.poll() is None:
            self.current.proc.terminate()
            try:
                self.current.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.current.proc.kill()
        if self.status.state != "STOPPED":
            self.status.set_state("STOPPED")
        self.status.feed_pid = None


def _forward_config(args, child_args):
    """A supervisor pointed at a non-default config file supervises children
    that must read the same one — two processes disagreeing about which
    file governs is a bug report nobody can reproduce. The default is not
    forwarded: the child finds that on its own, and an explicit path that
    does not exist should fail loudly, not implicitly."""
    child_args = list(child_args)
    if (args.config != default_config_path()
            and not any(a == "--config" or a.startswith("--config=")
                        for a in child_args)):
        child_args = ["--config", args.config] + child_args
    return child_args


def build_feed_argv(args, feed_args):
    feed = args.feed or os.path.join(REPO, "extractor", "obd_feed.py")
    if not os.path.exists(feed):
        sys.exit(f"cannot find the feed at {feed} — pass --feed to point at it")
    # -u: unbuffered child, so the per-second status line arrives per second.
    return [args.python, "-u", feed] + _forward_config(args, feed_args)


def build_gps_argv(args):
    overlay = args.gps_overlay or os.path.join(REPO, "gps", "gps_overlay.py")
    if not os.path.exists(overlay):
        sys.exit(f"cannot find the GPS overlay at {overlay} — pass "
                 f"--gps-overlay to point at it")
    # Split on whitespace, no quoting: a Windows path with a backslash must
    # survive, and anything that needs quoting belongs in config.json's
    # gps_overlay section, which the overlay reads on its own.
    gps_args = (args.gps_args or "").split()
    return [args.python, "-u", overlay] + _forward_config(args, gps_args)


def build_parser():
    ap = argparse.ArgumentParser(
        description="Keep obd_feed.py (and, with --gps, gps_overlay.py) alive "
                    "and publish a status file.",
        epilog="Everything after -- is passed to obd_feed.py untouched.",
    )
    ap.add_argument("--status-file",
                    default=os.path.join(REPO, "status", "obd2_status.json"),
                    help="where to publish status (default: status/obd2_status.json)")
    ap.add_argument("--status-interval", type=float, default=1.0,
                    help="seconds between status file writes (default: 1)")
    ap.add_argument("--stall-seconds", type=float, default=10.0,
                    help="no data for this long, while the feed is still "
                         "running, means STALLED (default: 10). For the GPS "
                         "leg 'data' is any status line at all — LOST counts")
    ap.add_argument("--stall-restart-seconds", type=float, default=45.0,
                    help="kill and restart the feed if it is still alive "
                         "after this many seconds without data, counted "
                         "within the current run — a serial write blocked on "
                         "a dead handle cannot exit on its own. Keep it "
                         "comfortably above --stall-seconds so STALLED gets "
                         "reported before it escalates. Never applies to "
                         "--replay runs. The GPS leg is killed only for this "
                         "many seconds with NO status line — never for LOST. "
                         "0 = report only, never kill (default: 45)")
    ap.add_argument("--backoff-start", type=float, default=2.0)
    ap.add_argument("--backoff-max", type=float, default=60.0)
    ap.add_argument("--healthy-seconds", type=float, default=60.0,
                    help="a run that carried data this long is judged healthy, "
                         "and the backoff resets (default: 60)")
    ap.add_argument("--max-restarts", type=int, default=0,
                    help="stop a leg after N restarts (default 0 = never stop; "
                         "for tests, not for the car)")
    ap.add_argument("--feed", help="path to obd_feed.py")
    ap.add_argument("--gps", action="store_true",
                    help="also run and watch gps/gps_overlay.py as a second "
                         "child. It reads config.json's gps_overlay section "
                         "on its own (its port lives there), so this flag is "
                         "usually the whole story; --gps-args for the rest")
    ap.add_argument("--gps-args", default="",
                    help="command line for the GPS overlay, in one quoted "
                         "string split on spaces: --gps-args \"--port COM5\" "
                         "or --gps-args \"--replay runs\\gps-last.txt\". "
                         "Anything that needs quoting goes in config.json")
    ap.add_argument("--gps-overlay", help="path to gps_overlay.py")
    ap.add_argument("--gps-fix-age-seconds", type=float, default=30.0,
                    help="a GPS line that says ok about a fix older than "
                         "this reads as LOST — the port went quiet without "
                         "saying so (default: 30)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--log-dir", default=os.path.join(REPO, "runs"),
                    help="where to keep the supervisor log (default: runs/)")
    ap.add_argument("--run-log", choices=["full", "tail", "off"],
                    default="tail",
                    help="the disk copy of this log: 'tail' (default) keeps "
                         "ONE size-capped file, supervisor-last.log, "
                         "overwritten every start; 'full' keeps a "
                         "timestamped file per start; 'off' writes nothing "
                         "(console echo and the status file are unaffected)")
    ap.add_argument("--quiet", action="store_true",
                    help="do not echo the children's output to this console")
    return ap


def main():
    args, feed_args = parse_with_config(build_parser(), "supervisor", known=True)
    if feed_args and feed_args[0] == "--":
        feed_args = feed_args[1:]

    # A --config typed after the -- would govern the feed but not this
    # process — two processes on different files is a bug report nobody
    # can reproduce. Refuse rather than split. Same for the GPS args.
    def _has_config(argv):
        return any(a == "--config" or a.startswith("--config=") for a in argv)
    if _has_config(feed_args):
        sys.exit("supervisor: put --config BEFORE the -- separator — one "
                 "config file governs both the supervisor and the feed")
    if args.gps and _has_config((args.gps_args or "").split()):
        sys.exit("supervisor: put --config on the supervisor, not in "
                 "--gps-args — one config file governs every process here")

    argv = build_feed_argv(args, feed_args)
    # Replay drives the status file's honesty and the stall-kill
    # exemption, and it can arrive from config.json as well as the
    # command line — so ask the feed's own parser what it will decide,
    # rather than string-matching argv and missing the config path.
    # argv[3:] is exactly the child's command line (after
    # [python, -u, feed]), including any forwarded --config.
    replay = bool(resolved_defaults("obd_feed", argv[3:]).replay)
    gps_argv = build_gps_argv(args) if args.gps else None

    log_file = LogSink(args.log_dir, args.run_log)

    stall_restart = (f"{args.stall_restart_seconds:g}s"
                     if args.stall_restart_seconds else "off (report only)")
    settings = {
        "stall_seconds": args.stall_seconds,
        "stall_restart_seconds": args.stall_restart_seconds,
        "status_interval": args.status_interval,
        "mode": "replay" if replay else "live",
    }
    gps = None
    if args.gps:
        settings["gps_fix_age_seconds"] = args.gps_fix_age_seconds
        gps = GpsStatus(fix_age_seconds=args.gps_fix_age_seconds)

    status = Status(args.status_file, stale_after_s=int(args.status_interval * 5 + 5),
                    replay=replay, settings=settings, gps=gps)
    status.write()

    stopping = threading.Event()

    def on_signal(signum, _frame):
        stopping.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):
        # Windows only. Console ctrl-c already arrives as SIGINT; this is the one
        # a parent process (a launcher, the tests) can send to ask us to stop.
        signal.signal(signal.SIGBREAK, on_signal)

    # The banner goes to the LOG as well as the console, and it names the
    # watchdog settings. When someone mails me a log from a driveway, "was
    # the stall-restart even in this copy?" has to be answerable from the
    # attachment — not from a question I ask them a day later while the car
    # sits in a different state.
    banner = [
        f"supervisor: watching {' '.join(argv)}",
    ]
    if gps_argv:
        banner.append(f"supervisor: watching {' '.join(gps_argv)}")
    banner += [
        f"supervisor: status  -> {args.status_file}",
        f"supervisor: log     -> {log_file.path or '(off)'}"
        + (f"  (this session only; previous kept at {LogSink.PREV}; "
           f"--run-log full to keep everything)"
           if log_file.mode == "tail" else "") + log_file.note,
        f"supervisor: stalled after {args.stall_seconds:g}s without data; "
        f"kill-and-restart a wedged feed after {stall_restart}",
    ]
    if gps_argv:
        banner.append(
            f"supervisor: the GPS leg is believed when it says LOST; it is "
            f"restarted only if it exits or prints no status line for "
            f"{stall_restart}")
    banner.append("supervisor: ctrl-c to stop.")
    for line in banner:
        print(line)
        log_file.write(line + "\n")
    print()
    log_file.write("\n")
    log_file.flush()

    legs = [Leg("obd", argv, status, log_file, args, echo=not args.quiet,
                stall_kill_seconds=(0 if replay else args.stall_restart_seconds))]
    if gps_argv:
        # No replay exemption here: a GPS replay keeps printing (LOST at
        # EOF, with the reason), so the only way it goes silent is a wedge.
        legs.append(Leg("gps", gps_argv, gps, log_file, args,
                        echo=not args.quiet,
                        stall_kill_seconds=args.stall_restart_seconds))

    try:
        while not stopping.is_set():
            for leg in legs:
                leg.tick()
            status.write()
            if all(leg.stopped for leg in legs):
                break
            stopping.wait(args.status_interval)
    finally:
        for leg in legs:
            leg.shutdown()
        status.write()
        print("\nsupervisor: stopped. Status file left at "
              f"{args.status_file} saying STOPPED.")
        log_file.close()


if __name__ == "__main__":
    main()
