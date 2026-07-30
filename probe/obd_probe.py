#!/usr/bin/env python3
"""
obd_probe.py — OBD2 adapter + vehicle capability probe
======================================================

Phase 1 of the OBD2 -> SimHub bridge project. Answers, on YOUR car with YOUR
adapter, the questions the bridge design depends on:

  1. Does the adapter respond, and what is it? (ELM327 vs STN / OBDLink)
  2. What protocol does the car speak? (CAN vs older = big rate difference)
  3. Which dashboard-relevant PIDs does the ECU actually expose?
  4. How fast can we poll — one PID at a time, and batched (CAN only)?

Usage (Windows, OBDLink MX+ over Bluetooth):
  py obd_probe.py --list-ports          # find the OUTGOING Bluetooth COM port
  py obd_probe.py --port COM5           # run the probe (engine running!)

Usage (development, against ELM327-emulator):
  python -m elm -n 35000 -s car         # in another terminal
  python obd_probe.py --port socket://127.0.0.1:35000

Requires:  python 3.9+,  pip install pyserial
Output:    human-readable report; add --json report.json for machine-readable.

Safety: read-only. Sends only Mode 01 data requests and ELM config commands.
Never writes to the ECU, never clears codes.
"""

import argparse
import json
import re
import sys
import time

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

PROMPT = b">"

# Mode 01 PID payload sizes (bytes) — needed to walk batched responses.
PID_LEN = {
    0x00: 4, 0x01: 4, 0x03: 2, 0x04: 1, 0x05: 1, 0x06: 1, 0x07: 1,
    0x08: 1, 0x09: 1, 0x0A: 1, 0x0B: 1, 0x0C: 2, 0x0D: 1, 0x0E: 1,
    0x0F: 1, 0x10: 2, 0x11: 1, 0x1F: 2, 0x20: 4, 0x21: 2, 0x22: 2,
    0x23: 2, 0x2C: 1, 0x2E: 1, 0x2F: 1, 0x33: 1, 0x40: 4, 0x42: 2,
    0x43: 2, 0x44: 2, 0x45: 1, 0x46: 1, 0x47: 1, 0x49: 1, 0x4A: 1,
    0x4C: 1, 0x51: 1, 0x52: 1, 0x5A: 1, 0x5C: 1, 0x5E: 2, 0x60: 4,
    0x61: 1, 0x62: 1, 0x63: 2, 0x80: 4, 0xA0: 4, 0xC0: 4,
}

# The PIDs a dashboard/overlay project cares about, with decoders.
DASH_PIDS = {
    0x0C: ("Engine RPM",        "rpm",  lambda d: (d[0] * 256 + d[1]) / 4.0),
    0x0D: ("Vehicle speed",     "km/h", lambda d: float(d[0])),
    0x11: ("Throttle position", "%",    lambda d: d[0] * 100.0 / 255.0),
    0x04: ("Engine load",       "%",    lambda d: d[0] * 100.0 / 255.0),
    0x05: ("Coolant temp",      "C",    lambda d: d[0] - 40.0),
    0x0F: ("Intake air temp",   "C",    lambda d: d[0] - 40.0),
    0x0B: ("Manifold pressure", "kPa",  lambda d: float(d[0])),
    0x10: ("MAF flow",          "g/s",  lambda d: (d[0] * 256 + d[1]) / 100.0),
    0x2F: ("Fuel level",        "%",    lambda d: d[0] * 100.0 / 255.0),
    0x42: ("Module voltage",    "V",    lambda d: (d[0] * 256 + d[1]) / 1000.0),
    0x5C: ("Oil temp",          "C",    lambda d: d[0] - 40.0),
    0x5E: ("Fuel rate",         "L/h",  lambda d: (d[0] * 256 + d[1]) / 20.0),
    0x0E: ("Timing advance",    "deg",  lambda d: d[0] / 2.0 - 64.0),
    0x33: ("Barometric press.", "kPa",  lambda d: float(d[0])),
}

# Poll priority when building batched requests (max 6 PIDs per CAN message).
BATCH_PRIORITY = [0x0C, 0x0D, 0x11, 0x04, 0x05, 0x42, 0x0F, 0x0B, 0x10]

