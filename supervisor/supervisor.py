#!/usr/bin/env python3
"""
supervisor.py — phase 3: keep the feed alive without a keyboard
===============================================================

Phase 2 answered "can the car talk to SimHub." This answers "can it keep
doing that for three hours while your hands are full." It owns one thing
only: the lifecycle of extractor/obd_feed.py. Start it, watch it, restart
it, forever, never exit.

Deliberately NOT in scope: OBS, VoiceAttack, SimHub itself, the router,
the MX+ pairing. Those are other people's processes with their own ideas
about startup, and a supervisor that tries to restart OBS mid-event can
do more harm than the failure it is fixing. This one babysits the
extractor, where it can actually detect state and recover it.

Usage (Windows, the real thing):
  py supervisor\\supervisor.py -- --port COM3

Usage (no car — against the fake car in another terminal):
  python extractor/fake_car.py --tcp 35000
  python supervisor/supervisor.py -- --port socket://127.0.0.1:35000

Everything after `--` is handed to obd_feed.py untouched, so any flag the
feed grows works here on the day it lands, with no change to this file.

WHAT IT WATCHES, AND WHY THAT WAY
The feed already prints a status line every second with flush=True. That
line is the liveness signal: seeing one means the car answered and packets
went out. So the supervisor reads the child's stdout rather than asking
the feed to grow a heartbeat channel — no change to obd_feed.py, and the
signal means "data actually moved," not "the process is still resident."
A process can be alive and wedged. A printed sample cannot.

Three failures, three responses:
  - feed exits (25 misses, sender death, crash) -> restart after backoff
  - feed alive but silent past --stall-seconds  -> report STALLED, keep watching
  - the adapter was never there                 -> report NO_ADAPTER, keep retrying

The 25-miss exit is the one that matters at an event: it means power-cycle
the adapter. The supervisor cannot power-cycle it for you, but it retries
forever — so when you do pull and replug the MX+, the feed comes back on
its own and you never touch the keyboard.

THE STATUS FILE
Written to status/obd2_status.json (atomically — a reader never sees a
half-written file). It carries a plain-English `summary` line meant to be
dropped straight into PitGirl's context, plus the structured detail
underneath for anything that wants to be precise.

One thing the reader MUST do: check `updated_at` against `stale_after_s`.
If the file is older than that, the supervisor itself is gone and the file
is a photograph, not a status. A stale file still says LIVE — that is the
oldest trap in monitoring, and this one is only avoided on the reading
side. Both fields are in the file so the check is possible without knowing
anything about this program.

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
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# The feed's per-second status line: "  t   42s  RPM  2100  speed ...".
# Replay mode prints the same shape, so one pattern covers both.
SAMPLE_LINE = re.compile(r"^\s*t\s+\d+s\s+RPM\s")

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


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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


class Status:
    """The supervisor's view of the world, and the file it writes it to.

    Every field here is either directly observed or derived from something
    observed. Nothing is assumed to persist: if the supervisor cannot see
    it this second, it does not claim it.
    """

    def __init__(self, path, stale_after_s, replay):
        self.path = path
        self.stale_after_s = stale_after_s
        self.replay = replay
        self.started = utc_now()
        self.state = "STARTING"
        self.state_since = utc_now()
        self.last_data = None          # wall clock of last sample line
        self.last_data_mono = None     # monotonic, for the staleness maths
        self.restarts = 0
        self.last_restart = None
        self.last_exit = None          # {"code": int, "reason": str}
        self.feed_pid = None
        self.total_samples = 0

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.state_since = utc_now()

    def saw_sample(self):
        self.last_data = utc_now()
        self.last_data_mono = time.monotonic()
        self.total_samples += 1

    def seconds_since_data(self):
        if self.last_data_mono is None:
            return None
        return round(time.monotonic() - self.last_data_mono, 1)

    def summary(self):
        """One sentence, spoken aloud, no jargon the driver has to decode."""
        in_state = human_duration((utc_now() - self.state_since).total_seconds())
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

    def healthy(self):
        return self.state == "LIVE"

    def snapshot(self):
        now = utc_now()
        return {
            "schema": 1,
            "state": self.state,
            "healthy": self.healthy(),
            "summary": self.summary(),
            "updated_at": iso(now),
            "updated_unix": int(now.timestamp()),
            # If updated_at is older than this, the SUPERVISOR is gone and
            # everything above is a photograph. Check it before believing it.
            "stale_after_s": self.stale_after_s,
            "detail": {
                "mode": "replay" if self.replay else "live",
                "state_since": iso(self.state_since),
                "seconds_in_state": int((now - self.state_since).total_seconds()),
                "last_data_at": iso(self.last_data) if self.last_data else None,
                "seconds_since_data": self.seconds_since_data(),
                "samples_seen": self.total_samples,
                "restarts": self.restarts,
                "last_restart_at": iso(self.last_restart) if self.last_restart else None,
                "last_exit": self.last_exit,
                "feed_pid": self.feed_pid,
                "supervisor_started_at": iso(self.started),
                "supervisor_uptime_s": int((now - self.started).total_seconds()),
            },
        }

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


class FeedProcess:
    """One run of obd_feed.py, and the thread that drains its output."""

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
            if SAMPLE_LINE.match(line):
                self.status.saw_sample()
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
        """The most useful line the feed said on its way out.

        The feed is good about explaining itself — '25 consecutive failed
        samples', 'feed stopped (sender): ...'. Prefer a line that carries a
        reason over the last line, which is often just a log path."""
        with self._lock:
            tail = list(self.tail)
        for line in reversed(tail):
            low = line.lower()
            if any(h in low for h in NO_ADAPTER_HINTS):
                return line.strip()
            if "consecutive failed samples" in low or "feed stopped" in low:
                return line.strip()
        for line in reversed(tail):
            if line.strip():
                return line.strip()
        return ""

    def looks_like_no_adapter(self):
        with self._lock:
            tail = " ".join(self.tail).lower()
        return any(h in tail for h in NO_ADAPTER_HINTS)


def build_feed_argv(args, feed_args):
    feed = args.feed or os.path.join(REPO, "extractor", "obd_feed.py")
    if not os.path.exists(feed):
        sys.exit(f"cannot find the feed at {feed} — pass --feed to point at it")
    # -u: unbuffered child, so the per-second status line arrives per second.
    return [args.python, "-u", feed] + list(feed_args)


def main():
    ap = argparse.ArgumentParser(
        description="Keep obd_feed.py alive and publish a status file.",
        epilog="Everything after -- is passed to obd_feed.py untouched.",
    )
    ap.add_argument("--status-file",
                    default=os.path.join(REPO, "status", "obd2_status.json"),
                    help="where to publish status (default: status/obd2_status.json)")
    ap.add_argument("--status-interval", type=float, default=1.0,
                    help="seconds between status file writes (default: 1)")
    ap.add_argument("--stall-seconds", type=float, default=10.0,
                    help="no data for this long, while the feed is still "
                         "running, means STALLED (default: 10)")
    ap.add_argument("--backoff-start", type=float, default=2.0)
    ap.add_argument("--backoff-max", type=float, default=60.0)
    ap.add_argument("--healthy-seconds", type=float, default=60.0,
                    help="a run that carried data this long is judged healthy, "
                         "and the backoff resets (default: 60)")
    ap.add_argument("--max-restarts", type=int, default=0,
                    help="stop after N restarts (default 0 = never stop; for "
                         "tests, not for the car)")
    ap.add_argument("--feed", help="path to obd_feed.py")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--log-dir", default=os.path.join(REPO, "runs"),
                    help="where to keep the supervisor log (default: runs/)")
    ap.add_argument("--quiet", action="store_true",
                    help="do not echo the feed's output to this console")
    args, feed_args = ap.parse_known_args()
    if feed_args and feed_args[0] == "--":
        feed_args = feed_args[1:]

    argv = build_feed_argv(args, feed_args)
    replay = any(a == "--replay" or a.startswith("--replay=") for a in feed_args)

    os.makedirs(args.log_dir, exist_ok=True)
    log_path = os.path.join(
        args.log_dir, f"supervisor-{utc_now().strftime('%Y%m%d-%H%M%S')}.log")
    log_file = open(log_path, "a", encoding="utf-8")

    status = Status(args.status_file, stale_after_s=int(args.status_interval * 5 + 5),
                    replay=replay)
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

    print(f"supervisor: watching {' '.join(argv)}")
    print(f"supervisor: status  -> {args.status_file}")
    print(f"supervisor: log     -> {log_path}")
    print("supervisor: ctrl-c to stop.\n")

    backoff = args.backoff_start
    current = None

    try:
        while not stopping.is_set():
            status.set_state("STARTING")
            status.write()
            current = FeedProcess(argv, log_file, status, echo=not args.quiet)
            run_started = time.monotonic()
            try:
                current.start()
            except OSError as e:
                status.last_exit = {"code": None, "reason": f"could not start the feed: {e}"}
                status.set_state("NO_ADAPTER")
                status.write()
                if not stopping.wait(backoff):
                    backoff = min(backoff * 2, args.backoff_max)
                    continue
                break

            # Watch this run until the child exits or we are told to stop.
            while not stopping.is_set():
                rc = current.proc.poll()
                if rc is not None:
                    break
                since = status.seconds_since_data()
                if since is None:
                    # No data yet this run. Give it the stall budget to
                    # connect before calling it anything worse than STARTING.
                    if time.monotonic() - run_started > args.stall_seconds:
                        status.set_state("STALLED")
                elif since > args.stall_seconds:
                    status.set_state("STALLED")
                else:
                    status.set_state("LIVE")
                status.write()
                stopping.wait(args.status_interval)

            if stopping.is_set():
                break

            rc = current.proc.wait()
            ran_for = time.monotonic() - run_started
            reason = current.exit_reason()
            status.last_exit = {"code": rc, "reason": reason}
            status.feed_pid = None

            if ran_for >= args.healthy_seconds and status.total_samples:
                # It worked for a real stretch before dying, so this is a
                # fresh failure and not a tight crash loop. Start over gently.
                backoff = args.backoff_start

            status.restarts += 1
            status.last_restart = utc_now()
            if args.max_restarts and status.restarts > args.max_restarts:
                status.set_state("STOPPED")
                status.last_exit = {"code": rc,
                                    "reason": f"stopped after {args.max_restarts} "
                                              f"restarts (--max-restarts)"}
                status.write()
                break

            status.set_state("NO_ADAPTER" if current.looks_like_no_adapter()
                             else "RECONNECTING")
            status.write()
            msg = (f"\nsupervisor: feed exited (code {rc}) after "
                   f"{human_duration(ran_for)} — {reason or 'no reason given'}")
            print(msg, flush=True)
            log_file.write(msg + "\n")
            print(f"supervisor: restarting in {backoff:.0f}s "
                  f"(restart #{status.restarts})\n", flush=True)

            if stopping.wait(backoff):
                break
            backoff = min(backoff * 2, args.backoff_max)
    finally:
        if current and current.proc and current.proc.poll() is None:
            current.proc.terminate()
            try:
                current.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                current.proc.kill()
        if status.state != "STOPPED":
            status.set_state("STOPPED")
        status.feed_pid = None
        status.write()
        print("\nsupervisor: stopped. Status file left at "
              f"{args.status_file} saying STOPPED.")
        log_file.close()


if __name__ == "__main__":
    main()
