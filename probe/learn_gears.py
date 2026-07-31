#!/usr/bin/env python3
"""
learn_gears.py — recover gearbox constants from a drive log
===========================================================

Phase 2 prep for the OBD2 -> SimHub bridge. Takes a drive log written by
`obd_probe.py --log` (columns: t_s, rpm, speed_kmh, ...), finds the gears as
clusters in the rpm/speed ratio, and prints the constants the live gear
readout will match against. The protocol that produces a good log is in the
README: all gears swept once, up and back down, modest rpm.

Usage:
  python probe/learn_gears.py reports/2026-07-31-kris-drive_01.csv
  python probe/learn_gears.py drive.csv --gears 6 --write calibration.json

  --gears N   expected gear count; exit 1 if the log yields anything else
              (0 = take what the data gives; default 6)
  --write F   update the "gears" section of calibration file F in place;
              everything else in the file is preserved

Method, so the numbers are auditable:
  1. walk consecutive sample pairs; a pair counts only if BOTH samples have
     rpm > MIN_RPM and speed > MIN_SPEED (engine driving, not idling or
     Auto-Stop, denominator not near zero) — pairing before filtering means
     a pair never spans a filtered-out gap;
  2. keep the pair's ratio when the two agree within STEADY_PCT (clutch
     engaged and holding a gear — shifts and coasting drop out);
  3. sort surviving ratios, split into clusters at any >GAP_PCT jump, drop
     clusters with fewer than MIN_CLUSTER samples (transient debris);
  4. each cluster's median is one gear's rpm-per-km/h, highest = 1st.

Tire note: OBD speed is computed by the ECU from wheel rotation against a
fixed assumed circumference, so these constants do NOT change with a tire
swap — only true road speed does (that's speed_factor in calibration.json).

Stdlib only; no pyserial, no hardware. Safety: reads a CSV, writes only the
file named by --write.
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time

MIN_RPM = 900.0        # below this the engine is idling or off
MIN_SPEED = 8.0        # km/h; below this speed quantization wrecks the ratio
STEADY_PCT = 3.0       # consecutive-sample ratio agreement = "holding a gear"
GAP_PCT = 8.0          # a jump this big between sorted ratios splits clusters
MIN_CLUSTER = 8        # clusters smaller than this are shift/coast debris


def load_samples(path):
    """Return [(t_s, rpm, speed_kmh)] from a --log CSV, tolerating extra columns."""
    out = []
    try:
        f = open(path, newline="")
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}")
    with f:
        for row in csv.DictReader(f):
            try:
                out.append((float(row["t_s"]), float(row["rpm"]), float(row["speed_kmh"])))
            except (KeyError, ValueError):
                continue  # header variants and blank lines are not data
    return out


def steady_ratios(samples):
    """Ratios from consecutive-sample pairs that agree within STEADY_PCT."""
    out = []
    for (t0, r0, v0), (t1, r1, v1) in zip(samples, samples[1:]):
        if min(r0, r1) > MIN_RPM and min(v0, v1) > MIN_SPEED:
            q0, q1 = r0 / v0, r1 / v1
            if abs(q1 - q0) / q0 * 100.0 < STEADY_PCT:
                out.append(q1)
    return out


def cluster(ratios):
    """Split sorted ratios at >GAP_PCT jumps; return medians of the survivors,
    descending (1st gear first)."""
    if not ratios:
        return []
    srt = sorted(ratios)
    groups, cur = [], [srt[0]]
    for q in srt[1:]:
        if q / cur[-1] > 1.0 + GAP_PCT / 100.0:
            groups.append(cur)
            cur = [q]
        else:
            cur.append(q)
    groups.append(cur)
    keep = [g for g in groups if len(g) >= MIN_CLUSTER]
    # spread_pct is a HALF-width (±): (max-min)/2 as a % of the median, so
    # the printed ±N% is the actual band around the constant, not double it.
    return sorted(
        ({"rpm_per_kmh": round(statistics.median(g), 1),
          "samples": len(g),
          "spread_pct": round((max(g) - min(g)) / 2.0 / statistics.median(g) * 100.0, 1)}
         for g in keep),
        key=lambda c: -c["rpm_per_kmh"],
    )


def write_calibration(path, gears, source):
    """Replace the gears constants in calibration file `path`, preserving the
    rest of the file; create a minimal one if it doesn't exist."""
    try:
        with open(path) as f:
            cal = json.load(f)
    except FileNotFoundError:
        cal = {}
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON ({e}); not touching it")
    if os.path.isabs(source):
        source = os.path.relpath(source)  # provenance, not a machine path
    section = cal.setdefault("gears", {})
    section["rpm_per_kmh"] = [c["rpm_per_kmh"] for c in gears]
    section.setdefault("tolerance_pct", 7)
    section["learned_from"] = source
    section["learned_on"] = time.strftime("%Y-%m-%d")
    with open(path, "w") as f:
        json.dump(cal, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="Learn gear constants from a drive log.")
    ap.add_argument("log", help="CSV from obd_probe.py --log")
    ap.add_argument("--gears", type=int, default=6,
                    help="expected gear count, 0 = no expectation (default 6)")
    ap.add_argument("--write", metavar="CALIBRATION_JSON",
                    help="update this calibration file's gears section in place")
    args = ap.parse_args()

    samples = load_samples(args.log)
    if not samples:
        sys.exit(f"no usable rows in {args.log} — expected columns t_s,rpm,speed_kmh "
                 f"(the header obd_probe.py --log writes)")
    ratios = steady_ratios(samples)
    gears = cluster(ratios)

    print(f"{len(samples)} samples, {len(ratios)} steady, {len(gears)} gear clusters\n")
    print("  gear   rpm per km/h   samples   spread")
    for i, c in enumerate(gears, 1):
        print(f"  {i:>4}   {c['rpm_per_kmh']:>12.1f}   {c['samples']:>7}   ±{c['spread_pct']:.1f}%")

    if args.gears and len(gears) != args.gears:
        sys.stdout.flush()  # keep the table above the FAIL line when piped
        print(f"\nFAIL: expected {args.gears} gears, log yielded {len(gears)}. "
              f"Not written. A partial sweep, a slipping clutch, or thresholds "
              f"that need a look — see the method block in this file's docstring.",
              file=sys.stderr)
        return 1

    if args.write:
        write_calibration(args.write, gears, args.log)
        print(f"\ngears section written to {args.write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
