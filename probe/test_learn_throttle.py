"""Fixture tests for learn_throttle. Run: python probe/test_learn_throttle.py
No hardware, no files needed beyond this script — drives synthetic logs through
the same functions the CLI uses, plus one round-trip against the real feed-side
mapping in extractor/obd_feed.py so the two can never drift apart silently."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir))
from learn_throttle import (MIN_WALL, ceiling_from_wall, coast_samples,
                            find_wall, idle_samples, load_samples,
                            nearest_rank, residual, rpm_bins,
                            write_calibration)

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


# --- the wall: repetition is what tells a plate stop from a glitch ---------
# The plate PARKS on its top byte under WOT, so the wall is the highest value
# that repeats. A lone higher sample is noise and must not become the ceiling.
walled = [88.6] * MIN_WALL + [50.0] * 20 + [91.0]
wall, n = find_wall(walled)
ok("wall is the highest REPEATED value, not the max", wall == 88.6, f"got {wall}")
ok("wall reports its own sample count", n == MIN_WALL, f"got {n}")

ok("one sample short of MIN_WALL is not a wall",
   find_wall([88.6] * (MIN_WALL - 1) + [50.0] * 20)[0] == 50.0)
ok("nothing repeating enough yields no wall at all",
   find_wall([1.0, 2.0, 3.0])[0] is None)
ok("empty log yields no wall", find_wall([])[0] is None)

# --- the ceiling: margin below the wall, and rounding goes the same way ----
ok("ceiling backs off the wall by the margin",
   ceiling_from_wall(88.6, 4.0) == 85.0, f"got {ceiling_from_wall(88.6, 4.0)}")
ok("rounding is DOWN — margin's direction, not nearest",
   ceiling_from_wall(100.0, 4.0) == 96.0 and ceiling_from_wall(88.69, 4.0) == 85.1)
ok("zero margin sits exactly on the wall", ceiling_from_wall(88.6, 0.0) == 88.6)
ok("a bigger margin gives a lower ceiling",
   ceiling_from_wall(88.6, 8.0) < ceiling_from_wall(88.6, 4.0))


# --- selectors: they key on load/speed/rpm and NEVER on throttle -----------
def row(rpm, speed, thr, load):
    return (rpm, speed, thr, load)


mixed = [
    row(1200, 0.0, 12.5, 20.0),     # idle, stopped
    row(1300, 0.0, 13.0, None),     # idle, load not yet polled
    row(300, 0.0, 99.0, 20.0),      # engine off/Auto-Stop: not idle
    row(2500, 90.0, 16.5, 2.0),     # coasting at speed
    row(2600, 90.0, 88.6, 2.0),     # coasting by load, WOT by throttle
    row(2500, 30.0, 16.0, 2.0),     # unloaded but too slow to count
    row(3000, 90.0, 60.0, 40.0),    # moving under load: neither regime
    row(3000, 90.0, 16.0, None),    # load missing: cannot judge coast
    row(2400, 50.0, 17.0, 2.0),     # EXACTLY at the boundary: strictly slower
]                                   # than "> 50", so OUT. See below.
coast = coast_samples(mixed)
ok("coast selector takes unloaded-and-moving only", len(coast) == 2, f"got {len(coast)}")
# The selector is `speed > 50`, and the printed line says so. A `>=` slip
# reads identically on every fixture that has no row sitting ON the boundary,
# and on Kris's real log it silently moves coast n 592 -> 610 and the residual
# zeros 316 -> 334 — published numbers, changed by a character. This row is the
# only thing in the suite that can tell the two apart.
ok("coast selector is strictly ABOVE the speed floor, not at-or-above",
   17.0 not in [t for t, _r in coast],
   "a row at exactly COAST_MIN_SPEED was counted as coasting — the selector "
   "is >= where the printout and the published sample counts say >")
ok("coast selector does NOT read throttle — the 88.6%% row is in",
   88.6 in [t for t, _r in coast])
ok("coast selector skips rows with no load_pct", 16.0 not in [t for t, _r in coast])
ok("coast pairs carry rpm for the per-rpm table",
   sorted(r for _t, r in coast) == [2500, 2600])

idle = idle_samples(mixed)
ok("idle selector takes stopped-and-running only", idle == [12.5, 13.0], f"got {idle}")
ok("idle selector keeps rows with no load_pct (it never asks)", 13.0 in idle)
ok("idle selector drops a stopped engine below IDLE_MIN_RPM", 99.0 not in idle)

# --- percentiles are readings the car produced, not interpolations ---------
vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
ok("nearest-rank p90 is a real sample", nearest_rank(vals, 90) == 9.0)
ok("nearest-rank p10 is a real sample", nearest_rank(vals, 10) == 1.0)
ok("nearest-rank p100 is the max", nearest_rank(vals, 100) == 10.0)
ok("nearest-rank of nothing is None", nearest_rank([], 50) is None)

# --- rpm bins: the limit this model cannot represent, made visible --------
climbing = [(14.0, 1200.0), (14.5, 1400.0), (23.0, 7100.0), (23.5, 7300.0)]
bins = rpm_bins(climbing)
ok("rpm bins are ascending by band", [lo for lo, _m, _n in bins] == [1000, 7000])
ok("rpm bins carry their own sample counts", [n for _lo, _m, n in bins] == [2, 2])
ok("rpm bins show the floor climbing with engine speed",
   bins[-1][1] > bins[0][1])

# --- residual: does the recommendation actually zero the coasting? --------
med, p90, zeros = residual([(16.5, 2000.0), (16.0, 2000.0), (20.0, 2000.0)],
                           16.5, 85.0)
ok("residual zeroes samples at or below the floor", zeros == 2, f"got {zeros}")
ok("residual never goes negative", med == 0.0 and p90 >= 0.0)
top = residual([(200.0, 2000.0)], 16.5, 85.0)
ok("residual clamps above the ceiling to 100", top[0] == 100.0, f"got {top[0]}")

# --- the log reader tolerates real obd_probe output -----------------------
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "log.csv")
    with open(p, "w") as f:
        f.write("t_s,gear,rpm,speed_kmh,throttle_pct,load_pct,coolant_c\n")
        f.write("0.013,0,1204.5,0.0,15.3,,\n")            # slow PIDs not in yet
        f.write("0.5,3,2500.0,90.0,16.5,2.0,88\n")
        f.write("\n")                                      # blank line
        f.write("1.0,3,,90.0,16.5,2.0,88\n")               # dropped rpm read
    s = load_samples(p)
    ok("reader keeps rows whose load_pct is blank", len(s) == 2, f"got {len(s)}")
    ok("reader marks a blank load_pct as None", s[0][3] is None)
    ok("reader drops rows missing a required column value",
       all(r is not None for r, _v, _t, _l in s))

    # --- --write preserves the file it is handed --------------------------
    cal = os.path.join(d, "calibration.json")
    json.dump({"active_set": "street_18", "gears": {"rpm_per_kmh": [103.0]}},
              open(cal, "w"))
    write_calibration(cal, 16.5, 85.0, "some/log.csv", 88.6)
    c = json.load(open(cal))
    ok("write: unrelated sections preserved",
       c["active_set"] == "street_18" and c["gears"]["rpm_per_kmh"] == [103.0])
    ok("write: throttle constants and provenance land",
       c["throttle"]["floor_pct"] == 16.5
       and c["throttle"]["ceiling_pct"] == 85.0
       and c["throttle"]["measured_wall_pct"] == 88.6
       and c["throttle"]["learned_from"] == "some/log.csv")

    bad = os.path.join(d, "bad.json")
    open(bad, "w").write("{not json")
    try:
        write_calibration(bad, 16.5, 85.0, "x.csv", 88.6)
        ok("invalid-JSON target refuses", False, "no SystemExit raised")
    except SystemExit:
        ok("invalid-JSON target refuses and is not clobbered",
           open(bad).read() == "{not json")

# --- the feed applies the same map this tool recommends -------------------
# The learner is only worth running if the feed consumes what it writes. This
# drives the real Packer branch rather than re-implementing the arithmetic.
from extractor.obd_feed import LayoutError, Packer

LAYOUT = {"header": [], "fields": [{"name": "thr", "type": "f32",
                                    "source": "derived:throttle_01"}]}


def fed(raw, cal):
    p = Packer(LAYOUT, cal=cal)
    return p._value(p.fields[0], {"pids": {0x11: raw}, "gear": 0,
                                  "true_speed_kmh": 0.0}, {})


CAL = {"throttle_floor_pct": 16.5, "throttle_ceiling_pct": 85.0}
ok("feed maps the floor to 0", fed(16.5, CAL) == 0.0)
ok("feed maps the ceiling to 1", fed(85.0, CAL) == 1.0)
ok("feed clamps below the floor", fed(0.0, CAL) == 0.0)
ok("feed clamps above the ceiling", fed(88.6, CAL) == 1.0)
ok("feed maps the midpoint to ~0.5", abs(fed(50.75, CAL) - 0.5) < 1e-9)
ok("no throttle section = the identity map it always was",
   fed(37.0, {}) == 0.37 and fed(0.0, {}) == 0.0 and fed(100.0, {}) == 1.0)
# A bad map dies at Packer CONSTRUCTION, not on the first packet: the
# constructor's fail-fast self-test resolves every source once against dummy
# data and turns a LayoutError into a loud exit. obd_feed's main() checks the
# same thing one step earlier so the operator sees a calibration message
# rather than a layout one; this is the backstop under it.
for label, bad_cal in (("inverted", {"throttle_floor_pct": 85.0,
                                     "throttle_ceiling_pct": 16.5}),
                       ("zero-span", {"throttle_floor_pct": 50.0,
                                      "throttle_ceiling_pct": 50.0})):
    try:
        fed(50.0, bad_cal)
        ok(f"a {label} map refuses at construction", False, "no exit raised")
    except (SystemExit, LayoutError):
        ok(f"a {label} map refuses at construction", True)

# --- the learner's own main(), driven as a user drives it -----------------
# Nothing above this line enters learn_throttle.main(), and that is where the
# crash lived: residual() was called before the `if coast:` guard, so ANY log
# without coast samples died with a statistics.StatisticsError traceback —
# including every log obd_probe.py --log can produce (its LOG_PIDS carry no
# load_pct, so the coast selector can never fire) and the tool's own
# recommended remedy, --floor-regime idle. A green unit suite said nothing
# about it. These run the CLI the way Kris will.
import subprocess

LEARN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "learn_throttle.py")


def learn_cli(rows, *flags):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "log.csv")
        with open(p, "w") as f:
            f.write("t_s,rpm,speed_kmh,throttle_pct\n")
            f.writelines(rows)
        r = subprocess.run([sys.executable, LEARN, p, *flags],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=60)
        return r.returncode, r.stdout


# A probe-shaped log: idle and a pull, no load_pct column anywhere, so there
# is a floor and a ceiling but zero coast samples. This is THE crash case.
PROBE_LOG = ([f"{i},800,0,12.5\n" for i in range(400)]
             + [f"{400 + i},6000,120,88.6\n" for i in range(6)])

for label, flags in (("default", ()), ("--floor-regime idle",
                                       ("--floor-regime", "idle"))):
    rc, out = learn_cli(PROBE_LOG, *flags)
    ok(f"main(): a coast-less log does not traceback ({label})",
       "Traceback" not in out and "StatisticsError" not in out,
       out[-300:])
    ok(f"main(): a coast-less log still reports its idle floor ({label})",
       "idle floor" in out, out[-200:])

rc, out = learn_cli(PROBE_LOG + [f"{500 + i},6000,120,88.6\n" for i in range(400)])
ok("main(): a log that is mostly wide-open refuses rather than "
   "calling a cruise the wall", rc == 1 and "flat 100%" in out, out[-300:])

# The verdict boundary, which no unit test reaches because it lives in main().
rc, out = learn_cli([f"{i},800,0,50.0\n" for i in range(400)])
ok("main(): a log with no span at all refuses", rc == 1, out[-300:])
ok("main(): the refusal names what is missing, not a stack",
   "Traceback" not in out and "FAIL" in out, out[-300:])


# --- the wiring is real: main() must hand the map to the Packer -----------
# Everything above this line passes with the two lines that install the
# throttle constants into the Packer DELETED from obd_feed's main() — that
# mutation was run and all seven suites stayed green while the live feed
# silently reverted to raw/100. It is the ORIGINAL ship-blocker (an inert
# config block), and no unit test can see it, because every unit test builds
# its own Packer and hands it the cal dict by hand. Only an end-to-end run
# through main() can tell "the feed CAN apply a map" from "the feed DOES".
#
# So: replay identical rows twice, once with a throttle section and once
# without, and read the wire. The offset is discovered from the identity
# run's own bytes rather than hard-coded, so this cannot rot when the packet
# layout changes — it fails loudly instead.
import socket
import struct
import subprocess

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
FEED = os.path.join(REPO, "extractor", "obd_feed.py")
REAL_CAL = os.path.join(REPO, "calibration.json")
COAST_THR = 16.5  # the floor: identity sends 0.165, a wired feed sends 0.0
# Gears the shipped seed config intentionally does NOT carry: a fresh clone
# must refuse to start rather than hand a stranger one car's ratios. The feed
# hard-exits without gears.rpm_per_kmh, so every fixture built from the real
# calibration has to supply its own or it tests the refusal instead of the
# throttle path.
FIXTURE_GEARS = [103.0, 60.4, 43.3, 35.0, 29.2, 25.2]


def cal_fixture():
    """The shipped calibration, made runnable: real sections, test gears."""
    base = json.load(open(REAL_CAL))
    base.setdefault("gears", {})["rpm_per_kmh"] = FIXTURE_GEARS
    base["gears"].setdefault("tolerance_pct", 7)
    return base


def replay_wire(cal_path, drive):
    """Run the real feed over a recorded drive and return one UDP packet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    proc = subprocess.run(
        [sys.executable, FEED, "--replay", drive, "--calibration", cal_path,
         "--udp", f"127.0.0.1:{port}", "--speed", "50", "--run-log", "off"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=60)
    sock.settimeout(0.5)
    packets = []
    while True:
        try:
            packets.append(sock.recv(65535))
        except socket.timeout:
            break
    sock.close()
    return packets, proc


with tempfile.TemporaryDirectory() as d:
    drive = os.path.join(d, "drive.csv")
    with open(drive, "w") as f:
        f.write("t_s,rpm,speed_kmh,throttle_pct\n")
        for i in range(40):
            f.write(f"{i * 0.1:.1f},2000,80,{COAST_THR}\n")

    base = cal_fixture()
    wired_path = os.path.join(d, "wired.json")
    bare_path = os.path.join(d, "bare.json")
    base["throttle"] = {"floor_pct": COAST_THR, "ceiling_pct": 85.0}
    json.dump(base, open(wired_path, "w"))
    base.pop("throttle")
    json.dump(base, open(bare_path, "w"))

    bare_pkts, bare_proc = replay_wire(bare_path, drive)
    wired_pkts, wired_proc = replay_wire(wired_path, drive)

    ok("end-to-end: the feed replays and sends without a throttle section",
       bare_pkts and bare_proc.returncode == 0,
       f"rc={bare_proc.returncode} pkts={len(bare_pkts)} {bare_proc.stdout[-300:]}")
    ok("end-to-end: the feed replays and sends with one",
       wired_pkts and wired_proc.returncode == 0,
       f"rc={wired_proc.returncode} pkts={len(wired_pkts)} {wired_proc.stdout[-300:]}")

    if bare_pkts and wired_pkts:
        # Locate the throttle field by what the identity map must put there.
        raw01 = struct.pack("<f", COAST_THR / 100.0)
        off = bare_pkts[-1].find(raw01)
        ok("end-to-end: the identity run puts raw/100 on the wire", off >= 0,
           "0.165 is nowhere in the packet — layout has no derived:throttle_01 "
           "field, so this test can no longer see the wiring")
        if off >= 0:
            got = struct.unpack_from("<f", wired_pkts[-1], off)[0]
            # THE FALSIFIER. Delete obd_feed's two Packer cal lines and this
            # is the assertion that fires: got would still be 0.165.
            ok("end-to-end: main() WIRES the calibration — a floor sample "
               "reads 0.0 on the wire, not raw/100",
               got == 0.0, f"got {got!r} at offset {off} (0.165 means the "
                           f"throttle section was read and then not used)")
            # Deliberately NOT `bare_pkt != wired_pkt`: packets carry clocks
            # and counters, so whole-packet inequality is true no matter what
            # the throttle field says. Verified — that weaker form passes
            # with the wiring deleted. Compare the field, not the frame.
            ok("end-to-end: the two runs disagree at the throttle field",
               struct.unpack_from("<f", bare_pkts[-1], off)[0] !=
               struct.unpack_from("<f", wired_pkts[-1], off)[0])

    # --- the startup visibility line ------------------------------------
    # This line is the ONLY signal for the two routes the validation gate
    # cannot see: a top-level typo ("throtle": {...}) and an explicit
    # identity section. Both start clean at exit 0 with the map reverted to
    # raw pass-through, so the gate is silent by construction and this
    # sentence is the whole mitigation. A ship-qa mutation made it always
    # claim a live map and both suites stayed green — it was paint. These
    # read it.
    ident_path = os.path.join(d, "identity.json")
    typo_path = os.path.join(d, "typo.json")
    base = cal_fixture()
    base["throttle"] = {"floor_pct": 0.0, "ceiling_pct": 100.0}
    json.dump(base, open(ident_path, "w"))
    base.pop("throttle")
    base["throtle"] = {"floor_pct": COAST_THR, "ceiling_pct": 85.0}
    json.dump(base, open(typo_path, "w"))

    _, ident_proc = replay_wire(ident_path, drive)
    _, typo_proc = replay_wire(typo_path, drive)

    def thr_line(proc):
        for ln in proc.stdout.splitlines():
            if ln.startswith("Throttle  ->"):
                return ln
        return ""

    ok("startup line: a live map reports its own range",
       thr_line(wired_proc).find(f"{COAST_THR:g}..85%") >= 0,
       repr(thr_line(wired_proc)))
    ok("startup line: no section says so, and says what to run",
       "no throttle section" in thr_line(bare_proc)
       and "learn_throttle.py" in thr_line(bare_proc),
       repr(thr_line(bare_proc)))
    # The one that was false before F5: an explicit 0..100 section printed
    # "no throttle section in calibration.json" — a claim about the file
    # that the file contradicts. It must report pass-through WITHOUT
    # denying the section exists.
    ok("startup line: an explicit identity section is not called absent",
       "raw pass-through" in thr_line(ident_proc)
       and "no throttle section" not in thr_line(ident_proc),
       repr(thr_line(ident_proc)))
    ok("startup line: a top-level typo reads as absent and starts clean",
       typo_proc.returncode == 0
       and "no throttle section" in thr_line(typo_proc),
       f"rc={typo_proc.returncode} {thr_line(typo_proc)!r}")


# --- the feed's OWN validation gate ------------------------------------------
# The suite above proves the wiring works when the section is good. It says
# nothing about a bad one, and a ship-qa pass proved the gap by reverting the
# whole validation block to its pre-fix shape and watching every suite stay
# green while a NaN calibration silently pinned the wire to 0.0. These drive
# the feed's main() with a broken section and require a NAMED refusal — the
# one outcome that must never happen is a clean start on a bad map.
with tempfile.TemporaryDirectory() as d:
    drive = os.path.join(d, "drive.csv")
    with open(drive, "w") as f:
        f.write("t_s,rpm,speed_kmh,throttle_pct\n")
        for i in range(20):
            f.write(f"{i * 0.1:.1f},2000,80,{COAST_THR}\n")
    base = cal_fixture()
    base.pop("throttle", None)

    BAD = [
        ("inverted", {"floor_pct": 85.0, "ceiling_pct": 16.5}),
        ("equal span", {"floor_pct": 50.0, "ceiling_pct": 50.0}),
        ("NaN floor", {"floor_pct": float("nan"), "ceiling_pct": 85.0}),
        ("out of range", {"floor_pct": 16.5, "ceiling_pct": 140.0}),
        ("negative", {"floor_pct": -5.0, "ceiling_pct": 85.0}),
        ("string value", {"floor_pct": "16.5", "ceiling_pct": 85.0}),
        ("bool value", {"floor_pct": True, "ceiling_pct": 85.0}),
        ("list value", {"floor_pct": [16.5], "ceiling_pct": 85.0}),
        # The two below are the ones the old block let through: nothing ever
        # asks for a misspelled key, so value validation cannot see it, and
        # `or {}` read a falsy section as an absent one. Both silently
        # restored the identity map — the inert-config defect wearing a hat.
        ("misspelled key", {"floor_pct": 16.5, "celing_pct": 40.0}),
        ("no _pct suffix", {"floor": 16.5, "ceiling": 85.0}),
    ]
    for label, section in BAD:
        p = os.path.join(d, f"bad-{label.replace(' ', '-')}.json")
        base["throttle"] = section
        with open(p, "w") as fh:
            json.dump(base, fh)
        pkts, proc = replay_wire(p, drive)
        ok(f"feed refuses a bad throttle section: {label}",
           proc.returncode != 0 and not pkts,
           f"rc={proc.returncode} pkts={len(pkts)} — the feed STARTED on a "
           f"{label} section. {proc.stdout[-200:]}")
        ok(f"...and the refusal names the file: {label}",
           os.path.basename(p) in proc.stdout or "throttle" in proc.stdout,
           f"unnamed refusal: {proc.stdout[-200:]}")

    for label, section in [("falsy 0", 0), ("falsy empty string", ""),
                           ("falsy list", []), ("non-dict int", 7)]:
        p = os.path.join(d, f"shape-{label.replace(' ', '-')}.json")
        base["throttle"] = section
        with open(p, "w") as fh:
            json.dump(base, fh)
        pkts, proc = replay_wire(p, drive)
        ok(f"feed refuses a throttle section that is not an object: {label}",
           proc.returncode != 0 and not pkts,
           f"rc={proc.returncode} pkts={len(pkts)} — a {label} section became "
           f"the identity map silently. {proc.stdout[-200:]}")

    # The provenance keys the learner writes must NOT trip the unknown-key
    # refusal — otherwise every file the tool itself produces fails to load.
    base["throttle"] = {"floor_pct": 16.5, "ceiling_pct": 85.0,
                        "measured_wall_pct": 88.6, "learned_from": "x.csv",
                        "learned_on": "2026-08-06", "_notes": "hi"}
    p = os.path.join(d, "provenance.json")
    with open(p, "w") as fh:
        json.dump(base, fh)
    pkts, proc = replay_wire(p, drive)
    ok("feed accepts the full section the learner writes, provenance and all",
       proc.returncode == 0 and pkts,
       f"rc={proc.returncode} — the unknown-key check rejects this tool's own "
       f"output. {proc.stdout[-300:]}")


# --- the write path, which is the only part of this tool that can lose work --
# calibration.json also carries the learned gear ratios — their own drive to
# measure. Every failure here used to be a traceback, and the write itself
# truncated in place, so an interrupt could take the gears with it.
GOOD_LOG = ([f"{i},2500,90,16.5,3.0\n" for i in range(3000)]        # coast
            + [f"{3000 + i},800,0,12.5,20.0\n" for i in range(6000)]  # idle
            + [f"{9000 + i},6500,150,88.6,90.0\n" for i in range(160)])  # pull


def learn_write(target, rows=GOOD_LOG):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "log.csv")
        with open(p, "w") as f:
            f.write("t_s,rpm,speed_kmh,throttle_pct,load_pct\n")
            f.writelines(rows)
        r = subprocess.run([sys.executable, LEARN, p, "--write", target],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=60)
        return r.returncode, r.stdout


