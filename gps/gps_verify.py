#!/usr/bin/env python3
"""
gps_verify.py — capture health report: what the file says, and whether to trust it
===================================================================================

The capture tool wrote down sixty seconds of dumb bytes so a parser could
be built on truth instead of guesses. This tool is the other half of that
bargain: point it at any capture and it reports what is actually in there
— sentence census with real rates, checksum coverage, binary damage, fix
validity, speed, footprint — and, if you tell it what the capture was
supposed to be, a verdict. A parser that reports motion in a driveway
capture has failed its own control; this is where that control lives.

  py gps\\gps_verify.py xgps160-capture.txt
  py gps\\gps_verify.py xgps160-capture.txt --expect stationary
  py gps\\gps_verify.py runs\\gps-last.txt --expect drive

--expect stationary passes when the parsed track never leaves GPS-wander
range of its first fix and never claims real speed. --expect drive passes
when it does both. Exit code says which, so a capture session can check
itself before anyone drives home disappointed.

One deliberate manner: the report prints sizes, rates and counts, never a
coordinate. A capture usually starts at someone's house; its health report
should be safe to paste into an email without mailing anyone your curb.

Reads the two-column format gps_capture.py writes (seconds, tab, sentence),
which is also what gps_overlay.py --run-log records. Stdlib only.
"""

import argparse
import collections
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nmea import enu_meters, nmea_checksum_ok, parse_gga, parse_rmc

# GPS-wander scale: a stationary receiver may scribble this far and still
# be honest. Anything past it is the parser (or the car) actually moving.
STATIONARY_MAX_M = 15.0
STATIONARY_MAX_KMH = 2.0
# A drive that never gets this far or this fast was a parking maneuver.
DRIVE_MIN_M = 100.0
DRIVE_MIN_KMH = 10.0

# Binary damage in either era's spelling: today's \xNN escapes, or the
# U+FFFD a pre-fix framer left in older captures.
_DAMAGE = re.compile(r"\\x[0-9a-f]{2}|\ufffd")


def parse_capture_lines(lines):
    """(t, sentence) rows from capture text lines; count of rejects.

    A row is seconds, one tab, the sentence. Anything else — blank line,
    no tab, unparseable timestamp — is counted, not silently dropped.
    """
    rows = []
    malformed = 0
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        if "\t" not in line:
            malformed += 1
            continue
        ts, sentence = line.split("\t", 1)
        try:
            rows.append((float(ts), sentence))
        except ValueError:
            malformed += 1
    return rows, malformed


def analyze(rows):
    """Everything the report needs, as one dict. Pure; no I/O.

    Positions are kept internally to size the footprint but never leave
    this function except as distances — see the module manner.
    """
    types = collections.Counter()
    cs_present = cs_failed = damaged = 0
    rmc_parsed = rmc_valid = gga_parsed = 0
    speeds = []
    pts = []
    pwr_first = pwr_last = None

    for _, sentence in rows:
        if _DAMAGE.search(sentence):
            damaged += 1
        di = sentence.find("$")
        body = sentence[di:] if di >= 0 else ""
        if body:
            types[body[1:].split(",")[0].split("*")[0]] += 1
            if "*" in body:
                cs_present += 1
                if not nmea_checksum_ok(body):
                    cs_failed += 1
            if body.startswith("$GPPWR"):
                fields = body.split(",")
                if pwr_first is None:
                    pwr_first = fields
                pwr_last = fields
        fix = parse_rmc(sentence)
        if fix:
            rmc_parsed += 1
            if fix["valid"]:
                rmc_valid += 1
                speeds.append(fix["speed_kmh"])
                pts.append((fix["lat"], fix["lon"]))
            continue
        if parse_gga(sentence):
            gga_parsed += 1

    ew = ns = dmax = 0.0
    if pts:
        lat0, lon0 = pts[0]
        es, ns_ = [], []
        for lat, lon in pts:
            e, n = enu_meters(lat0, lon0, lat, lon)
            es.append(e)
            ns_.append(n)
            dmax = max(dmax, math.hypot(e, n))
        ew = max(es) - min(es)
        ns = max(ns_) - min(ns_)

    span = rows[-1][0] - rows[0][0] if len(rows) > 1 else 0.0
    return {
        "rows": len(rows),
        "span_s": span,
        "types": types,
        "cs_present": cs_present,
        "cs_failed": cs_failed,
        "damaged": damaged,
        "rmc_parsed": rmc_parsed,
        "rmc_valid": rmc_valid,
        "gga_parsed": gga_parsed,
        "unique_positions": len(set(pts)),
        "speed_max": max(speeds) if speeds else 0.0,
        "speed_mean": sum(speeds) / len(speeds) if speeds else 0.0,
        "ew_m": ew,
        "ns_m": ns,
        "dmax_m": dmax,
        "pwr_first": pwr_first,
        "pwr_last": pwr_last,
    }


