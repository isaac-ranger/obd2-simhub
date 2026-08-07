#!/usr/bin/env python3
"""
fake_car.py — a fake ELM327 + car for developing the feed without a car
=======================================================================

The pinned ELM327-emulator can't answer batched multi-PID requests (README:
"Development without a car"), which is exactly what the phase 2 feed sends.
This one can: it answers a 6-PID Mode 01 batch the way Kris's real 982 does —
one ISO-TP reply spanning multiple frames — and it drives itself through the
gears meanwhile, so the whole pipeline (batch request, multi-frame parse,
gear inference, UDP feed) runs against it end to end.

  python extractor/fake_car.py --tcp 35000
  python extractor/obd_feed.py --port socket://127.0.0.1:35000

The simulated drive: idle, then pull away and shift up through all six gears
using learned constants from calibration.json when it has them, cruise, and back
down. Not a physics model — a gear-shaped signal generator. The point is
that the feed's readout should agree with what this script *meant*.

Stdlib only. Also importable by test_feed.py (serve_once / FakeCar).
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)

sys.path.insert(0, REPO)
from obd_config import parse_with_config

# PID payload sizes for the PIDs this fake car supports.
SUPPORTED = {0x04: 1, 0x05: 1, 0x0C: 2, 0x0D: 1, 0x11: 1,
             0x2F: 1, 0x33: 1, 0x42: 2, 0x5C: 1}


def _bitmap(base, pids, more):
    """Supported-PID bitmap for 01{base:02X}: PIDs base+1..base+32."""
    val = 0
    for p in pids:
        if base < p <= base + 32:
            val |= 1 << (31 - (p - base - 1))
    if more:
        val |= 1  # bit for base+0x20: "further bitmaps follow"
    return val


class FakeCar:
    """Scripted drive with a real gearbox shape."""

    # A six-speed shape to simulate when the calibration carries none. The
    # shipped calibration.json deliberately has no learned gears — a clone
    # must not inherit one car's ratios — and the simulator is the one
    # consumer that should keep working anyway: it invents a car by
    # definition, so it needs plausible ratios, not YOUR ratios.
    DEFAULT_CONSTANTS = [103.0, 60.4, 43.3, 35.0, 29.2, 25.2]

    def __init__(self, constants=None):
        if constants is None:
            with open(os.path.join(REPO, "calibration.json"),
                      encoding="utf-8") as f:
                constants = json.load(f).get("gears", {}).get(
                    "rpm_per_kmh") or self.DEFAULT_CONSTANTS
        self.constants = constants
        self.t0 = time.monotonic()
        # (duration_s, gear, start_speed, end_speed) — gear 0 = clutch in /
        # neutral moment between gears (rpm decoupled from speed).
        plan = [(3.0, 0, 0.0, 0.0)]
        speeds = [(20, 35), (35, 55), (55, 75), (75, 95), (95, 115),
                  (115, 130)]
        v = 10.0
        for g, (lo, hi) in enumerate(speeds, start=1):
            plan.append((0.6, 0, v, float(lo)))     # shift moment
            plan.append((4.0, g, float(lo), float(hi)))
            v = float(hi)
        plan.append((3.0, 6, 130.0, 130.0))         # cruise
        for g in (5, 4, 3, 2, 1):
            lo, hi = speeds[g - 1]
            plan.append((0.6, 0, v, float(hi)))
            plan.append((2.5, g, float(hi), float(lo)))
            v = float(lo)
        plan.append((3.0, 0, 8.0, 0.0))             # roll to a stop
        self.plan = plan
        self.total = sum(d for d, *_ in plan)

    def sample(self, t=None):
        """(gear, rpm, speed_kmh, throttle_pct) at elapsed time t (loops)."""
        if t is None:
            t = time.monotonic() - self.t0
        t %= self.total
        for dur, gear, v0, v1 in self.plan:
            if t <= dur:
                v = v0 + (v1 - v0) * (t / dur if dur else 1.0)
                if gear > 0 and v > 0:
                    rpm = self.constants[gear - 1] * v
                    throttle = 25.0 if v1 >= v0 else 8.0
                elif v > 0:
                    # clutch in: revs DECAY, they don't hover — a pinned rpm
                    # with moving speed sweeps the ratio slowly enough to
                    # look like a genuine (phantom) gear. QA caught the
                    # readout believing it. Real shifts are fast and messy;
                    # be fast and messy.
                    rpm = max(900.0, 2400.0 - 2500.0 * t)
                    throttle = 5.0
                else:
                    rpm = 800.0                      # idle
                    throttle = 0.0
                return gear, rpm, max(0.0, v), throttle
            t -= dur
        return 0, 800.0, 0.0, 0.0

    def pid_bytes(self, pid):
        gear, rpm, speed, throttle = self.sample()
        if pid == 0x0C:
            n = max(0, min(0xFFFF, int(rpm * 4)))
            return bytes((n >> 8, n & 0xFF))
        if pid == 0x0D:
            return bytes((max(0, min(255, int(speed))),))
        if pid == 0x11:
            return bytes((int(throttle * 255 / 100),))
        if pid == 0x04:
            return bytes((int(throttle * 2.2 * 255 / 100) & 0xFF,))
        if pid == 0x05:
            return bytes((90 + 40,))                 # 90 C, warmed up
        if pid == 0x5C:
            return bytes((104 + 40,))                # 104 C oil
        if pid == 0x2F:
            return bytes((int(0.62 * 255),))         # 62% fuel
        if pid == 0x33:
            return bytes((101,))                     # 101 kPa
        if pid == 0x42:
            return bytes((0x36, 0x9C))               # 13.980 V
        return None


class ElmFront:
    """The ELM327 dialect, one connection's worth. Talks CRLF, echoes '>'."""

    def __init__(self, car):
        self.car = car

    def _mode01(self, hexstr):
        pids = [int(hexstr[i:i + 2], 16) for i in range(0, len(hexstr), 2)]
        payload = bytearray([0x41])
        for pid in pids:
            if pid in (0x00, 0x20, 0x40):
                more = pid < 0x40
                payload += bytes([pid]) + _bitmap(pid, SUPPORTED, more) \
                    .to_bytes(4, "big")
            elif pid in SUPPORTED:
                data = self.car.pid_bytes(pid)
                if data is not None:
                    payload += bytes([pid]) + data
        if len(payload) == 1:
            return ["NO DATA"]
        hexpay = payload.hex().upper()
        if len(payload) <= 7:
            return [hexpay]                          # single frame
        # ISO-TP multi-frame, the way the probe's parser expects it:
        # 3-hex-digit byte count, then 0:/1:/2: continuation lines.
        lines = [f"{len(payload):03X}"]
        first, rest = hexpay[:12], hexpay[12:]
        lines.append(f"0:{first}")
        idx = 1
        while rest:
            lines.append(f"{idx:X}:{rest[:14]}")
            rest = rest[14:]
            idx += 1
        return lines

    def handle(self, cmd):
        c = cmd.strip().upper().replace(" ", "")
        if not c:
            return []
        if c == "ATZ":
            return ["ELM327 v1.4b (fake car)"]
        if c == "ATRV":
            return ["13.9V"]
        if c == "ATDPN":
            return ["A6"]
        if c.startswith("AT"):
            return ["OK"]
        if c == "STI":
            return ["?"]                             # generic ELM, not an STN
        if c.startswith("01") and re.fullmatch(r"[0-9A-F]+", c[2:] or "x"):
            h = c[2:]
            if len(h) % 2 == 1:
                h = h[:-1]   # trailing expected-response-count digit —
                             # honored by not dawdling, which we never did
            return self._mode01(h) if h else ["NO DATA"]
        return ["?"]


