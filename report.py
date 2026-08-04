#!/usr/bin/env python3
"""
report.py — post-drive report from a run log
============================================

Reads a drive CSV (obd_probe.py --log, or the run log obd_feed.py writes as
a side effect) and prints what the drive can testify to: sample cadence,
channel coverage, which gears were confirmed against calibration and how
tightly, real shifts separated from coasting, warm-up curves, and extremes.

Usage:
  python report.py reports/2026-07-31-kris-drive_02.csv
  python report.py drive.csv --calibration calibration.json

The gear sections need gears.rpm_per_kmh from calibration.json (learned by
probe/learn_gears.py); without it they are skipped and the rest still prints.

Definitions, so the numbers are auditable:
  * steady sample — consecutive-pair ratio agreement within STEADY_PCT with
    rpm > MIN_RPM and speed > MIN_SPEED, same gates as the learner. A gear's
    measured constant is the median of its steady in-band samples.
  * shift — engaged gear A, a neutral window shorter than SHIFT_MAX_S while
    still moving, then engaged gear B. A == B is a clutch dip, not a shift.
  * coast — a moving neutral window of SHIFT_MAX_S or longer. Standing
    neutral (speed <= STAND_KMH) is idle/stopped and counted separately.
  A log without a gear column (probe format) assigns each steady sample to
  the nearest calibration band; shift/coast tables need the logged gear
  column and are skipped without it.

Stdlib only; no pyserial, no hardware. Reads one CSV, writes nothing.
"""

import argparse
import csv
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from obd_config import parse_with_config

MIN_RPM = 900.0        # below this the engine is idling or off
MIN_SPEED = 8.0        # km/h; below this speed quantization wrecks the ratio
STEADY_PCT = 3.0       # consecutive-sample ratio agreement = "holding a gear"
MIN_CONFIRM = 8        # steady in-band samples before a gear reads CONFIRMED
STAND_KMH = 2.0        # at or below this the car is standing
SHIFT_MAX_S = 2.5      # moving-neutral shorter than this = a shift in progress
WARM_COOLANT_C = 90.0  # "the coolant gauge says ready" line
WARM_OIL_C = 80.0      # oil actually ready (conservative street number)


def load_rows(path):
    """[(dict per row with floats or None)] tolerating both log formats.

    A feed-format row whose gear cell is missing or unparseable is dropped
    whole: the likeliest cause is a final line cut mid-write when the run
    was killed in the car, and every gear consumer downstream indexes on
    that column. Probe-format logs have no gear column and are untouched."""
    rows = []
    try:
        f = open(path, newline="")
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}")
    with f:
        reader = csv.DictReader(f)
        has_gear = "gear" in (reader.fieldnames or [])
        for raw in reader:
            row = {}
            for k, v in raw.items():
                if k is None or v is None or v == "":
                    row[k] = None
                else:
                    try:
                        row[k] = float(v)
                    except ValueError:
                        row[k] = None
            if row.get("t_s") is None:
                continue
            if has_gear and row.get("gear") is None:
                continue                     # damaged row, not data
            rows.append(row)
    return rows


def fmt_span(seconds):
    whole = int(round(seconds))
    sign = "-" if whole < 0 else ""
    m, s = divmod(abs(whole), 60)
    return f"{sign}{m}:{s:02d} min" if m else f"{sign}{s}s"


# --------------------------------------------------------------------------
# Sections. Each returns a list of lines; main() joins them. Anything a log
# cannot support returns a note saying so instead of a guess.
# --------------------------------------------------------------------------