with tempfile.TemporaryDirectory() as d:
    rc, out = learn_write(os.path.join(d, "cal.json"))
    ok("--write: the reference log writes cleanly", rc == 0, out[-300:])

    for label, target in (("a path whose directory does not exist",
                           os.path.join(d, "nope", "cal.json")),
                          ("a directory", d)):
        rc, out = learn_write(target)
        ok(f"--write refuses {label} by name, not by traceback",
           rc == 1 and "Traceback" not in out
           and ("cannot write" in out or "cannot read" in out), out[-300:])

    # os.replace renames over its target, and the filesystem allows that on a
    # read-only FILE — the permission that governs it lives on the directory.
    # Honouring the bits and not the intent would be a silent regression that
    # only the atomic write introduced.
    ro = os.path.join(d, "ro.json")
    with open(ro, "w") as f:
        f.write('{"engine": {"redline_rpm": 7000}}\n')
    before = open(ro).read()
    os.chmod(ro, 0o444)
    rc, out = learn_write(ro)
    ok("--write refuses a read-only calibration instead of replacing it",
       rc == 1 and "read-only" in out and "Traceback" not in out, out[-300:])
    os.chmod(ro, 0o644)
    ok("--write left the read-only file byte-for-byte intact",
       open(ro).read() == before)

    # Atomicity's visible half: no debris, and every other section survives.
    real = os.path.join(d, "real.json")
    with open(REAL_CAL) as f:
        original = json.load(f)
    with open(real, "w") as f:
        json.dump(original, f)
    rc, out = learn_write(real)
    with open(real) as f:
        written = json.load(f)
    ok("--write preserves every section it did not learn", rc == 0
       and {k: v for k, v in written.items() if k != "throttle"}
       == {k: v for k, v in original.items() if k != "throttle"}, out[-200:])
    ok("--write leaves no .tmp debris behind",
       not [p for p in os.listdir(d) if p.endswith(".tmp")],
       str(os.listdir(d)))

    # Atomicity's other half, and the one no black-box assertion can reach:
    # a write that truncates in place and one that renames a finished file
    # over the target are indistinguishable by their RESULT. They differ in
    # what a half-finished run leaves behind, and nothing here can interrupt
    # one. So test the mechanism instead of the outcome — a truncating write
    # keeps the target's inode, a rename replaces it. Reverting os.replace
    # passes every other test in this block; it fails this one.
    ino_before = os.stat(real).st_ino
    rc, out = learn_write(real)
    ok("--write replaces the file rather than truncating it in place",
       rc == 0 and (ino_before == 0 or os.stat(real).st_ino != ino_before),
       f"inode unchanged ({ino_before}) — the learned gear ratios in this "
       f"file are only as safe as the write is atomic")