def report(stats, name, malformed=0):
    """The health report, as text. Sizes and rates; never a coordinate."""
    lines = [f"capture: {name}"]
    span = stats["span_s"]
    lines.append(f"  {stats['rows']} rows over {span:.1f} s"
                 + (f"  ({malformed} malformed rows skipped)"
                    if malformed else ""))
    lines.append("  sentences (rate over the whole span):")
    for t, count in stats["types"].most_common():
        rate = count / span if span > 0 else 0.0
        lines.append(f"    {t:6s} {count:6d}  ({rate:.1f} Hz)")
    lines.append(f"  checksums: {stats['cs_present']} carried, "
                 f"{stats['cs_failed']} failed")
    lines.append(f"  binary damage: {stats['damaged']} line(s) carry "
                 f"escaped or replaced bytes")
    rmc_seen = sum(c for t, c in stats["types"].items() if t.endswith("RMC"))
    lines.append(f"  fixes: {stats['rmc_parsed']} of {rmc_seen} RMC parsed, "
                 f"{stats['rmc_valid']} valid, "
                 f"{stats['unique_positions']} unique positions")
    lines.append(f"  speed: max {stats['speed_max']:.1f} km/h, "
                 f"mean {stats['speed_mean']:.1f}")
    lines.append(f"  footprint: {stats['ew_m']:.1f} m EW x "
                 f"{stats['ns_m']:.1f} m NS, "
                 f"max {stats['dmax_m']:.1f} m from first fix")
    if stats["pwr_first"]:
        f0, fl = stats["pwr_first"], stats["pwr_last"]
        v = (f"{f0[1]} -> {fl[1]}" if len(f0) > 1 and len(fl) > 1 else "?")
        lines.append(f"  $GPPWR: {stats['types'].get('GPPWR', 0)} seen, "
                     f"field1 {v} (voltage-shaped: it declines as the "
                     f"battery does)")
    return "\n".join(lines)


def verdict(stats, expect):
    """(passed, reason) for an --expect claim against the analyzed track."""
    dmax, vmax = stats["dmax_m"], stats["speed_max"]
    if stats["rmc_valid"] == 0:
        return False, "no valid fixes at all"
    if expect == "stationary":
        if dmax > STATIONARY_MAX_M:
            return False, (f"track wanders {dmax:.1f} m from first fix "
                           f"(limit {STATIONARY_MAX_M:.0f} m)")
        if vmax > STATIONARY_MAX_KMH:
            return False, (f"claims {vmax:.1f} km/h while parked "
                           f"(limit {STATIONARY_MAX_KMH:.0f})")
        return True, (f"max {dmax:.1f} m from first fix, "
                      f"max {vmax:.1f} km/h")
    if dmax < DRIVE_MIN_M:
        return False, (f"track never leaves {dmax:.1f} m "
                       f"(a drive covers {DRIVE_MIN_M:.0f}+)")
    if vmax < DRIVE_MIN_KMH:
        return False, (f"never exceeds {vmax:.1f} km/h "
                       f"(a drive exceeds {DRIVE_MIN_KMH:.0f})")
    return True, f"{dmax:.0f} m from start, max {vmax:.0f} km/h"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="GPS capture health report (never prints a coordinate).")
    ap.add_argument("capture", help="capture file (seconds TAB sentence)")
    ap.add_argument("--expect", choices=["stationary", "drive"],
                    help="what the capture claims to be; sets the exit code")
    args = ap.parse_args(argv)

    try:
        with open(args.capture, encoding="utf-8", errors="replace") as f:
            rows, malformed = parse_capture_lines(f)
    except OSError as e:
        sys.exit(f"could not read {args.capture}: {e}")
    if not rows:
        sys.exit(f"{args.capture}: no capture rows at all — "
                 f"nothing to verify, and that IS the verdict")

    stats = analyze(rows)
    print(report(stats, os.path.basename(args.capture), malformed))
    if args.expect:
        passed, reason = verdict(stats, args.expect)
        print(f"verdict: {args.expect.upper()} "
              f"{'PASS' if passed else 'FAIL'} — {reason}")
        return 0 if passed else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
