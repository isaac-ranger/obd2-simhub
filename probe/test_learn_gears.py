"""Fixture tests for learn_gears clustering. Run: python probe/test_learn_gears.py
No hardware, no files needed beyond this script — drives synthetic logs through
the same functions the CLI uses."""
import sys
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from learn_gears import steady_ratios, cluster, load_samples, MIN_CLUSTER

FAILED = []

def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


def synth(ratio, n, v0=20.0, t0=0.0):
    """n steady samples holding one gear: speed climbs gently, rpm follows."""
    out = []
    for i in range(n):
        v = v0 + i * 0.5
        out.append((t0 + i * 0.2, ratio * v, v))
    return out


# --- clean 6-speed sweep recovers six gears, ordered 1st-first -------------
# Each gear is driven where a real car would drive it: speed chosen so the
# engine sits near 2000 rpm, not the fixture's default 20 km/h (which would
# put 6th gear under the MIN_RPM idle floor and silently thin the clusters).
SIX = [103.0, 60.4, 43.3, 35.0, 29.2, 25.2]
log = []
for g in SIX:
    log += synth(g, 20, v0=2000.0 / g, t0=len(log) * 0.2)
gears, _ = cluster(steady_ratios(log))
ok("six clean gears found", len(gears) == 6, f"got {len(gears)}")
ok("ordered highest-first (1st gear first)",
   [c["rpm_per_kmh"] for c in gears] == sorted((c["rpm_per_kmh"] for c in gears), reverse=True))
ok("medians land on the planted ratios",
   all(abs(c["rpm_per_kmh"] - want) / want < 0.02 for c, want in zip(gears, SIX)),
   f"got {[c['rpm_per_kmh'] for c in gears]}")

# --- shift transients between gears drop out -------------------------------
log = synth(60.0, 20, v0=2000.0 / 60.0)
# a shift: ratio sweeping 60 -> 43 across a few samples (clutch out, revs falling)
for i, q in enumerate([57.0, 53.0, 49.0, 46.0]):
    v = 35.0 + i
    log.append((100.0 + i * 0.2, q * v, v))
log += synth(43.0, 20, v0=40.0, t0=110.0)
gears, _ = cluster(steady_ratios(log))
ok("shift transient yields two gears, not three", len(gears) == 2, f"got {len(gears)}")

# --- idle, Auto-Stop zeros, and crawl speeds are not data ------------------
log = [(0.0, 0.0, 0.0), (0.2, 0.0, 0.0),          # Auto-Stop: engine off
       (0.4, 800.0, 0.0), (0.6, 800.0, 0.0)]      # idling at a stop
log += [(1.0 + i * 0.2, 2000.0, 3.0) for i in range(10)]   # parking-lot crawl
ok("engine-off / idle / crawl produce no clusters",
   cluster(steady_ratios(log)) == ([], []))

# --- tiny clusters (debris) are dropped, and the rejection says why --------
# MIN_CLUSTER - 1 samples: enough density to be looked at, too few to count.
log = synth(60.0, 20, v0=2000.0 / 60.0) + synth(43.0, MIN_CLUSTER - 1,
                                                v0=2000.0 / 43.0, t0=50.0)
gears, rejected = cluster(steady_ratios(log))
ok("cluster below MIN_CLUSTER is debris", len(gears) == 1, f"got {len(gears)}")
ok("...and is rejected by name",
   len(rejected) == 1 and "samples" in rejected[0]["why"],
   f"got {rejected}")

# --- loader tolerates extra columns and junk rows --------------------------
import tempfile, os as _os
with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
    f.write("t_s,rpm,speed_kmh,throttle_pct\n")
    f.write("0.2,1000,20,14.9\n")
    f.write("bad,row,here,\n")
    f.write("0.3\n")                     # truncated row: DictReader hands the
    f.write("0.4,1010,20.2,15.0\n")      # missing cells over as None
    tmp = f.name
try:
    ok("loader keeps good rows, drops junk and truncated rows",
       len(load_samples(tmp)) == 2)
finally:
    _os.unlink(tmp)

# --- a UTF-16 log refuses by name, matching learn_throttle's reader --------
# Excel's "Unicode Text" save produces one; the strict utf-8 open makes it
# fail the same way on every platform (cp1252 would otherwise decode it into
# NUL-garbage and blame the columns), and the refusal says what to do.
with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as f:
    f.write("t_s,rpm,speed_kmh\n1,2000,50\n".encode("utf-16"))
    tmp = f.name
try:
    load_samples(tmp)
    ok("UTF-16 log refuses by name", False, "no SystemExit raised")
except SystemExit as e:
    ok("UTF-16 log refuses by name (re-save as CSV UTF-8)",
       "CSV UTF-8" in str(e.code), str(e.code)[:160])
finally:
    _os.unlink(tmp)

# ...and the named remedy must round-trip: Excel's "CSV UTF-8" carries a BOM,
# and utf-8-sig keeps it out of the t_s column name.
with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                 encoding="utf-8-sig") as f:
    f.write("t_s,rpm,speed_kmh\n0.2,1000,20\n")
    tmp = f.name
try:
    ok("BOM'd \"CSV UTF-8\" log loads — the remedy round-trips",
       len(load_samples(tmp)) == 1)
