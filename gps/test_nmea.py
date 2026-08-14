"""Tests for gps/nmea.py. Run: python gps/test_nmea.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nmea import dm_to_degrees, enu_meters, nmea_checksum_ok, parse_gga, parse_rmc

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


# From the parked XGPS160 capture
RMC = ("$GPRMC,215658.400,A,3248.6613,N,11715.0748,W,000.0,341.9,130826,,,D*76")

ok("checksum: known-good RMC passes", nmea_checksum_ok(RMC))
ok("checksum: flipped CS fails",
   not nmea_checksum_ok(RMC[:-2] + "00"))

fix = parse_rmc(RMC)
ok("rmc: parses", fix is not None)
ok("rmc: valid", fix["valid"] is True)
ok("rmc: lat in San Diego ballpark",
   fix is not None and 32.8 < fix["lat"] < 32.82, f"{fix and fix['lat']}")
ok("rmc: lon in San Diego ballpark",
   fix is not None and -117.26 < fix["lon"] < -117.24, f"{fix and fix['lon']}")
ok("rmc: speed 0", fix is not None and abs(fix["speed_kmh"]) < 0.01)
ok("rmc: course 341.9",
   fix is not None and abs(fix["course_deg"] - 341.9) < 0.01)

void = RMC.replace(",A,", ",V,", 1)
# Recompute CS after the edit.
body, _, _cs = void.partition("*")
x = 0
for ch in body[1:]:
    x ^= ord(ch)
void = body + "*%02X" % x
ok("rmc: void status is invalid",
   parse_rmc(void) is not None and parse_rmc(void)["valid"] is False)


# Empty course when stopped (field present but blank)
blank_cog = ("$GPRMC,120000.000,A,3248.6613,N,11715.0748,W,000.0,,130826,,,A*00")
# Fix checksum for blank_cog - easier to skip CS by stripping
body = blank_cog.split("*")[0]
cs = 0
for ch in body[1:]:
    cs ^= ord(ch)
blank_cog = body + "*%02X" % cs
f2 = parse_rmc(blank_cog)
ok("rmc: empty course -> None", f2 is not None and f2["course_deg"] is None)

# Dual binary prefix before '$'
junk = "U\x04\x008\x00\x00" + RMC
ok("rmc: resyncs past binary junk", parse_rmc(junk) is not None)

ok("dm: 3248.6613 N",
   abs(dm_to_degrees("3248.6613", "N") - (32 + 48.6613 / 60)) < 1e-9)

e, n = enu_meters(32.0, -117.0, 32.0, -117.0)
ok("enu: zero at origin", abs(e) < 1e-9 and abs(n) < 1e-9)
e, n = enu_meters(32.0, -117.0, 32.001, -117.0)
ok("enu: north is positive for +lat", n > 100 and abs(e) < 1e-6, f"e={e} n={n}")
e, n = enu_meters(32.0, -117.0, 32.0, -116.999)
ok("enu: east is positive for +lon", e > 50 and abs(n) < 1e-6, f"e={e} n={n}")

GGA = ("$GPGGA,215658.500,3248.6613,N,11715.0748,W,2,18,0.6,70.7,M,-35.1,M,,0000*57")
gga = parse_gga(GGA)
ok("gga: parses", gga is not None)
ok("gga: DGPS quality 2", gga and gga["quality"] == 2)
ok("gga: sats", gga and gga["sats"] == 18)
ok("gga: hdop 0.6", gga and abs(gga["hdop"] - 0.6) < 1e-9)
ok("gga: accuracy_m = hdop * 5",
   gga and abs(gga["accuracy_m"] - 3.0) < 1e-9, f"{gga and gga['accuracy_m']}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
