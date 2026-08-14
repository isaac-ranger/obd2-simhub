"""Tests for gps/gps_verify.py. Run: python gps/test_gps_verify.py

Synthetic captures only — built from the same ballpark coordinates the
nmea suite already uses, so no real drive ever needs to live in the repo.
The controls are the point: a stationary file must PASS stationary and
FAIL drive, and the reverse, or the verify tool is a rubber stamp.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gps_verify import analyze, parse_capture_lines, report, verdict

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


def cs(body):
    """Append a real checksum to a $-sentence body."""
    x = 0
    for ch in body[1:]:
        x ^= ord(ch)
    return body + "*%02X" % x


def rmc(lat_dm="3248.6613", lon_dm="11715.0748", sog_kn="0.0", cog="341.9"):
    return cs(f"$GPRMC,120000.000,A,{lat_dm},N,{lon_dm},W,"
              f"{sog_kn},{cog},130826,,,D")


def gga(lat_dm="3248.6613", lon_dm="11715.0748"):
    return cs(f"$GPGGA,120000.000,{lat_dm},N,{lon_dm},W,2,18,0.6,"
              f"70.7,M,-35.1,M,,0000")


def capture_text(sentences, hz=10.0):
    return [f"{i / hz:.3f}\t{s}\n" for i, s in enumerate(sentences)]


# --- parse_capture_lines: the two-column contract ----------------------------

rows, bad = parse_capture_lines(["0.100\t$GPRMC,x\n", "no tab here\n",
                                 "nan-ish\t$ok\n", "\n", "x\t$y\n"])
ok("rows: tabbed rows with float stamps land",
   len(rows) == 1 and rows[0] == (0.1, "$GPRMC,x"), f"{rows!r}")
ok("rows: no-tab and bad-stamp lines are counted, not dropped silently",
   bad == 3, f"{bad}")

# --- a stationary capture: the driveway control ------------------------------

still = []
for _ in range(30):
    still.append(rmc())
    still.append(gga())
still.append("$GPPWR,04C6,0,0,0,0,00,5,S,97, 1 8 ,S00*0D")
# The connection-rite binary packet, in the framer's escaped spelling.
still.insert(0, "U\\x04\\x008\\x00\\x00\\xef\\x00" + cs(
    "$GPGSA,A,1,,,,,,,,,,,,,0.0,0.0,0.0"))
rows, _ = parse_capture_lines(capture_text(still))
st = analyze(rows)

ok("stationary: every RMC parses and is valid",
   st["rmc_parsed"] == 30 and st["rmc_valid"] == 30,
   f"{st['rmc_parsed']}/{st['rmc_valid']}")
ok("stationary: one unique position", st["unique_positions"] == 1)
ok("stationary: footprint is zero meters",
   st["dmax_m"] == 0.0 and st["ew_m"] == 0.0, f"{st['dmax_m']}")
ok("stationary: the escaped binary line is counted as damage",
   st["damaged"] == 1, f"{st['damaged']}")
ok("stationary: checksums counted, none failed",
   st["cs_present"] == 62 and st["cs_failed"] == 0,
   f"{st['cs_present']}/{st['cs_failed']}")

passed, why = verdict(st, "stationary")
ok("stationary: PASSES its own control", passed, why)
passed, why = verdict(st, "drive")
ok("stationary: FAILS the drive claim — the discriminator works",
   not passed, why)

# --- a drive: north about 550 m at speed -------------------------------------

drive = []
for i in range(50):
    lat_dm = f"{3248.6613 + i * 0.006:.4f}"   # ~11 m per fix, northward
    drive.append(rmc(lat_dm=lat_dm, sog_kn="21.6"))  # ~40 km/h
rows, _ = parse_capture_lines(capture_text(drive))
dr = analyze(rows)

ok("drive: footprint is hundreds of meters, not wander",
   500 < dr["dmax_m"] < 600, f"{dr['dmax_m']:.1f}")
passed, why = verdict(dr, "drive")
ok("drive: PASSES the drive claim", passed, why)
passed, why = verdict(dr, "stationary")
ok("drive: FAILS the stationary claim", not passed, why)

# --- damage and rejection ----------------------------------------------------

broken = rmc()[:-2] + "00"   # flip the checksum
rows, _ = parse_capture_lines(capture_text([broken, rmc()]))
bs = analyze(rows)
ok("a flipped checksum is counted failed and not parsed as a fix",
   bs["cs_failed"] == 1 and bs["rmc_valid"] == 1,
   f"failed={bs['cs_failed']} valid={bs['rmc_valid']}")

rows, _ = parse_capture_lines(capture_text(["$GPGSV,junk*00", "$GPPWR,x"]))
ok("no valid fixes means both claims fail, loudly",
   not verdict(analyze(rows), "stationary")[0]
   and not verdict(analyze(rows), "drive")[0])

# --- the report's manners ----------------------------------------------------

text = report(st, "control.txt")
# 30 RMC over the file's 6.1 s span: the census rate is measured, not assumed.
ok("report: per-type rate is computed from the stamps", "4.9 Hz" in text,
   text)
ok("report: never prints a coordinate",
   "48.66" not in text and "15.07" not in text
   and "3248" not in text and "11715" not in text, text)
ok("report: names the damage count", "1 line(s) carry" in text, text)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
