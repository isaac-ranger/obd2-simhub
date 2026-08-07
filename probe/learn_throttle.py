#!/usr/bin/env python3
"""
learn_throttle.py — recover throttle floor and ceiling from a drive log
======================================================================

The throttle PID (0x11) does not read 0-100 on a real car. On the 982 it
rests around 12-17% with your foot completely off the pedal, and it stops
dead at 88.6% under wide-open throttle — the plate never opens further, at
any rpm, in any drive mode. Sent straight through, an overlay shows
moderate throttle while you coast and never once reaches 100% on a pull.

This takes a log written by `obd_probe.py --log` (or `obd_feed.py`'s run
logs) and prints the two numbers that fix it, plus every sample count
behind them, so you can see whether your drive actually earned the answer.

Usage:
  python probe/learn_throttle.py logs/my-drive.csv
  python probe/learn_throttle.py logs/my-drive.csv --write calibration.json

  --write F           update the "throttle" section of calibration file F
                      in place; everything else in the file is preserved
  --floor-regime R    coast | idle | auto (default auto — see below)
  --ceiling-margin-pct N   how far below the measured wall to sit (default 4)
  --min-samples N     refuse to recommend a number backed by fewer than
                      this many samples (default 30)

WHAT DRIVE PRODUCES A GOOD LOG

Anything with both ends in it: idle in the garage, one honest wide-open
pull, and some steady cruising with your foot fully off at road speed.
Ten minutes is plenty. The ceiling needs the pull; the floor needs the
coasting. This tool tells you which one your log is short of instead of
quietly averaging its way to a number.

METHOD, so the numbers are auditable

  ceiling:
    1. count how often each distinct throttle value appears. The PID is a
       single byte scaled k*100/255, so real readings land on a grid and
       the plate PARKS on its top byte under WOT — a value that appears
       many times is a wall, a value that appears once is a glitch;
    2. the wall = the highest value seen at least MIN_WALL times;
    3. ceiling = wall backed off CEILING_MARGIN_PCT, rounded DOWN.

    The back-off is the whole point. A ceiling set exactly AT the wall
    means any run that stops just short of it — a spirited drive that
    never quite hits the stop — never reads 100% either, which is the
    bug being fixed, just rarer and harder to notice. Margin below the
    wall makes everything at or above it read a flat 100.

  floor:
    4. coast regime: samples with load_pct <= COAST_LOAD_PCT and
       speed_kmh > COAST_MIN_SPEED — moving with the engine unloaded.
       Median of those;
    5. idle regime: samples with speed_kmh == 0 and rpm >= IDLE_MIN_RPM.
       Median of those;
    6. recommended floor = the coast median when the log has enough coast
       samples, else the idle median with a warning.

    Coast, not idle, because the two disagree and coast is the higher of
    them: zero the idle reading only and a freeway coast still shows a few
    percent of phantom throttle. Zero the coast reading and both regimes
    rest at zero.

  Every selector above keys on load_pct, speed_kmh and rpm ONLY. None of
  them looks at throttle_pct. Selecting low-throttle samples and then
  reporting that throttle was low is a circle that produces a confident
  wrong number; this file states its selectors so you can check that it
  isn't drawing one.

THE ONE THING THIS MODEL CANNOT REPRESENT

There is no single closed-throttle zero. Within the coast regime the plate
opens further as engine speed rises — on the reference car the median coast
reading climbs from ~16% near 1,500 rpm to ~23% near 7,000. A two-point map
has one floor, so it fixes whichever part of that range the median lands in
and leaves a small residual at the ends. That is a real limit of the shape
Kris asked for, not a bug in the fit, and the per-rpm table printed below
the recommendation shows you exactly how much of it your car has. If the
spread is large the tool says so out loud rather than burying it.

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

MIN_WALL = 5            # a throttle value seen this often is a wall, not a blip
CEILING_MARGIN_PCT = 4.0  # how far below the wall the ceiling sits, by default
COAST_LOAD_PCT = 5.0    # engine load at or below this = not being asked for
COAST_MIN_SPEED = 50.0  # km/h; slower than this and "coasting" blurs into town
IDLE_MIN_RPM = 400.0    # below this the engine is off or in Auto-Stop
RPM_BIN = 1000.0        # width of the coast floor's per-rpm bins, in rpm
SPREAD_WARN_PCT = 5.0   # coast floor varying more than this across rpm = say so
FLAT_WARN_PCT = 5.0     # this much of a log pinned at 100% = the wall is a cruise
LOW_WALL_PCT = 70.0     # a pedal's mechanical stop sits near the top of its range
MIN_SUSTAINED = 30      # samples on the wall below this = a brief hold, not a stop

# Every key the feed's throttle section is allowed to carry. Must stay
# identical to THROTTLE_KEYS in extractor/obd_feed.py — the feed refuses to
# start on anything outside this set, so a --write that emitted a key the feed
# rejects would hand the user a file its own tool told them would fix things.
# probe/test_learn_throttle.py asserts the two sets are equal.
THROTTLE_KEYS = {"floor_pct", "ceiling_pct", "measured_wall_pct",
                 "learned_from", "learned_on", "_notes"}


def load_samples(path):
    """Return [(rpm, speed_kmh, throttle_pct, load_pct_or_None)] from a log CSV.

    load_pct is optional per ROW, not just per file: obd_probe polls it on the
    slow loop, so early rows and any dropped read arrive blank. Those rows are
    still usable for the ceiling and the idle floor, so they are kept with
    load_pct None rather than discarded.
    """
    out = []
    try:
        # encoding= is load-bearing, not hygiene. Without it the default is
        # the locale codepage, and cp1252 — the codepage of the platform this
        # tool was written for — decodes UTF-16's ASCII-range bytes without a
        # murmur, into NUL-riddled garbage whose column names match nothing:
        # the refusal below could only ever fire on machines that would never
        # produce the file. Everything the probe and feed write is
        # ascii/utf-8, so the strict read costs nothing real. -sig, because
        # the refusal's remedy is Excel's "CSV UTF-8" and Excel writes that
        # WITH a BOM — plain utf-8 would fold it into the first column name
        # and refuse the very file it asked for.
        f = open(path, newline="", encoding="utf-8-sig")
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}")
    with f:
        # The decode happens lazily, row by row, so a non-UTF-8 file gets past
        # open() and dies mid-iteration. Excel's "Unicode Text" save writes
        # UTF-16 and this tool's audience is Windows-first, so that is a real
        # path to a traceback rather than an exotic one. Refuse by name.
        try:
            rows = list(csv.DictReader(f))
        except UnicodeDecodeError:
            sys.exit(f"{path} is not a text CSV this tool can read — it "
                     f"looks like UTF-16 or binary. If it came out of Excel, "
                     f"re-save it as \"CSV UTF-8\"; obd_probe.py --log writes "
                     f"that format directly.")
        for row in rows:
            try:
                rpm = float(row["rpm"])
                speed = float(row["speed_kmh"])
                thr = float(row["throttle_pct"])
            except (KeyError, TypeError, ValueError):
                continue  # header variants, blank lines and gaps are not data
            if not (math.isfinite(rpm) and math.isfinite(speed)
                    and math.isfinite(thr)):
                continue  # float() accepts "nan" and "1e400"; a corrupt or
                # hand-made row must not become the wall — inf sorts above
                # every real reading and then cannot be turned into a byte.
            try:
                load = float(row["load_pct"])
            except (KeyError, TypeError, ValueError):
                load = None
            else:
                if not math.isfinite(load):
                    load = None
            out.append((rpm, speed, thr, load))
    return out


def find_wall(throttles, min_wall=MIN_WALL):
    """Highest throttle value that appears at least min_wall times, with its
    count. (None, 0) if nothing repeats that often — a log with no sustained
    high-throttle event has no wall to find."""
    counts = {}
    for t in throttles:
        counts[t] = counts.get(t, 0) + 1
    walls = [(v, n) for v, n in counts.items() if n >= min_wall]
    if not walls:
        return None, 0
    top = max(walls, key=lambda vn: vn[0])
    return top[0], top[1]


def ceiling_from_wall(wall, margin_pct=CEILING_MARGIN_PCT):
    """Back off from the wall and round DOWN — margin below the wall is the
    safe direction, so the rounding goes the same way as the intent."""
    return math.floor(wall * (1.0 - margin_pct / 100.0) * 10.0) / 10.0


def coast_samples(samples):
    """(throttle, rpm) pairs with the engine unloaded and the car moving.
    Selector: load_pct and speed_kmh. Never throttle_pct."""
    return [(t, r) for r, v, t, ld in samples
            if ld is not None and ld <= COAST_LOAD_PCT and v > COAST_MIN_SPEED]


def idle_samples(samples):
    """Throttle readings with the car stopped and the engine running.
    Selector: speed_kmh and rpm. Never throttle_pct."""
    return [t for r, v, t, _ld in samples if v == 0.0 and r >= IDLE_MIN_RPM]


def nearest_rank(values, pct):
    """Nearest-rank percentile — no interpolation, so every number printed is
    a reading the car actually produced."""
    if not values:
        return None
    srt = sorted(values)
    k = max(1, math.ceil(pct / 100.0 * len(srt)))
    return srt[min(k, len(srt)) - 1]


def rpm_bins(coast, width=RPM_BIN):
    """[(bin_low, median_throttle, n)] for the coast samples, ascending — the
    picture of how much the floor moves with engine speed."""
    buckets = {}
    for t, r in coast:
        buckets.setdefault(int(r // width) * int(width), []).append(t)
    return [(lo, statistics.median(ts), len(ts))
            for lo, ts in sorted(buckets.items())]


def residual(coast, floor, ceiling):
    """What the coast samples map to under a recommendation: (median, p90, n
    at exactly zero). The honest read on 'does this actually zero my coast'."""
    span = ceiling - floor
    mapped = [max(0.0, min(100.0, (t - floor) / span * 100.0)) for t, _r in coast]
    return (statistics.median(mapped), nearest_rank(mapped, 90),
            sum(1 for m in mapped if m == 0.0))


def write_calibration(path, floor, ceiling, source, wall):
    """Replace the throttle constants in calibration file `path`, preserving
    the rest of the file; create a minimal one if it doesn't exist."""
    try:
        # Explicit UTF-8 both directions. The feed reads this file as strict
        # UTF-8; on Windows the default here would be the locale codepage, so
        # a pasted curly quote round-trips to mojibake or dies uncaught.
        with open(path, encoding="utf-8") as f:
            cal = json.load(f)
    except FileNotFoundError:
        cal = {}  # a fresh install has no calibration yet; make it one
    except OSError as e:
        # A directory, a permission wall, a bad path. Name it — the read side
        # of this tool already does, and this is the side that can lose work.
        sys.exit(f"cannot read {path}: {e}")
    except UnicodeDecodeError as e:
        # Not UTF-8 at all — a file saved in a Windows codepage, or UTF-16.
        # Named, because the traceback for this one says nothing useful.
        sys.exit(f"{path} is not UTF-8 text ({e}); not touching it. Save it "
                 f"as UTF-8 and re-run, or put the throttle section in by hand.")
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON ({e}); not touching it")
    if os.path.isabs(source):
        try:
            rel = os.path.relpath(source)  # provenance, not a machine path
        except ValueError:
            rel = source  # Windows: log on E:, repo on C:. There is no
            # relative path between two drives, and this string is only ever
            # read by a human — the absolute one says the same thing.
        # ...and a relpath that climbs out of the repo says it worse than the
        # absolute one did. Shorter is only better while it stays inside.
        source = source if rel.startswith(os.pardir) else rel
    if os.path.exists(path) and not os.access(path, os.W_OK):
        # The atomic write below renames a new file over this one, which the
        # filesystem will happily allow on a read-only target — the mode is on
        # the file, the permission that matters is on the directory. Somebody
        # who marked this file read-only meant it, so refuse here instead of
        # quietly honouring the letter of the permission bits and not the point.
        sys.exit(f"{path} is read-only; not replacing it. The recommendation "
                 f"above is still good — make the file writable, or put the "
                 f"throttle section in by hand.")
    if not isinstance(cal, dict):
        # Valid JSON, wrong shape — a bare list or string parses fine and then
        # has no place to put a section. Refuse by name like every other bad
        # input here, rather than an AttributeError one line down.
        sys.exit(f"{path} holds a JSON {type(cal).__name__}, not an object; "
                 f"a calibration file is a {{...}} of sections. The "
                 f"recommendation above is still good — put the throttle "
                 f"section in by hand.")
    existing = cal.get("throttle")
    if existing is None:
        existing = {}          # absent, or an explicit null someone wrote to
                               # disable the section — both mean "start clean"
    elif not isinstance(existing, dict):
        # A number, a string, a list where a section belongs. Refuse by name
        # rather than dying on .items() one line down; the recommendation the
        # user just read is still good either way.
        sys.exit(f'the "throttle" key in {path} holds a JSON '
                 f"{type(existing).__name__}, not an object; a throttle "
                 f"section is a {{...}} of settings. The recommendation above "
                 f"is still good — replace that key by hand.")
    # Keep only what the feed will accept. A hand-typed `celing_pct` sitting in
    # the file makes the feed refuse to start, and the feed's own error tells
    # the user to run this tool — so preserving the typo here would send them
    # around that loop forever. The learner's output is the thing the feed
    # promised would fix it; it has to actually be clean.
    section = {k: v for k, v in existing.items() if k in THROTTLE_KEYS}
    cal["throttle"] = section   # a comprehension is a NEW dict; setdefault used
                                # to attach it for us and no longer does
    section["floor_pct"] = floor
    section["ceiling_pct"] = ceiling
    section["measured_wall_pct"] = wall
    section["learned_from"] = source
    section["learned_on"] = time.strftime("%Y-%m-%d")
    # Write beside the target and rename over it. calibration.json also holds
    # the learned gear ratios, which took their own drive to measure: a
    # truncating write that is interrupted — Ctrl-C, a full disk, a dying
    # laptop — would take those with it. os.replace is atomic on both
    # platforms, so the file is either the old one or the new one.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cal, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        sys.exit(f"cannot write {path}: {e}. Nothing was changed — the "
                 f"recommendation above is still good, and you can put the "
                 f"throttle section in by hand.")