PROTOCOL_NAMES = {
    "1": "SAE J1850 PWM (41.6 kbaud)",
    "2": "SAE J1850 VPW (10.4 kbaud)",
    "3": "ISO 9141-2 (5 baud init)",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11-bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29-bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11-bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29-bit, 250 kbaud)",
    "A": "SAE J1939 CAN (29-bit, 250 kbaud)",
}


class ElmError(Exception):
    pass


class Elm:
    """Minimal, robust ELM327/STN talker over pyserial (COM port or socket://)."""

    def __init__(self, port, baud=115200, timeout=6.0):
        # serial_for_url handles both real ports (COM5, /dev/rfcomm0)
        # and URLs (socket://host:port) with one code path.
        self.ser = serial.serial_for_url(port, baudrate=baud, timeout=0.2)
        self.timeout = timeout
        self.log = []

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def cmd(self, command, timeout=None):
        """Send one command, read until the '>' prompt, return cleaned lines."""
        deadline = time.perf_counter() + (timeout or self.timeout)
        self.ser.reset_input_buffer()
        self.ser.write(command.encode("ascii") + b"\r")
        self.ser.flush()
        buf = bytearray()
        while time.perf_counter() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                if PROMPT in buf:
                    break
        else:
            self.log.append(f"TIMEOUT waiting for prompt after {command!r}")
        text = buf.decode("ascii", errors="replace")
        self.log.append(f"> {command}  ->  {text!r}")
        lines = []
        for raw in re.split(r"[\r\n]+", text):
            line = raw.strip()
            if not line or line == ">":
                continue
            # Echo of our own command (in case ATE0 hasn't landed yet)
            if line.upper().replace(" ", "") == command.upper().replace(" ", ""):
                continue
            if line.upper() in ("SEARCHING...", "BUS INIT...", "BUS INIT: OK"):
                continue
            lines.append(line)
        return lines


def parse_mode01(lines):
    """Parse ELM output lines for a Mode 01 request into ({pid: bytes}, responders).

    Handles: single-frame ('410C1AF8'), multi-frame ISO-TP ('00D' length
    header then '0:...'/'1:...' continuation lines), spaces on or off,
    multiple ECUs responding (each full '41...' line parsed; first responder
    wins per PID), and the ELM error vocabulary.
    """
    status_errors = {"NO DATA", "STOPPED", "CAN ERROR", "BUS ERROR",
                     "UNABLE TO CONNECT", "DATA ERROR", "BUFFER FULL",
                     "FB ERROR", "ACT ALERT", "ERR"}
    cleaned = []
    for line in lines:
        u = line.upper().replace(" ", "")
        if line.strip() == "?":
            raise ElmError("command rejected ('?') — not supported by adapter")
        if any(u.startswith(e.replace(" ", "")) for e in status_errors):
            raise ElmError(line.strip())
        cleaned.append(u)

    # Reassemble ISO-TP multi-frame: length header line (3 hex digits),
    # then 'N:payload' lines in order.
    payloads = []           # list of hex payload strings, one per responder
    mf_total = None         # expected byte count for multi-frame reassembly
    mf_parts = {}
    for u in cleaned:
        m = re.match(r"^([0-9A-F]):(.*)$", u)
        if m:
            mf_parts[int(m.group(1), 16)] = m.group(2)
            continue
        if re.fullmatch(r"[0-9A-F]{3}", u):
            mf_total = int(u, 16)
            continue
        if re.fullmatch(r"[0-9A-F]+", u) and len(u) % 2 == 0:
            payloads.append(u)
    if mf_parts:
        joined = "".join(mf_parts[k] for k in sorted(mf_parts))
        if mf_total is not None:
            joined = joined[: mf_total * 2]
        payloads.append(joined)

    results = {}
    responders = 0
    for payload in payloads:
        try:
            data = bytes.fromhex(payload)
        except ValueError:
            continue
        if len(data) < 2 or data[0] != 0x41:
            continue
        responders += 1
        i = 1
        while i < len(data):
            pid = data[i]
            n = PID_LEN.get(pid)
            if n is None or i + 1 + n > len(data):
                break  # unknown PID length or truncated — stop walking safely
            results.setdefault(pid, data[i + 1: i + 1 + n])
            i += 1 + n
    if not results:
        raise ElmError(f"no parseable Mode 01 payload in {lines!r}")
    return results, responders


