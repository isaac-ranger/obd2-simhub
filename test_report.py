"""Tests for report.py. Run: python test_report.py

Synthetic logs with a known story assert each section's judgment; the two
real logs in reports/ assert the tool against ground truth Kris already
confirmed from the driver's seat. Stdlib only; writes only to tempfiles.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from report import (load_rows, gear_report, shift_report, warmup_report,
                    extremes_report, overview, STAND_KMH, SHIFT_MAX_S)

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


def write_log(header, rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="")
    f.write(header + "\n")
    for r in rows:
        f.write(",".join(str(c) for c in r) + "\n")
    f.close()
    return f.name


CONSTANTS = [103.0, 60.4, 43.3, 35.0, 29.2, 25.2]

# --- synthetic drive: launch, one timed shift, one direct shift, a coast,
# --- a clutch dip, and a stop. 5 Hz, integer speeds like the real thing.
rows = []
t = 0.0


def seg(seconds, gear, ratio, speed0, speed1):
    """Append `seconds` of samples holding `gear` (0 = neutral) while speed
    ramps speed0 -> speed1; rpm follows ratio (or idles in neutral)."""
    global t
    n = int(seconds * 5)
    for i in range(n):
        spd = round(speed0 + (speed1 - speed0) * i / max(1, n - 1))
        rpm = ratio * spd if ratio else 900
        rows.append((f"{t:.3f}", gear, f"{rpm:.1f}", spd, "20.0"))
        t += 0.2


seg(4.0, 0, None, 0, 0)              # standing at idle
seg(3.0, 1, 103.0, 9, 20)            # 1st, pulling away
seg(0.6, 0, None, 20, 21)            # clutch window (a shift: < SHIFT_MAX_S)
seg(4.0, 2, 60.4, 21, 40)            # 2nd
seg(1.0, 3, 43.3, 40, 42)            # direct 2->3, no neutral shown
seg(4.0, 0, None, 42, 30)            # coasting (>= SHIFT_MAX_S)
seg(3.0, 3, 43.3, 30, 33)            # back in 3rd
seg(1.0, 0, None, 33, 32)            # clutch dip...
seg(2.0, 3, 43.3, 32, 34)            # ...same gear both sides
seg(2.0, 0, None, 2, 0)              # stopped: neither shift nor coast

path = write_log("t_s,gear,rpm,speed_kmh,throttle_pct",
                 [(a, b, c, d, e) for a, b, c, d, e in rows])
R = load_rows(path)

lines = shift_report(R)
text = "\n".join(lines)
ok("shifts: exactly two (one timed, one direct)",
   "2 shifts" in text, text)
ok("shifts: the timed 1->2 shows its clutch window",
   any("1 -> 2" in ln and "s in neutral" in ln for ln in lines), text)
ok("shifts: the direct 2->3 reads clean rev-match",
   any("2 -> 3" in ln and "clean rev-match" in ln for ln in lines), text)
ok("shifts: one coast, and the stop is neither shift nor coast",
   "1 coast" in text, text)
ok("shifts: the clutch dip is named, not counted as a shift",
   "1 clutch dip" in text and "in 3" in text, text)

glines = gear_report(R, CONSTANTS, 7)
gtext = "\n".join(glines)
ok("gears: 1..3 confirmed on synthetic drive",
   all(f"  {g}  " in ln and "CONFIRMED" in ln
       for g in (1, 2, 3) for ln in glines if ln.strip().startswith(str(g))),
   gtext)
ok("gears: 5 and 6 honestly not seen",
   sum("not seen" in ln for ln in glines) >= 2, gtext)
ok("gears: neutral split standing vs moving",
   any("standing" in ln and "moving" in ln for ln in glines), gtext)
os.unlink(path)

# --- warm-up: oil lagging coolant must draw the note
wpath = write_log("t_s,coolant_c,oil_c",
                  [(i, 50 + i, 45 + i * 0.4) for i in range(60)])
wlines = warmup_report(load_rows(wpath))
wtext = "\n".join(wlines)
ok("warm-up: coolant crossing 90 is dated", "reached 90C" in wtext, wtext)
ok("warm-up: cold-oil note fires when coolant says ready first",
   "not an oil gauge" in wtext, wtext)
os.unlink(wpath)

wpath = write_log("t_s,coolant_c,oil_c",
                  [(i, 90 + i * 0.1, 85 + i * 0.1) for i in range(30)])
wlines = warmup_report(load_rows(wpath))
ok("warm-up: no cold-oil note when oil is actually warm",
   not any("not an oil gauge" in ln for ln in wlines), "\n".join(wlines))
os.unlink(wpath)

# --- the real logs assert the story Kris confirmed from the seat ----------
DRIVE1 = os.path.join(HERE, "reports", "2026-07-31-kris-drive_01.csv")
DRIVE2 = os.path.join(HERE, "reports", "2026-07-31-kris-drive_02.csv")

R1 = load_rows(DRIVE1)
g1 = "\n".join(gear_report(R1, CONSTANTS, 7))
ok("drive_01: all six gears confirmed (the calibration sweep)",
   g1.count("CONFIRMED") == 6, g1)
ok("drive_01: no gear column -> shift section says so, no crash",
   "no gear column" in "\n".join(shift_report(R1)))

R2 = load_rows(DRIVE2)
g2 = "\n".join(gear_report(R2, CONSTANTS, 7))
ok("drive_02: gears 1-4 confirmed, 5-6 not seen",
   g2.count("CONFIRMED") == 4 and g2.count("not seen") == 2, g2)
ok("drive_02: every confirmed delta within 2 percent",
   all(abs(float(ln.split("%")[0].split()[-1])) <= 2.0
       for ln in g2.splitlines() if "CONFIRMED" in ln), g2)
s2 = "\n".join(shift_report(R2))
ok("drive_02: five shifts including the rev-matched 4->3",
   "5 shifts" in s2 and "4 -> 3" in s2, s2)
w2 = "\n".join(warmup_report(R2))
ok("drive_02: the oil-lag note fires (99C coolant, 71C oil)",
   "not an oil gauge" in w2, w2)

# --- CLI end to end -------------------------------------------------------
proc = subprocess.run([sys.executable, os.path.join(HERE, "report.py"),
                       DRIVE2], capture_output=True, text=True)
ok("cli: exit 0 and all five sections present",
   proc.returncode == 0 and all(s in proc.stdout for s in
                                ("DRIVE", "GEARS", "SHIFTS", "WARM-UP",
                                 "EXTREMES")),
   proc.stdout[-400:] + proc.stderr[-400:])
proc = subprocess.run([sys.executable, os.path.join(HERE, "report.py"),
                       DRIVE2, "--calibration", "/nonexistent.json"],
                      capture_output=True, text=True)
ok("cli: missing calibration degrades, still exits 0",
   proc.returncode == 0 and "no calibration" in proc.stdout,
   proc.stdout[-400:])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