def build_parser():
    ap = argparse.ArgumentParser(
        description="Learn throttle floor and ceiling from a drive log.")
    ap.add_argument("log", help="CSV from obd_probe.py --log or a feed run log")
    ap.add_argument("--floor-regime", choices=("auto", "coast", "idle"),
                    default="auto",
                    help="which resting reading becomes the floor: coast "
                         "(foot off at speed), idle (stopped), or auto — "
                         "coast when the log has enough of it (default auto)")
    ap.add_argument("--ceiling-margin-pct", type=float,
                    default=CEILING_MARGIN_PCT,
                    help=f"how far below the measured wall the ceiling sits "
                         f"(default {CEILING_MARGIN_PCT}); larger clips less, "
                         f"smaller reaches 100%% more often")
    ap.add_argument("--min-samples", type=int, default=30,
                    help="refuse a recommendation backed by fewer samples "
                         "than this (default 30)")
    # A verb, not a setting: configured, it would rewrite the calibration on
    # every learn run — a write to the one file a drive paid for should
    # always be asked for out loud.
    ap.add_argument("--write", metavar="CALIBRATION_JSON",
                    help="update this calibration file's throttle section "
                         "in place").per_run = True
    return ap


def main():
    args = parse_with_config(build_parser(), "learn_throttle")

    if not 0.0 <= args.ceiling_margin_pct < 100.0:
        sys.exit("--ceiling-margin-pct is a percentage below the wall: "
                 "0 (sit on it) up to but not including 100")

    samples = load_samples(args.log)
    if not samples:
        sys.exit(f"no usable rows in {args.log} — expected columns "
                 f"rpm,speed_kmh,throttle_pct (exactly what obd_probe.py "
                 f"--log writes). A load_pct column is optional and only "
                 f"the feed's own logs carry it; without one the coast "
                 f"floor cannot be measured and you want --floor-regime "
                 f"idle.")

    throttles = [t for _r, _v, t, _ld in samples]
    coast = coast_samples(samples)
    idle = idle_samples(samples)
    have_load = any(ld is not None for _r, _v, _t, ld in samples)

    print(f"{len(samples)} samples from {args.log}")
    print(f"  throttle range      {min(throttles):.1f} .. {max(throttles):.1f} %")
    print(f"  coast samples       {len(coast)}  "
          f"(load <= {COAST_LOAD_PCT:.0f}%, speed > {COAST_MIN_SPEED:.0f} km/h)")
    print(f"  idle samples        {len(idle)}  "
          f"(stopped, rpm >= {IDLE_MIN_RPM:.0f})")
    if not have_load:
        print("  NOTE: this log has no load_pct column — the coast selector "
              "cannot run, so the floor can only come from idle.")

    # --- ceiling -----------------------------------------------------------
    wall, wall_n = find_wall(throttles)
    problems = []
    if wall is None:
        problems.append(
            f"no throttle value repeats {MIN_WALL}+ times, so there is no "
            f"wall to find — this log has no sustained high-throttle event. "
            f"Drive one honest wide-open pull and log it.")
        ceiling = None
    else:
        ceiling = ceiling_from_wall(wall, args.ceiling_margin_pct)
        at_wall = sum(1 for t in throttles if t >= wall)
        over = sum(1 for t in throttles if t >= ceiling)
        print(f"\n  measured wall       {wall:.1f} %  "
              f"({wall_n} samples sit exactly on it, {at_wall} at or above)")
        print(f"  ceiling             {ceiling:.1f} %  "
              f"(wall less {args.ceiling_margin_pct:.1f}%, rounded down)")
        flat_pct = over / len(throttles) * 100.0
        print(f"                      {over} samples "
              f"({flat_pct:.2f}% of the log) would read a flat 100%")
        # find_wall takes the highest value that REPEATS, and a steady cruise
        # repeats as willingly as a plate stop — so a log with no wide-open
        # pull still yields a wall, silently, and it is just the hardest the
        # driver happened to push. The tell is this fraction: a real pull
        # spends ~1% of the log pinned, a cruise mistaken for a wall spends
        # tens of percent. Warn on it rather than trust the number.
        if flat_pct > FLAT_WARN_PCT:
            problems.append(
                f"{flat_pct:.1f}% of this log would read a flat 100% under a "
                f"ceiling of {ceiling:.1f}. That is far too much of a drive to "
                f"be spent at wide-open throttle, so {wall:.1f}% is very "
                f"likely the hardest you pushed rather than the plate's stop. "
                f"Log one honest wide-open pull. If you already know this "
                f"car's wall, write the throttle section into "
                f"calibration.json by hand instead of learning it.")
        # The check above catches a wall that is really a CRUISE, because a
        # cruise is long and shows up as a big flat fraction. It is structurally
        # blind to the opposite shape: a BRIEF hard push — a one-second overtake
        # — in a long gentle drive. Ten samples out of seven thousand is 0.14%
        # flat, under the threshold above and under even a real pull's ~1%, so
        # that log yields a confidently wrong ceiling at exit 0. Two tells that
        # do not depend on log length: a plate's mechanical stop sits near the
        # top of the pedal's range, and it is HELD, not touched.
        elif wall < LOW_WALL_PCT or wall_n < MIN_SUSTAINED:
            problems.append(
                f"the wall this log offers is {wall:.1f}% with only {wall_n} "
                f"sample{'' if wall_n == 1 else 's'} sitting on it"
                + (f" — a pedal's mechanical stop reads near the top of its "
                   f"range, not {wall:.1f}%." if wall < LOW_WALL_PCT else
                   f" — that is a brief hold, not a stop the pedal was resting "
                   f"against.")
                + f" This looks like the hardest you happened to push, not the "
                  f"pedal running out of travel, and a ceiling of "
                  f"{ceiling:.1f}% would make ordinary driving read 100%. Log "
                  f"one honest wide-open pull, or write the throttle section "
                  f"in by hand if you already know this car's wall.")
        if max(throttles) > wall:
            print(f"  NOTE: {sum(1 for t in throttles if t > wall)} sample(s) "
                  f"read above the wall, up to {max(throttles):.1f}% — too few "
                  f"to be the plate's stop, treated as noise.")

    # --- floor -------------------------------------------------------------
    coast_thr = [t for t, _r in coast]
    coast_med = statistics.median(coast_thr) if coast_thr else None
    idle_med = statistics.median(idle) if idle else None

    if args.floor_regime == "coast":
        chosen, floor, why = "coast", coast_med, "asked for"
    elif args.floor_regime == "idle":
        chosen, floor, why = "idle", idle_med, "asked for"
    elif len(coast_thr) >= args.min_samples:
        chosen, floor, why = "coast", coast_med, "enough coast in this log"
    elif idle:
        chosen, floor, why = "idle", idle_med, \
            f"only {len(coast_thr)} coast samples, under --min-samples"
    else:
        chosen, floor, why = "coast", coast_med, "nothing else available"

    print()
    if coast_med is not None:
        print(f"  coast floor         {coast_med:.1f} %  "
              f"(p10 {nearest_rank(coast_thr, 10):.1f}, "
              f"p90 {nearest_rank(coast_thr, 90):.1f}, n={len(coast_thr)})")
    if idle_med is not None:
        print(f"  idle floor          {idle_med:.1f} %  "
              f"(p10 {nearest_rank(idle, 10):.1f}, "
              f"p90 {nearest_rank(idle, 90):.1f}, n={len(idle)})")
    if floor is None:
        problems.append(
            f"no samples in the {chosen} regime, so there is no floor to "
            f"measure. Log some steady cruising with your foot completely "
            f"off at road speed (or --floor-regime idle for a stopped-only "
            f"log, which zeroes idle but not coasting).")
    else:
        floor = round(floor, 1)
        n_backing = len(coast_thr) if chosen == "coast" else len(idle)
        print(f"  floor               {floor:.1f} %  "
              f"(from the {chosen} regime — {why}, n={n_backing})")
        if n_backing < args.min_samples:
            problems.append(
                f"the {chosen} floor rests on {n_backing} samples, under the "
                f"--min-samples floor of {args.min_samples}. Drive more of "
                f"that regime before trusting it.")
        if chosen == "idle" and coast_med is not None and coast_med > idle_med:
            print(f"  NOTE: coasting reads {coast_med - idle_med:.1f} points "
                  f"higher than idle. An idle floor leaves that much phantom "
                  f"throttle on the overlay while you coast.")

    # --- how much of the floor is rpm, not pedal ---------------------------
    bins = rpm_bins(coast)
    if len(bins) > 1:
        print("\n  the coast floor is not one number — it moves with rpm:")
        print("      rpm band       median thr   samples")
        for lo, med, n in bins:
            print(f"      {lo:>5.0f}-{lo + RPM_BIN:<5.0f}    "
                  f"{med:>7.1f} %   {n:>7}")
        spread = max(m for _lo, m, _n in bins) - min(m for _lo, m, _n in bins)
        if spread > SPREAD_WARN_PCT:
            print(f"  Spread {spread:.1f} points across the range. A two-point "
                  f"map has ONE floor, so it lands in the middle of that and "
                  f"leaves a residual at both ends. Expected on this engine; "
                  f"nothing here can remove it.")

    # --- what the recommendation actually does to your coasting ------------
    # `coast` gates the call, not just the print: residual() takes a median of
    # the mapped samples, and a log with no coast in it (every obd_probe --log
    # capture, every city drive, every --floor-regime idle run) would hand
    # statistics.median an empty list and die here with a traceback.
    if floor is not None and ceiling is not None and ceiling > floor and coast:
        med, p90, zeros = residual(coast, floor, ceiling)
        print(f"\n  under this map your coast samples read: median "
              f"{med:.1f}%, p90 {p90:.1f}%, {zeros} of {len(coast)} "
              f"({zeros / len(coast) * 100.0:.0f}%) exactly zero")

    # --- verdict -----------------------------------------------------------
    if floor is not None and ceiling is not None and ceiling <= floor:
        problems.append(
            f"ceiling {ceiling:.1f} is not above floor {floor:.1f} — that map "
            f"has no span and would divide by zero at the feed. The log is "
            f"missing a real wide-open pull, or the floor regime caught "
            f"something that was not resting.")

    if problems:
        sys.stdout.flush()  # keep the numbers above the FAIL line when piped
        print("\nFAIL: not written.", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"\n  calibration.json:  "
          f'"throttle": {{ "floor_pct": {floor:.1f}, '
          f'"ceiling_pct": {ceiling:.1f} }}')
    print(f"  mapping:           "
          f"out = clamp((raw - floor) / (ceiling - floor), 0, 1)")

    if args.write:
        write_calibration(args.write, floor, ceiling, args.log, wall)
        print(f"\nthrottle section written to {args.write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