# --- corrupt numbers must not become the wall -------------------------------
# float() accepts "1e400" (inf) and "nan". inf sorts above every real reading,
# so a handful of corrupt rows would be elected the wall and then fail to
# convert to a byte — OverflowError, mid-run, on somebody else's log file.
INF_LOG = ([f"{i},2500,90,16.5,3.0\n" for i in range(3000)]
           + [f"{3000 + i},800,0,12.5,20.0\n" for i in range(6000)]
           + [f"{9000 + i},6500,150,88.6,90.0\n" for i in range(160)]
           + [f"{9200 + i},6500,150,1e400,90.0\n" for i in range(10)])
rc, out = learn_cli(INF_LOG)
ok("a log with non-finite throttle values does not traceback",
   "Traceback" not in out and "OverflowError" not in out, out[-300:])
ok("...and the corrupt rows do not become the wall",
   "88.6" in out and "inf" not in out.lower(), out[-300:])


# --- the brief-hold gate, which nothing was asserting ------------------------
# Deleting the `wall < LOW_WALL_PCT or wall_n < MIN_SUSTAINED` branch from
# main() left all 88 checks green while the mutant happily wrote a ceiling of
# 57.6 at exit 0 for a log whose "wall" was six samples of a lift halfway up
# the pedal. The gate was the headline of the commit that added it and it had
# no test: the reference log trips it, but nothing looked at the return code.
BRIEF_HOLD = ([f"{i},2500,90,16.5,3.0\n" for i in range(3000)]
              + [f"{3000 + i},800,0,12.5,20.0\n" for i in range(6000)]
              + [f"{9000 + i},3000,100,60.0,45.0\n" for i in range(6)])