def serve(port, car=None, once=False, quiet=False):
    car = car or FakeCar()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    # Short timeout so Ctrl-C can interrupt accept()/recv() on Windows,
    # where SIGINT does not wake a blocking socket call.
    srv.settimeout(0.5)
    if not quiet:
        print(f"fake car on socket://127.0.0.1:{srv.getsockname()[1]}  "
              f"(drive loop {car.total:.0f}s; Ctrl-C to stop)")
    try:
        while True:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            front = ElmFront(car)
            buf = b""
            try:
                conn.settimeout(0.5)
                conn.sendall(b">")
                while True:
                    try:
                        chunk = conn.recv(256)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\r" in buf:
                        cmd, buf = buf.split(b"\r", 1)
                        lines = front.handle(cmd.decode("ascii",
                                                        errors="replace"))
                        out = "".join(l + "\r\n" for l in lines) + ">"
                        conn.sendall(out.encode("ascii"))
            except (ConnectionError, OSError):
                pass
            finally:
                conn.close()
            if once:
                return
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


def serve_background(port=0):
    """For tests: serve one connection on an ephemeral port; return (port,
    thread). Port 0 lets the OS pick."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    real_port = srv.getsockname()[1]
    car = FakeCar()

    def _one():
        conn, _ = srv.accept()
        front = ElmFront(car)
        buf = b""
        try:
            conn.sendall(b">")
            while True:
                chunk = conn.recv(256)
                if not chunk:
                    break
                buf += chunk
                while b"\r" in buf:
                    cmd, buf2 = buf.split(b"\r", 1)
                    buf = buf2
                    lines = front.handle(cmd.decode("ascii",
                                                    errors="replace"))
                    conn.sendall(("".join(l + "\r\n" for l in lines)
                                  + ">").encode("ascii"))
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()
            srv.close()

    th = threading.Thread(target=_one, daemon=True)
    th.start()
    return real_port, th


def build_parser():
    ap = argparse.ArgumentParser(description="fake ELM327 + car (TCP)")
    ap.add_argument("--tcp", type=int, default=35000,
                    help="TCP port to listen on (default 35000)")
    return ap


def main():
    args = parse_with_config(build_parser(), "fake_car")
    serve(args.tcp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