finally:
    _os.unlink(tmp)

# --- empty input is an empty answer, not a crash ---------------------------
ok("empty log clusters to nothing",
   cluster([]) == ([], []) and steady_ratios([]) == [])

# --- THE CHAINING DEFECT (2026-08-07, Kris's dense logs): debris between ---
# gears must not link everything into one cluster. Six held gears, plus a
# steady-passing debris pair every 2% across the whole ratio range — under
# sorted-neighbour gap splitting no consecutive pair ever jumps 8%, so this
# exact shape collapsed to 1 cluster at ±222%. Density bumps must not chain.
log = []
for g in SIX:
    log += synth(g, 20, v0=2000.0 / g, t0=len(log) * 0.2)
t, q = 1000.0, 24.0
while q < 110.0:
    v = 2500.0 / q                       # debris at driving rpm, so the idle
    log += [(t, q * v, v), (t + 0.2, q * v, v)]   # veto isn't what kills it
    t, q = t + 10.0, q * 1.02
gears, _ = cluster(steady_ratios(log))
ok("dense inter-gear debris does not chain six gears into one",
   len(gears) == 6, f"got {len(gears)}")
ok("...and the medians still land on the planted ratios",
   all(abs(c["rpm_per_kmh"] - want) / want < 0.02 for c, want in zip(gears, SIX)),
   f"got {[c['rpm_per_kmh'] for c in gears]}")
ok("...with single-digit spreads",
   all(c["spread_pct"] < 5.0 for c in gears),
   f"got {[c['spread_pct'] for c in gears]}")

# --- idle creep is not a gear, however long it holds -----------------------
# Rolling at ~1000 rpm in neutral: tight ratio, real hold, engine idling.
log = synth(60.0, 20, v0=2000.0 / 60.0) + synth(43.0, 20, v0=2000.0 / 43.0,
                                                t0=50.0)
log += [(100.0 + i * 0.2, 1001.0, 11.0) for i in range(12)]
gears, rejected = cluster(steady_ratios(log))
ok("idle-band creep is rejected as a gear", len(gears) == 2, f"got {len(gears)}")
ok("...naming the idle band",
   any("idle" in c["why"] for c in rejected), f"got {rejected}")

# --- a lump that is never HELD is not a gear -------------------------------
# Four 3-sample bursts at the same ratio, 10s apart: 12 tight samples at
# driving rpm that no one ever held for HOLD_S.
log = synth(60.0, 20, v0=2000.0 / 60.0) + synth(43.0, 20, v0=2000.0 / 43.0,
                                                t0=50.0)
for burst in range(4):
    t0 = 200.0 + burst * 10.0
    log += [(t0 + i * 0.2, 50.0 * 50.0, 50.0) for i in range(3)]
gears, rejected = cluster(steady_ratios(log))
ok("a never-held lump is rejected as a gear", len(gears) == 2, f"got {len(gears)}")
ok("...naming the missing hold",
   any("held" in c["why"] for c in rejected), f"got {rejected}")

# --- spread is a HALF-width: a band spanning 58..62 around 60 is ±3.3% ----
# The band is continuous (every bin populated): to a density clusterer,
# isolated spikes with empty bins between them are separate structures —
# merging anything within 8% regardless was the chaining defect.
from learn_gears import write_calibration
flat, _ = cluster([(i * 0.2, q, q * 30.0) for i, q in
                   enumerate(58.0 + 0.25 * j for j in range(17))])
ok("spread_pct is the half-width of the band",
   len(flat) == 1 and abs(flat[0]["spread_pct"] - 3.3) < 0.05,
   f"got {len(flat)} clusters, {flat and flat[0]['spread_pct']}")

# --- write_calibration: fresh file gets defaults, existing keys survive ----
import json
gears_fixture, _ = cluster(steady_ratios(synth(60.0, 20, v0=2000.0 / 60.0)))
with tempfile.TemporaryDirectory() as d:
    fresh = _os.path.join(d, "new.json")
    write_calibration(fresh, gears_fixture, "some/log.csv")
    c = json.load(open(fresh))
    ok("fresh file: tolerance defaults to 7", c["gears"]["tolerance_pct"] == 7)
    ok("fresh file: learned_on stamped", bool(c["gears"].get("learned_on")))

    seeded = _os.path.join(d, "seeded.json")
    json.dump({"active_set": "street_18", "gears": {"tolerance_pct": 5}}, open(seeded, "w"))
    write_calibration(seeded, gears_fixture, "some/log.csv")
    c = json.load(open(seeded))
    ok("existing file: unrelated keys preserved", c["active_set"] == "street_18")
    ok("existing file: an explicit tolerance is not overwritten",
       c["gears"]["tolerance_pct"] == 5)
    ok("existing file: constants and provenance updated",
       c["gears"]["rpm_per_kmh"] == [gears_fixture[0]["rpm_per_kmh"]]
       and c["gears"]["learned_from"] == "some/log.csv")

    bad = _os.path.join(d, "bad.json")
    open(bad, "w").write("{not json")
    try:
        write_calibration(bad, gears_fixture, "x.csv")
        ok("invalid-JSON target refuses", False, "no SystemExit raised")
    except SystemExit:
        ok("invalid-JSON target refuses and is not clobbered",
           open(bad).read() == "{not json")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