rc, out = learn_cli(BRIEF_HOLD)
ok("main(): a brief hold partway up the pedal is not a wall", rc == 1, out[-300:])
ok("...and the refusal explains the reasoning rather than stacking",
   "Traceback" not in out and "mechanical stop" in out
   and "wide-open pull" in out, out[-400:])

# The other half of the same branch: a value repeated often enough to look
# sustained, but too low on the range to be a mechanical stop.
LOW_WALL = ([f"{i},2500,90,16.5,3.0\n" for i in range(3000)]
            + [f"{3000 + i},800,0,12.5,20.0\n" for i in range(6000)]
            + [f"{9000 + i},3000,100,55.0,45.0\n" for i in range(200)])
rc, out = learn_cli(LOW_WALL)
ok("main(): a sustained-but-low plateau is not a wall either", rc == 1,
   out[-300:])

# And the probe-shaped log, which trips this gate at HEAD — asserted here so
# the earlier no-traceback checks can never be the only thing watching it.
rc, out = learn_cli(PROBE_LOG)
ok("main(): the probe-shaped log's six-sample pull is refused, not rounded",
   rc == 1, out[-300:])


# --- --write must emit a file the feed will actually accept -----------------
# The feed refuses to start on an unknown key in the throttle section, and its
# error tells the user to run this tool. If --write preserved the typo, that
# instruction would be a loop: refuse -> learn -> refuse, exit 0 each time,
# with the tool's own output as the thing being rejected.
from learn_throttle import THROTTLE_KEYS as LEARNER_KEYS
from extractor.obd_feed import THROTTLE_KEYS as FEED_KEYS