def query_pids(elm, pids, timeout=None):
    """Request one or more Mode 01 PIDs in a single message."""
    req = "01" + "".join(f"{p:02X}" for p in pids)
    lines = elm.cmd(req, timeout=timeout)
    return parse_mode01(lines)


def get_supported_pids(elm):
    """Walk the supported-PID bitmaps (0100, 0120, ...) and return a set."""
    supported = set()
    base = 0x00
    while base <= 0xC0:
        try:
            res, _ = query_pids(elm, [base])
        except ElmError:
            break
        bits = res.get(base)
        if not bits or len(bits) != 4:
            break
        val = int.from_bytes(bits, "big")
        for i in range(32):
            if val & (1 << (31 - i)):
                supported.add(base + i + 1)
        if base + 0x20 not in supported:
            break
        base += 0x20
    return supported


def rate_test(elm, pids, seconds):
    """Poll a fixed PID request repeatedly; return (hz, samples, last_values)."""
    end = time.perf_counter() + seconds
    count = 0
    last = {}
    while time.perf_counter() < end:
        res, _ = query_pids(elm, pids, timeout=3.0)
        count += 1
        last.update(res)
    hz = count / seconds
    return hz, count, last


def fmt_values(byte_map):
    out = []
    for pid, data in sorted(byte_map.items()):
        if pid in DASH_PIDS:
            name, unit, fn = DASH_PIDS[pid]
            try:
                out.append(f"{name} = {fn(data):.1f} {unit}")
            except Exception:
                out.append(f"{name} = <decode error on {data.hex()}>")
    return out


def run_probe(elm, report, args):
    # --- reset + identify ----------------------------------------------------
    ident = " / ".join(elm.cmd("ATZ", timeout=12.0)) or "(no reply)"
    print(f"Adapter reset:     {ident}")
    report["adapter"] = ident

    for setup in ("ATE0", "ATL0", "ATS0", "ATH0", "ATAT1", "ATSP0"):
        elm.cmd(setup)

    sti = elm.cmd("STI")
    stn = bool(sti) and not any(l.strip() == "?" for l in sti)
    report["stn_firmware"] = " / ".join(sti) if stn else None
    print(f"STN firmware:      {report['stn_firmware'] or 'no (generic ELM327 command set)'}")

    volts = " / ".join(elm.cmd("ATRV")) or "?"
    report["battery"] = volts
    print(f"Battery voltage:   {volts}")

    # --- first contact with the car (triggers protocol auto-detect) ----------
    print("\nContacting vehicle (0100 supported-PID request)...")
    try:
        _first, responders = query_pids(elm, [0x00], timeout=15.0)
    except ElmError as e:
        print(f"  FAILED: {e}")
        print("  Is the ignition on / engine running? Is the adapter plugged in?")
        report["vehicle_contact"] = f"failed: {e}"
        return
    report["vehicle_contact"] = "ok"
    report["responders"] = responders

    dpn = "".join(elm.cmd("ATDPN")).lstrip("A")  # 'A6' -> auto-detected 6
    proto = PROTOCOL_NAMES.get(dpn, f"unknown (ATDPN={dpn!r})")
    is_can = dpn in ("6", "7", "8", "9")
    report["protocol"] = proto
    report["is_can"] = is_can
    print(f"  Protocol:        {proto}")
    print(f"  ECUs responding: {responders}")

    # --- supported PID inventory ---------------------------------------------
    supported = get_supported_pids(elm)
    report["supported_pids"] = sorted(f"{p:02X}" for p in supported)
    print(f"\nECU advertises {len(supported)} Mode 01 PIDs. Dashboard-relevant ones:")
    available_dash = []
    for pid, (name, _unit, _fn) in sorted(DASH_PIDS.items()):
        ok = pid in supported
        if ok:
            available_dash.append(pid)
        print(f"  [{'x' if ok else ' '}] {pid:02X}  {name}")
    report["dash_pids_available"] = [f"{p:02X}" for p in available_dash]

    # --- live values sanity check --------------------------------------------
    core = [p for p in BATCH_PRIORITY if p in available_dash]
    if core:
        try:
            live = {}
            for p in core[:4]:
                r, _ = query_pids(elm, [p])
                live.update(r)
            print("\nLive values (sanity check):")
            for line in fmt_values(live):
                print(f"  {line}")
            report["live_values"] = fmt_values(live)
        except ElmError as e:
            print(f"\nLive value read failed: {e}")

    # --- rate tests ------------------------------------------------------------
    print(f"\nRate tests ({args.seconds:.0f}s each):")
    rates = {}
    best_updates = 0.0  # channel-updates per second, the number that matters

    hz, n, _ = rate_test(elm, [0x0C], args.seconds)
    rates["single_pid_hz"] = round(hz, 1)
    best_updates = max(best_updates, hz)
    print(f"  1 PID  (RPM only)         : {hz:5.1f} requests/s  ({n} samples)")

    if is_can:
        batches = []
        if len(core) >= 3:
            batches.append(core[:3])
        if len(core) >= 5:
            batches.append(core[:6])
        for batch in batches:
            label = f"{len(batch)} PIDs (one CAN message)"
            try:
                hz, n, last = rate_test(elm, batch, args.seconds)
                got = len([p for p in batch if p in last])
                rates[f"batch_{len(batch)}_hz"] = round(hz, 1)
                rates[f"batch_{len(batch)}_answered"] = got
                best_updates = max(best_updates, hz * got)
                print(f"  {label:26s}: {hz:5.1f} requests/s = "
                      f"{hz * got:5.0f} channel-updates/s  ({got}/{len(batch)} PIDs answered)")
            except ElmError as e:
                rates[f"batch_{len(batch)}_hz"] = f"unsupported: {e}"
                print(f"  {label:26s}: not answered here ({e})")
    else:
        print("  (batched multi-PID requests skipped — CAN only, and this car is not on CAN)")
    report["rates"] = rates
    report["best_channel_updates_per_s"] = round(best_updates, 1)

    # --- verdict ----------------------------------------------------------------
    print("\n=== Verdict for the SimHub bridge ===")
    if best_updates >= 40:
        print(f"  ~{best_updates:.0f} channel-updates/s: plenty for smooth gauges "
              f"(10+ Hz each on 4-6 channels).")
    elif best_updates >= 15:
        print(f"  ~{best_updates:.0f} channel-updates/s: workable. Prioritize RPM/speed "
              f"in the poll rotation; slow channels (temps, fuel) every few seconds.")
    else:
        print(f"  ~{best_updates:.0f} channel-updates/s: gauges will update visibly "
              f"stepwise. A tiered poll schedule is essential (RPM first).")
        if not is_can:
            print("  (Pre-CAN protocol is the limiter — this is expected on older cars.)")
    print("  Send this report back (use --json) and phase 2, the SimHub feed,")
    print("  gets built around what your car actually delivers.")


