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
  3. histogram the survivors in BIN_PCT log-spaced bins; a gear candidate
     is a density bump that rises PROM_RATIO above the saddle toward any
     taller bump (a gear is a spike; a taller gear's shoulder is not), and
     its extent is where density has fallen EDGE_DROP below its own peak;
  4. a candidate must then prove it was DRIVEN: at least MIN_CLUSTER
     samples and MIN_CLUSTER_FRAC of the steady evidence, one continuous
     hold of HOLD_S somewhere in the log (slip and rev-match transients
     photograph well but are never held), and a median rpm above
     IDLE_RPM_CEIL (creeping and coasting happen at idle; driving doesn't);
  5. each survivor's median is one gear's rpm-per-km/h, highest = 1st.

  Step 3 replaced sorted-neighbour gap splitting (GAP_PCT, through
  2026-08-07): on a dense log the debris between gears fills every gap, no
  neighbour pair ever jumps, and all of it chains into one cluster — the
  richer the log, the wronger the answer. Density bumps can't chain.

Tire note: OBD speed is computed by the ECU from wheel rotation against a
fixed assumed circumference, so these constants do NOT change with a tire
swap — only true road speed does (that's speed_factor in calibration.json).

Stdlib only; no pyserial, no hardware. Safety: reads a CSV, writes only the
file named by --write.
"""

import argparse
import csv
import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir))
from obd_config import parse_with_config

MIN_RPM = 900.0        # below this the engine is idling or off
MIN_SPEED = 8.0        # km/h; below this speed quantization wrecks the ratio
STEADY_PCT = 3.0       # consecutive-sample ratio agreement = "holding a gear"
MIN_CLUSTER = 8        # bumps smaller than this are debris whatever else they prove
BIN_PCT = 1.0          # histogram bin width; gears sit >=10% apart, so bins
                       # must slice the valley between two, never straddle it
MIN_PEAK = 2.0         # smoothed-count floor for even looking at a bump
PROM_RATIO = 4.0       # a bump is a candidate only if it rises this far above
                       # the saddle toward any taller bump — kills the
                       # shoulders of real gears without a magic width
EDGE_DROP = 4.0        # a bump ends where density falls this far below its
                       # peak; the tails beyond are neighbouring debris
MIN_CLUSTER_FRAC = 0.005  # dense logs grow debris lumps past MIN_CLUSTER; a
                          # gear that was really driven owns at least this
                          # share of the steady evidence
HOLD_S = 0.6           # a gear is HELD: no continuous hold this long means
                       # slip/rev-match transients, however tight the ratio
RUN_GAP_S = 0.6        # a hold breaks when steady samples sit further apart
IDLE_RPM_CEIL = 1400.0 # a "gear" whose median rpm is idle-band is the car
                       # creeping or coasting in neutral; a real 6th-gear
                       # cruise sits ~1500 in the logs, so stay under it
SANE_SPREAD_PCT = 10.0 # no real gear is this wide; wider = smeared debris


def load_samples(path):
    """Return [(t_s, rpm, speed_kmh)] from a --log CSV, tolerating extra columns."""
    out = []
    try:
        # Strict utf-8, same as learn_throttle's reader and for the same
        # reason: under a Windows locale default (cp1252) an Excel "Unicode
        # Text" save decodes into NUL-garbage instead of raising, and the
        # named refusal below never gets its turn. The probe and feed write
        # ascii/utf-8 only. -sig, because the refusal's remedy is Excel's
        # "CSV UTF-8" and Excel writes that WITH a BOM — plain utf-8 folds
        # it into the first column name and t_s never matches again.
        f = open(path, newline="", encoding="utf-8-sig")
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}")
    with f:
        try:
            rows = list(csv.DictReader(f))
        except UnicodeDecodeError:
            sys.exit(f"{path} is not a text CSV this tool can read — it "
                     f"looks like UTF-16 or binary. If it came out of Excel, "
                     f"re-save it as \"CSV UTF-8\"; obd_probe.py --log "
                     f"writes that format directly.")
        for row in rows:
            try:
                out.append((float(row["t_s"]), float(row["rpm"]), float(row["speed_kmh"])))
            except (KeyError, TypeError, ValueError):
                continue  # header variants, blank lines and short rows are not data
    return out


def steady_ratios(samples):
    """[(t_s, ratio, rpm)] from consecutive-sample pairs that agree within
    STEADY_PCT. Time and rpm ride along: cluster() needs them to tell a held
    gear from a photogenic transient and from idle creep."""
    out = []
    for (t0, r0, v0), (t1, r1, v1) in zip(samples, samples[1:]):
        if min(r0, r1) > MIN_RPM and min(v0, v1) > MIN_SPEED:
            q0, q1 = r0 / v0, r1 / v1
            if abs(q1 - q0) / q0 * 100.0 < STEADY_PCT:
                out.append((t1, q1, r1))
    return out


def _longest_hold(times):
    """Longest continuous stretch of a sorted time list, where a gap over
    RUN_GAP_S breaks the stretch."""
    best = 0.0
    start = prev = times[0]
    for t in times[1:]:
        if t - prev > RUN_GAP_S:
            best = max(best, prev - start)
            start = t
        prev = t
    return max(best, prev - start)


def cluster(steady):
    """Find gears as density bumps in the steady ratios. Returns (gears,
    rejected): both lists of {rpm_per_kmh, samples, spread_pct}, descending;
    rejected entries add a "why" naming the guard that dropped the bump."""
    if not steady:
        return [], []
    # Bins are log-spaced so BIN_PCT means the same thing at 25 and at 103
    # rpm/km/h. Smoothing is a (1,2,1)/4 binomial kernel: a flat 3-bin mean
    # lets debris TWO bins out leak into a spike's neighbours and crater the
    # summit — both rim bins then read as local maxima and the same gear
    # reports twice. A centre-weighted kernel cannot crater a spike.
    width = math.log(1.0 + BIN_PCT / 100.0)
    bin_of = lambda q: math.floor(math.log(q) / width)
    counts = {}
    for _, q, _ in steady:
        b = bin_of(q)
        counts[b] = counts.get(b, 0) + 1
    lo, hi = min(counts), max(counts)
    sm = {b: (counts.get(b - 1, 0) + 2 * counts.get(b, 0) + counts.get(b + 1, 0)) / 4.0
          for b in range(lo - 1, hi + 2)}

    peaks = []
    for peak in sorted(sm):
        h = sm[peak]
        if h < MIN_PEAK or h < sm.get(peak - 1, 0.0) or h <= sm.get(peak + 1, 0.0):
            continue
        # Prominence: on each side, the lowest density crossed before reaching
        # taller ground. A shoulder's saddle is nearly its own height, so it
        # fails the ratio however many samples it holds.
        saddles = []
        for step in (-1, 1):
            lowest, b = h, peak
            while True:
                b += step
                if b not in sm:
                    break
                if sm[b] > h:
                    saddles.append(lowest)
                    break
                lowest = min(lowest, sm[b])
        key_saddle = max(saddles) if saddles else 0.0
        if key_saddle > 0.0 and h / key_saddle < PROM_RATIO:
            continue
        peaks.append(peak)

    floor_n = max(MIN_CLUSTER, int(MIN_CLUSTER_FRAC * len(steady)))
    gears, rejected, claimed = [], [], []
    # Tallest first: two equal-height peaks inside one bump (possible on a
    # plateau, where neither reads as the other's "taller ground") resolve
    # to the same span, and the first to claim it wins.
    for peak in sorted(peaks, key=lambda b: -sm[b]):
        h = sm[peak]
        # The bump's extent: every contiguous bin still EDGE_DROP-close to
        # the peak. Prominence already certified a below-edge saddle before
        # any neighbouring gear, so the walk cannot cross into one.
        edge = h / EDGE_DROP
        left = peak
        while left - 1 in sm and sm[left - 1] > edge:
            left -= 1
        right = peak
        while right + 1 in sm and sm[right + 1] > edge:
            right += 1
        if any(left <= r and cl <= right for cl, r in claimed):
            continue
        claimed.append((left, right))

        members = sorted(x for x in steady if left <= bin_of(x[1]) <= right)
        qs = sorted(q for _, q, _ in members)
        med = statistics.median(qs)
        entry = {"rpm_per_kmh": round(med, 1),
                 "samples": len(members),
                 # spread_pct is a HALF-width (±): (max-min)/2 as a % of the
                 # median, so the printed ±N% is the actual band around the
                 # constant, not double it.
                 "spread_pct": round((qs[-1] - qs[0]) / 2.0 / med * 100.0, 1)}
        why = None
        if len(members) < floor_n:
            why = (f"{len(members)} samples < {floor_n} "
                   f"({MIN_CLUSTER_FRAC:.1%} of steady) — debris accumulates, "
                   f"a driven gear concentrates")
        elif statistics.median(r for _, _, r in members) < IDLE_RPM_CEIL:
            why = (f"median rpm "
                   f"{statistics.median(r for _, _, r in members):.0f} is "
                   f"idle-band — creeping or coasting, not a coupled gear")
        elif _longest_hold([t for t, _, _ in members]) < HOLD_S:
            why = (f"never held: longest continuous run "
                   f"{_longest_hold([t for t, _, _ in members]):.2f}s < "
                   f"{HOLD_S}s — slip/rev-match transients, not a gear")
        elif entry["spread_pct"] > SANE_SPREAD_PCT:
            why = (f"±{entry['spread_pct']}% wide — no gear is; smeared "
                   f"debris (this line is a bug report, please send the log)")
        if why is None:
            gears.append(entry)
        else:
            entry["why"] = why
            rejected.append(entry)
    key = lambda c: -c["rpm_per_kmh"]
    return sorted(gears, key=key), sorted(rejected, key=key)


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


def build_parser():
    ap = argparse.ArgumentParser(description="Learn gear constants from a drive log.")
    ap.add_argument("log", help="CSV from obd_probe.py --log")
    ap.add_argument("--gears", type=int, default=6,
                    help="expected gear count, 0 = no expectation (default 6)")
    # A verb, not a setting: configured, it would rewrite the calibration
    # on every learn run — a write to the one file a drive paid for should
    # always be asked for out loud.
    ap.add_argument("--write", metavar="CALIBRATION_JSON",
                    help="update this calibration file's gears section "
                         "in place").per_run = True
    return ap


def main():
    args = parse_with_config(build_parser(), "learn_gears")

    samples = load_samples(args.log)
    if not samples:
        sys.exit(f"no usable rows in {args.log} — expected columns t_s,rpm,speed_kmh "
                 f"(the header obd_probe.py --log writes)")
    steady = steady_ratios(samples)
    gears, rejected = cluster(steady)

    print(f"{len(samples)} samples, {len(steady)} steady, {len(gears)} gear clusters\n")
    print("  gear   rpm per km/h   samples   spread")
    for i, c in enumerate(gears, 1):
        print(f"  {i:>4}   {c['rpm_per_kmh']:>12.1f}   {c['samples']:>7}   ±{c['spread_pct']:.1f}%")

    if (args.gears and len(gears) != args.gears) or (rejected and not gears):
        sys.stdout.flush()  # keep the table above the FAIL lines when piped
        if args.gears:
            print(f"\nFAIL: expected {args.gears} gears, log yielded {len(gears)}. "
                  f"Not written.", file=sys.stderr)
        else:
            print(f"\nFAIL: no bump in this log survived the driven-gear "
                  f"guards. Not written.", file=sys.stderr)
        if rejected:
            print("Density bumps found and rejected, and why:", file=sys.stderr)
            for c in rejected:
                print(f"  {c['rpm_per_kmh']:>7.1f}  n={c['samples']:<5d} "
                      f"±{c['spread_pct']:.1f}%  {c['why']}", file=sys.stderr)
        if len(gears) < (args.gears or 0):
            print(f"If no rejected line above is the missing gear, this is a "
                  f"partial sweep: a gear never held at driving rpm for "
                  f"{HOLD_S}s doesn't count as driven. The sweep protocol is "
                  f"in the README.", file=sys.stderr)
        return 1

    if args.write:
        write_calibration(args.write, gears, args.log)
        print(f"\ngears section written to {args.write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
