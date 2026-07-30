"""Adversarial fixture tests for parse_mode01. Run: python probe/test_parse.py"""
import sys
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from obd_probe import parse_mode01, ElmError, DASH_PIDS

FAILED = []

def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)

def expect(name, lines, want_pids=None, want_resp=None, want_bytes=None):
    try:
        res, resp = parse_mode01(lines)
    except ElmError as e:
        ok(name, False, f"unexpected ElmError: {e}  input={lines!r}")
        return
    except Exception as e:
        ok(name, False, f"CRASH {type(e).__name__}: {e}  input={lines!r}")
        return
    good = True
    detail = f"got pids={{{', '.join(f'{p:02X}:{b.hex()}' for p,b in sorted(res.items()))}}} resp={resp}  input={lines!r}"
    if want_pids is not None and sorted(res) != sorted(want_pids):
        good = False
    if want_resp is not None and resp != want_resp:
        good = False
    if want_bytes:
        for p, b in want_bytes.items():
            if res.get(p) != b:
                good = False
    ok(name, good, detail)

def expect_err(name, lines):
    try:
        res, resp = parse_mode01(lines)
        ok(name, False, f"no error raised; got {res} input={lines!r}")
    except ElmError:
        ok(name, True)
    except Exception as e:
        ok(name, False, f"CRASH (non-ElmError) {type(e).__name__}: {e}  input={lines!r}")

# --- basics ---
expect("single frame plain", ["410C1AF8"], [0x0C], 1, {0x0C: b"\x1a\xf8"})
expect("single frame spaced", ["41 0C 1A F8"], [0x0C], 1, {0x0C: b"\x1a\xf8"})
expect("lowercase hex", ["41 0c 1a f8"], [0x0C], 1, {0x0C: b"\x1a\xf8"})
expect("batched 3 PIDs one frame", ["41 0C 1A F8 0D 32 05 5A"],
       [0x0C, 0x0D, 0x05], 1, {0x0D: b"\x32", 0x05: b"\x5a"})

# --- multi-frame ISO-TP ---
mf = ["00B", "0:4100BE3FA813", "1:2090 07B015AAAA"]
expect("multi-frame w/ length trim (padding AA must go)", mf,
       [0x00, 0x20], 1, {0x00: b"\xbe\x3f\xa8\x13", 0x20: b"\x90\x07\xb0\x15"})
expect("multi-frame lines out of order", ["00B", "1:209007B015AAAA", "0:4100BE3FA813"],
       [0x00, 0x20], 1, {0x20: b"\x90\x07\xb0\x15"})
expect("multi-frame missing length header (padding walked safely)",
       ["0:4100BE3FA813", "1:209007B015AAAA"], [0x00, 0x20], 1)
# realistic 6-PID batch response, 15 bytes over 3 frames
mf6 = ["00F", "0: 41 0C 1A F8 0D 32", "1: 04 57 11 44 42 39", "2: 10 0F 3C AA AA AA"]
expect("multi-frame 6-PID batch", mf6, [0x0C, 0x0D, 0x04, 0x11, 0x42, 0x0F], 1,
       {0x0C: b"\x1a\xf8", 0x42: b"\x39\x10", 0x0F: b"\x3c"})
# hex frame indexes beyond 9, deliberately shuffled
big = ["041", "3:0D0E0F101112", "1:0102030405 06", "0:4100AABBCCDD",
       "2:0708090A0B0C", "A:2122232425 26", "4:131415161718", "5:191A1B1C1D1E",
       "6:1F2021222324", "7:25262728292A", "8:2B2C2D2E2F30", "9:3132333435 36"]
try:
    res, resp = parse_mode01(big)
    ok("hex frame index A sorts after 9", res.get(0x00) == b"\xaa\xbb\xcc\xdd" and resp == 1)
except Exception as e:
    ok("hex frame index A sorts after 9", False, f"{type(e).__name__}: {e}")

# --- multi-ECU ---
expect("multi-ECU same PID first wins", ["41055A", "410546"], [0x05], 2, {0x05: b"\x5a"})
expect("multi-ECU mixed spacing/case", ["41 0c 1a f8", "410C1B00"], [0x0C], 2, {0x0C: b"\x1a\xf8"})

# --- error vocabulary / negatives ---
expect_err("NO DATA", ["NO DATA"])
expect_err("STOPPED", ["STOPPED"])
expect_err("CAN ERROR", ["CAN ERROR"])
expect_err("UNABLE TO CONNECT", ["UNABLE TO CONNECT"])
expect_err("BUFFER FULL", ["BUFFER FULL"])
expect_err("question mark", ["?"])
expect_err("negative response alone 7F0112", ["7F 01 12"])
expect("negative + positive mixed", ["7F0131", "410C1AF8"], [0x0C], 1)
expect_err("only chatter", ["ELM327 v1.5", "OK"])
expect_err("<DATA ERROR variant", ["<DATA ERROR"])

# --- malformed / truncation ---
expect_err("truncated payload 410C1A (0C needs 2 bytes)", ["410C1A"])
expect_err("odd-length hex 410C1AF", ["410C1AF"])
expect_err("bare 41", ["41"])
expect_err("empty input", [])
expect_err("empty + blank lines only", ["", "   "])
expect("blank lines around payload", ["", "410C1AF8", "  "], [0x0C], 1)
expect("SEARCHING passed through raw", ["SEARCHING...", "410C1AF8"], [0x0C], 1)
expect("BUS INIT: OK passed raw", ["BUS INIT: OK", "410D32"], [0x0D], 1)
expect_err("headers-on 11-bit line (odd length, must not misparse)", ["7E80641 0C1AF8"])
expect_err("headers-on 29-bit line (even length, wrong first byte)", ["18DAF11006410C1AF8"])
expect_err("mode-03 response to mode-01 parse", ["43 01 33 00 00 00 00"])
expect("command echo line ignored", ["010C", "410C1AF8"], [0x0C], 1)
expect("trailing garbage byte after known PID", ["410C1AF8FF"], [0x0C], 1)
expect_err("unknown-length PID only (41AB12)", ["41AB12"])
expect("length line with no mf parts", ["00B", "410C1AF8"], [0x0C], 1)
expect_err("orphan continuation line only", ["1:209007B015AAAA"])
expect("empty continuation part", ["0:", "410C1AF8"], [0x0C], 1)

# --- decoder formula spot checks (SAE J1979) ---
checks = [
    (0x0C, b"\x1a\xf8", 1726.0),      # RPM (26*256+248)/4
    (0x0D, b"\x32", 50.0),            # speed km/h
    (0x11, b"\xff", 100.0),           # throttle 100%
    (0x04, b"\x00", 0.0),             # load 0%
    (0x05, b"\x28", 0.0),             # coolant 40-40
    (0x10, b"\x01\x00", 2.56),        # MAF 256/100
    (0x42, b"\x39\x10", 14.608),      # 14608 mV
    (0x0E, b"\x80", 0.0),             # timing 128/2-64
    (0x5E, b"\x00\x14", 1.0),         # fuel rate 20/20
    (0x2F, b"\xff", 100.0),           # fuel level
]
for pid, data, want in checks:
    name, unit, fn = DASH_PIDS[pid]
    got = fn(data)
    ok(f"decoder {pid:02X} {name}", abs(got - want) < 1e-9, f"got {got}, want {want}")

print()
print(f"{'ALL PASS' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}  "
      f"({len(FAILED)} failed)")
sys.exit(1 if FAILED else 0)