def main():
    ap = argparse.ArgumentParser(description="OBD2 adapter/vehicle probe (phase 1 of OBD2->SimHub)")
    ap.add_argument("--port", help="COM port (COM5), device (/dev/rfcomm0), or socket://host:port")
    ap.add_argument("--baud", type=int, default=115200, help="baud rate (ignored by Bluetooth SPP)")
    ap.add_argument("--seconds", type=float, default=5.0, help="duration of each rate test")
    ap.add_argument("--json", metavar="FILE", help="also write machine-readable results")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--debug", action="store_true", help="dump raw traffic at the end")
    args = ap.parse_args()

    if args.list_ports:
        ports = list(list_ports.comports())
        if not ports:
            print("No serial ports found.")
        for p in ports:
            print(f"{p.device:12s} {p.description}")
        print("\nBluetooth pairing creates TWO ports; use the OUTGOING one")
        print("(Windows: Bluetooth settings -> More Bluetooth options -> COM Ports).")
        return 0

    if not args.port:
        ap.error("--port is required (or use --list-ports)")

    report = {"port": args.port, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"=== OBD probe on {args.port} ===\n")

    try:
        elm = Elm(args.port, baud=args.baud)
    except Exception as e:
        print(f"Could not open {args.port}: {e}")
        print("Check --list-ports, and make sure nothing else (OBDLink app, "
              "another dashboard tool) is holding the port.")
        return 1

    try:
        run_probe(elm, report, args)
    finally:
        if args.debug:
            print("\n--- raw traffic log ---")
            for line in elm.log:
                print(line)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            print(f"\nJSON report written to {args.json}")
        elm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
