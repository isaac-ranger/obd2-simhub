"""Framing tests for gps_capture. Run: python gps/test_gps_capture.py

Everything here runs against canned byte streams — no device, no port, no
GPS. The sentences are NMEA-shaped for flavor, but nothing asserts on their
meaning: this suite defends the framing (chunk boundaries, \\r\\n vs \\n,
the deadline, EOF, Ctrl-C), because framing is the only promise the capture
tool makes. What the sentences say is exactly the thing we don't know yet.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gps_capture import LineFramer, run_capture, parse_args

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILED.append(name)


class FakeClock:
    """A clock that moves only when the test says so."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Script:
    """A scripted byte source: each read() hands out the next canned chunk.

    An exhausted script returns b"" forever — a blocking fd's way of saying
    the far end hung up. A KeyboardInterrupt entry raises instead, which is
    where a real Ctrl-C lands: inside the read.
    """

    def __init__(self, chunks, clock=None, step=0.0):
        self.chunks = list(chunks)
        self.clock = clock
        self.step = step

    def read(self, n=256):
        if self.clock is not None:
            self.clock.t += self.step
        if not self.chunks:
            return b""
        c = self.chunks.pop(0)
        if c is KeyboardInterrupt:
            raise KeyboardInterrupt
        return c


class Out(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


def capture(chunks, secs=60.0, step=0.1):
    clock = FakeClock()
    out = Out()
    src = Script(chunks, clock=clock, step=step)
    n = run_capture(src, out, secs, clock=clock)
    return n, out, src


def lines_of(out):
    return out.getvalue().splitlines()


# --- LineFramer: chunk boundaries are nobody's friend ------------------------

f = LineFramer()
got = f.feed(b"$GPGGA,120000,3251.9,N,11710.2,W,1,08,1.1,52.0,M,,,*47\r\n"
             b"$GPRMC,120000,A,3251.9,N,11710.2,W,34.2,81.0,130826,,*1F\r\n")
ok("framer: two clean CRLF sentences in one chunk",
   got == ["$GPGGA,120000,3251.9,N,11710.2,W,1,08,1.1,52.0,M,,,*47",
           "$GPRMC,120000,A,3251.9,N,11710.2,W,34.2,81.0,130826,,*1F"],
   f"{got!r}")

f = LineFramer()
first = f.feed(b"$GPGSV,3,1,11,05,64,")
ok("framer: a split mid-sentence emits nothing early", first == [], f"{first!r}")
second = f.feed(b"050,41,12,30,110,38*7C\r\n")
ok("framer: the two halves come back as one sentence",
   second == ["$GPGSV,3,1,11,05,64,050,41,12,30,110,38*7C"], f"{second!r}")

f = LineFramer()
got = f.feed(b"$GPGGA,one*11\r\n$GPRMC,two*22\n$GPGSV,three*33\r\n")
ok("framer: CRLF and bare LF frame identically in one stream",
   got == ["$GPGGA,one*11", "$GPRMC,two*22", "$GPGSV,three*33"], f"{got!r}")

f = LineFramer()
got = []
for i in range(len(b"$GPVTG,81.0,T*55\r\n")):
    got += f.feed(b"$GPVTG,81.0,T*55\r\n"[i:i + 1])
ok("framer: byte-at-a-time chunking still frames one sentence",
   got == ["$GPVTG,81.0,T*55"], f"{got!r}")

f = LineFramer()
f.feed(b"$GPGGA,tail-no-newline")
ok("framer: an unterminated tail stays buffered, not emitted",
   f.feed(b"") == [] and f.buf == b"$GPGGA,tail-no-newline", f"{f.buf!r}")

f = LineFramer()
got = f.feed(b"$PXGPS,\xff\xfebinary?\r\n")
ok("framer: an undecodable byte becomes U+FFFD, not a crash",
   got == ["$PXGPS,��binary?"], f"{got!r}")

# --- run_capture: the loop around the framer ---------------------------------

n, out, _ = capture([b"$GPGGA,a*01\r\n$GPRMC,b*02\r\n", b"$GPGSV,c*03\r\n"])
ok("capture: line count returned matches lines written",
   n == 3 and len(lines_of(out)) == 3, f"n={n} out={out.getvalue()!r}")

stamps = [ln.split("\t")[0] for ln in lines_of(out)]
ok("capture: every line is stamped %.3f then a tab",
   all(len(s.split(".")[1]) == 3 and float(s) >= 0 for s in stamps),
   f"{stamps}")

body = [ln.split("\t", 1)[1] for ln in lines_of(out)]
ok("capture: sentences survive the trip intact",
   body == ["$GPGGA,a*01", "$GPRMC,b*02", "$GPGSV,c*03"], f"{body}")

ok("capture: flushed after every drain, not once at the end",
   out.flushes >= 2, f"flushes={out.flushes}")

n, out, src = capture([b"$GPGGA,%d*00\r\n" % i for i in range(100)],
                      secs=1.0, step=0.3)
ok("capture: the deadline stops the loop with chunks still unread",
   len(src.chunks) > 0 and 0 < n < 100, f"n={n} left={len(src.chunks)}")

n, out, src = capture([b"$GPGGA,only*00\r\n"], secs=60.0, step=0.1)
ok("capture: EOF breaks the loop instead of spinning on empty reads",
   n == 1 and src.chunks == [], f"n={n}")

n, out, _ = capture([b"$GPGGA,before*00\r\n", KeyboardInterrupt,
                     b"$GPGGA,never*00\r\n"])
ok("capture: Ctrl-C keeps everything framed so far",
   n == 1 and lines_of(out)[0].endswith("$GPGGA,before*00"),
   f"n={n} out={out.getvalue()!r}")

n, out, _ = capture([b"$GPGGA,half", KeyboardInterrupt])
ok("capture: Ctrl-C mid-sentence drops only the unterminated tail",
   n == 0 and out.getvalue() == "", f"out={out.getvalue()!r}")

# --- parse_args: the promised defaults ---------------------------------------

port, secs = parse_args([])
ok("args: no arguments means the cu-port hint and 60 seconds",
   port == "/dev/cu.XGPS160-XXXXXX" and secs == 60.0, f"{port} {secs}")

port, secs = parse_args(["/dev/cu.XGPS160-A1B2C3", "15"])
ok("args: port and seconds both land, seconds as float",
   port == "/dev/cu.XGPS160-A1B2C3" and secs == 15.0, f"{port} {secs}")

# ---------------------------------------------------------------------------------

print()
print(f"{'ALL PASS' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}  "
      f"({len(FAILED)} failed)")
sys.exit(1 if FAILED else 0)