ok("the learner and the feed agree on the legal key set",
   LEARNER_KEYS == FEED_KEYS, f"{LEARNER_KEYS ^ FEED_KEYS}")

with tempfile.TemporaryDirectory() as d:
    target = os.path.join(d, "cal.json")
    json.dump({"gears": {"3": 1.34}, "throttle": {"celing_pct": 85.0,
                                                  "floor_pct": 9.9}},
              open(target, "w"))
    rc, out = learn_write(target)
    written = json.load(open(target))
    ok("--write drops a key the feed would refuse", rc == 0
       and "celing_pct" not in written["throttle"], out[-300:])
    ok("...and keeps the rest of the file", written.get("gears") == {"3": 1.34},
       str(written)[:200])
    ok("...and every key it emits is one the feed accepts",
       set(written["throttle"]) <= FEED_KEYS, str(set(written["throttle"])))

    # Nulling the section is a natural way to disable it by hand — the feed
    # reads null as absent and runs the identity map. The learner used to
    # meet that with a TypeError.
    for label, value in (("null", None), ("a number", 85.0),
                         ("a string", "off"), ("a list", [16.5, 85.0])):
        json.dump({"throttle": value}, open(target, "w"))
        rc, out = learn_write(target)
        ok(f'--write survives a "throttle" key holding {label}',
           "Traceback" not in out, out[-300:])
    # null means start clean and succeed; the wrong-shape ones refuse by name.
    json.dump({"throttle": None}, open(target, "w"))
    rc, out = learn_write(target)
    ok('--write treats "throttle": null as absent and writes', rc == 0
       and json.load(open(target))["throttle"]["floor_pct"] == 16.5, out[-300:])

    # Encoding: the feed reads this file as strict UTF-8, so the learner has
    # to write it that way on every platform, and say so when it cannot read it.
    json.dump({"throttle": {"_notes": "coast — foot fully off"}},
              open(target, "w", encoding="utf-8"), ensure_ascii=False)
    rc, out = learn_write(target)
    ok("--write round-trips non-ASCII provenance as UTF-8", rc == 0
       and "coast — foot fully off"
       in open(target, encoding="utf-8").read(), out[-300:])

    with open(target, "wb") as f:
        f.write('{"throttle": {"_notes": "café"}}'.encode("utf-16"))
    rc, out = learn_write(target)
    ok("--write refuses a non-UTF-8 calibration by name, not by traceback",
       rc == 1 and "Traceback" not in out and "UTF-8" in out, out[-300:])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