def overview(rows, path):
    t = [r["t_s"] for r in rows]
    span = t[-1] - t[0]
    dts = sorted(b - a for a, b in zip(t, t[1:]) if b > a)
    lines = [f"log      {os.path.basename(path)}",
             f"samples  {len(rows)} over {fmt_span(span)}"]
    backwards = sum(1 for a, b in zip(t, t[1:]) if b < a)
    if backwards:
        lines.append(f"WARNING  time runs backwards {backwards}x — "
                     f"concatenated runs? Every duration below is suspect; "
                     f"split the file at the reset and report each run alone")
    if dts:
        med = dts[len(dts) // 2]
        p95 = dts[int(len(dts) * 0.95)]
        lines.append(f"cadence  {1.0 / med:.1f} Hz median "
                     f"(gap p95 {p95 * 1000:.0f} ms, worst {dts[-1] * 1000:.0f} ms)")
    channels = [k for k in rows[0] if k not in ("t_s", "gear")]
    filled = {k: sum(1 for r in rows if r.get(k) is not None) for k in channels}
    full = sorted(k for k, n in filled.items() if n == len(rows))
    rot = sorted((k, n) for k, n in filled.items() if 0 < n < len(rows))
    dead = sorted(k for k, n in filled.items() if n == 0)
    if full:
        lines.append(f"channels {', '.join(full)}: every sample")
    if rot:
        lines.append("         " + ", ".join(
            f"{k} {100 * n // len(rows)}%" for k, n in rot) + " (rotating tier)")
    if dead:
        lines.append(f"         {', '.join(dead)}: never answered")
    return lines


def gear_stints(rows):
    """[(gear:int, i0, i1)] from the logged gear column; None if no column."""
    if "gear" not in rows[0] or rows[0].get("gear") is None:
        return None
    stints, start = [], 0
    for i in range(1, len(rows)):
        if int(rows[i]["gear"]) != int(rows[i - 1]["gear"]):
            stints.append((int(rows[i - 1]["gear"]), start, i - 1))
            start = i
    stints.append((int(rows[-1]["gear"]), start, len(rows) - 1))
    return stints


def steady_mask(rows):
    """Per-row: is this row's ratio steady vs its predecessor (learner gates)?"""
    mask = [False] * len(rows)
    for i in range(1, len(rows)):
        r0, r1 = rows[i - 1], rows[i]
        if None in (r0.get("rpm"), r0.get("speed_kmh"),
                    r1.get("rpm"), r1.get("speed_kmh")):
            continue
        if min(r0["rpm"], r1["rpm"]) > MIN_RPM and \
           min(r0["speed_kmh"], r1["speed_kmh"]) > MIN_SPEED:
            q0 = r0["rpm"] / r0["speed_kmh"]
            q1 = r1["rpm"] / r1["speed_kmh"]
            if abs(q1 - q0) / q0 * 100.0 < STEADY_PCT:
                mask[i] = True
    return mask


def nearest_band(ratio, constants, tol_pct):
    best, best_err = 0, None
    for i, c in enumerate(constants):
        err = abs(ratio - c) / c
        if best_err is None or err < best_err:
            best, best_err = i + 1, err
    return best if best_err is not None and best_err <= tol_pct / 100.0 else 0


def gear_report(rows, constants, tol_pct):
    """Ladder verification + time-in-gear. Works with or without a gear
    column: logged gear labels the sample when present, nearest band else."""
    stints = gear_stints(rows)
    steady = steady_mask(rows)
    per_gear = {}          # gear -> [steady ratios]
    time_in = {}           # gear -> seconds (logged column only)
    for i, r in enumerate(rows):
        if stints is not None:
            g = int(r["gear"])
            if i + 1 < len(rows):
                time_in[g] = time_in.get(g, 0.0) + rows[i + 1]["t_s"] - r["t_s"]
        else:
            g = 0
            if steady[i]:
                g = nearest_band(r["rpm"] / r["speed_kmh"], constants, tol_pct)
        if steady[i] and g > 0:
            per_gear.setdefault(g, []).append(r["rpm"] / r["speed_kmh"])
    lines = ["gear   calibration   measured   delta    evidence"]
    for g, c in enumerate(constants, 1):
        qs = per_gear.get(g, [])
        if len(qs) >= MIN_CONFIRM:
            med = statistics.median(qs)
            verdict = f"CONFIRMED ({len(qs)} steady samples)"
            lines.append(f"  {g}    {c:>8.1f}      {med:>7.1f}   "
                         f"{(med - c) / c * 100.0:+5.1f}%   {verdict}")
        elif qs:
            med = statistics.median(qs)
            lines.append(f"  {g}    {c:>8.1f}      {med:>7.1f}   "
                         f"{(med - c) / c * 100.0:+5.1f}%   "
                         f"thin ({len(qs)} samples — not enough to confirm)")
        else:
            lines.append(f"  {g}    {c:>8.1f}      {'—':>7}         "
                         f"  not seen this drive")
    if time_in:
        engaged = ", ".join(f"{g}: {fmt_span(time_in[g])}"
                            for g in sorted(time_in) if g > 0)
        lines.append(f"time in gear   {engaged or '(never engaged)'}")
        if 0 in time_in:
            standing = moving = 0.0
            for g, i0, i1 in gear_stints(rows):
                if g != 0:
                    continue
                for i in range(i0, min(i1 + 1, len(rows) - 1)):
                    dt = rows[i + 1]["t_s"] - rows[i]["t_s"]
                    spd = rows[i].get("speed_kmh")
                    if spd is not None and spd > STAND_KMH:
                        moving += dt
                    else:
                        standing += dt
            lines.append(f"neutral        standing {fmt_span(standing)}, "
                         f"moving (clutch in / coasting) {fmt_span(moving)} "
                         f"— honest N, the inference declines rather than guesses")
        lines.append("(quantization note: OBD speed is integer km/h; "
                     "measured constants ride on that floor)")
    return lines


def shift_report(rows):
    """Shifts vs coasts from the logged gear column."""
    stints = gear_stints(rows)
    if stints is None:
        return ["(no gear column in this log — shift analysis needs the "
                "feed's run log or probe --log with gear)"]
    shifts, dips, coasts = [], [], []
    # a rev-match clean enough that the judge re-bands without ever showing
    # neutral is still a shift — adjacent engaged stints, zero clutch window
    for j in range(len(stints) - 1):
        a, b = stints[j], stints[j + 1]
        if a[0] > 0 and b[0] > 0 and a[0] != b[0]:
            at = rows[b[1]]["t_s"]
            spd = rows[b[1]].get("speed_kmh") or 0.0
            shifts.append((at, a[0], b[0], 0.0, spd))
    for j in range(1, len(stints) - 1):
        g, i0, i1 = stints[j]
        if g != 0:
            continue
        prev_g, next_g = stints[j - 1][0], stints[j + 1][0]
        if prev_g == 0 or next_g == 0:
            continue
        dwell = rows[min(i1 + 1, len(rows) - 1)]["t_s"] - rows[i0]["t_s"]
        speeds = [rows[i]["speed_kmh"] for i in range(i0, i1 + 1)
                  if rows[i].get("speed_kmh") is not None]
        if not speeds or max(speeds) <= STAND_KMH:
            continue                      # a stop, not a shift or a coast
        at = rows[i0]["t_s"]
        if dwell >= SHIFT_MAX_S:
            coasts.append((at, prev_g, next_g, dwell))
        elif prev_g == next_g:
            dips.append((at, prev_g, dwell))
        else:
            shifts.append((at, prev_g, next_g, dwell,
                           statistics.median(speeds)))
    lines = []
    if shifts:
        shifts.sort()
        lines.append(f"{len(shifts)} shifts (gear-to-gear, clutch window "
                     f"under {SHIFT_MAX_S:.1f}s):")
        for at, a, b, dwell, spd in shifts:
            arrow = "up  " if b > a else "down"
            window = (f"{dwell:.2f}s in neutral" if dwell > 0
                      else "no neutral visible (clean rev-match)")
            lines.append(f"  t={at:6.1f}s  {a} -> {b}  {arrow}  "
                         f"{window}  at ~{spd:.0f} km/h")
        timed = sorted(d for _t, _a, _b, d, _s in shifts if d > 0)
        if timed:
            lines.append(f"  median clutch window {timed[len(timed) // 2]:.2f}s "
                         f"(timed shifts only)")
    else:
        lines.append("no completed gear-to-gear shifts in this log")
    if dips:
        lines.append(f"{len(dips)} clutch dip(s) (same gear both sides): " +
                     ", ".join(f"t={t:.0f}s in {g} ({d:.1f}s)"
                               for t, g, d in dips))
    if coasts:
        lines.append(f"{len(coasts)} coast(s) (moving in neutral "
                     f">= {SHIFT_MAX_S:.1f}s):")
        for at, a, b, dwell in coasts:
            lines.append(f"  t={at:6.1f}s  left {a}, took {b}  "
                         f"{dwell:.1f}s rolling")
    return lines


def warmup_report(rows):
    lines = []
    for col, label, ready in (("coolant_c", "coolant", WARM_COOLANT_C),
                              ("oil_c", "oil", WARM_OIL_C)):
        pts = [(r["t_s"], r[col]) for r in rows if r.get(col) is not None]
        if not pts:
            continue
        (t0, v0), (t1, v1) = pts[0], pts[-1]
        crossed = next((t for t, v in pts if v >= ready), None)
        state = (f"reached {ready:.0f}C at t={crossed:.0f}s" if crossed
                 else f"never reached {ready:.0f}C — ended at {v1:.0f}C")
        lines.append(f"{label:8s} {v0:.0f}C -> {v1:.0f}C   {state}")
    if len(lines) == 2:
        cool = [(r["t_s"], r["coolant_c"]) for r in rows
                if r.get("coolant_c") is not None]
        oil = [(r["t_s"], r["oil_c"]) for r in rows if r.get("oil_c") is not None]
        t_cool = next((t for t, v in cool if v >= WARM_COOLANT_C), None)
        if t_cool is not None:
            oil_then = min(oil, key=lambda p: abs(p[0] - t_cool))[1]
            if oil_then < WARM_OIL_C:
                lines.append(f"note: when the coolant said ready, oil was at "
                             f"{oil_then:.0f}C — the coolant gauge is not an "
                             f"oil gauge; give it the extra minutes")
    return lines or ["(no temperature channels in this log)"]


def extremes_report(rows, cal):
    lines = []
    rpm = [r["rpm"] for r in rows if r.get("rpm") is not None]
    spd = [r["speed_kmh"] for r in rows if r.get("speed_kmh") is not None]
    if rpm:
        line = f"max rpm  {max(rpm):.0f}"
        max_rpm = (cal.get("engine", {}) or {}).get("max_rpm")
        if max_rpm:
            line += f"  ({100 * max(rpm) / max_rpm:.0f}% of {max_rpm:.0f} redline)"
        lines.append(line)
    if spd:
        factor = 1.0
        sets = cal.get("tire_sets", {})
        active = sets.get(cal.get("active_set", ""), {})
        factor = float(active.get("speed_factor", 1.0))
        line = f"max speed  {max(spd) * factor:.0f} km/h true"
        if factor != 1.0:
            line += f" (OBD {max(spd):.0f} x {factor} for {cal.get('active_set')})"
        lines.append(line)
    thr = [r["throttle_pct"] for r in rows if r.get("throttle_pct") is not None]
    if thr:
        lines.append(f"max throttle  {max(thr):.0f}%")
    volts = [r["voltage_v"] for r in rows if r.get("voltage_v") is not None]
    if volts:
        lines.append(f"voltage  {min(volts):.1f} - {max(volts):.1f} V")
    fuel = [r["fuel_pct"] for r in rows if r.get("fuel_pct") is not None]
    if fuel:
        line = f"fuel  {fuel[0]:.0f}% -> {fuel[-1]:.0f}%"
        tank = (cal.get("engine", {}) or {}).get("fuel_tank_l")
        if tank:
            line += (f"  ({fuel[0] * tank / 100.0:.1f} -> "
                     f"{fuel[-1] * tank / 100.0:.1f} L of {tank:.0f})")
        lines.append(line)
    return lines or ["(no extreme-worthy channels in this log)"]


def build_parser():
    ap = argparse.ArgumentParser(description="Post-drive report from a run log.")
    ap.add_argument("log", help="CSV from obd_feed.py's run log or obd_probe.py --log")
    ap.add_argument("--calibration", default=None,
                    help="calibration.json (default: next to this script)")
    return ap


def main():
    args = parse_with_config(build_parser(), "report")

    cal_path = args.calibration or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "calibration.json")
    cal = {}
    try:
        with open(cal_path, encoding="utf-8") as f:
            cal = json.load(f)
    except OSError:
        pass                                  # report what we can without it
    except json.JSONDecodeError as e:
        sys.exit(f"{cal_path} is not valid JSON ({e})")

    rows = load_rows(args.log)
    if len(rows) < 2:
        sys.exit(f"{len(rows)} usable data row(s) in {args.log} — a report "
                 f"needs at least 2 (expected columns: the header "
                 f"obd_probe.py --log or obd_feed.py's run log writes)")

    sections = [("DRIVE", overview(rows, args.log))]
    gears = (cal.get("gears", {}) or {}).get("rpm_per_kmh")
    if gears and not all(isinstance(c, (int, float)) and c > 0 for c in gears):
        sys.exit(f"{cal_path} gears.rpm_per_kmh contains a non-positive "
                 f"constant ({gears}) — a gear ratio can't be zero; re-run "
                 f"probe/learn_gears.py or fix the file by hand")
    if gears:
        tol = float(cal.get("gears", {}).get("tolerance_pct", 7))
        sections.append(("GEARS", gear_report(rows, gears, tol)))
    else:
        sections.append(("GEARS", ["(no calibration.json with gears — run "
                                   "probe/learn_gears.py first)"]))
    sections.append(("SHIFTS", shift_report(rows)))
    sections.append(("WARM-UP", warmup_report(rows)))
    sections.append(("EXTREMES", extremes_report(rows, cal)))

    for title, lines in sections:
        print(f"\n== {title} " + "=" * max(1, 58 - len(title)))
        for ln in lines:
            print(ln)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
