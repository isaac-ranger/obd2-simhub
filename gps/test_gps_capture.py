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
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gps_capture import (LineFramer, run_capture, parse_args, is_com_port,
                         open_source)

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
        self.reads = 0

    def read(self, n=256):
        self.reads += 1
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


def capture(chunks, secs=60.0, step=0.1, empty_is_eof=True):
    clock = FakeClock()
    out = Out()
    src = Script(chunks, clock=clock, step=step)
    n = run_capture(src, out, secs, clock=clock, empty_is_eof=empty_is_eof)
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

# The read count is the discriminator here, not the line count: a loop that
# spins on empty reads until the deadline still writes n==1 and drains the
# script, so those assertions alone pass with the exact defect this test
# exists to catch (ship-qa proved it by mutation). break = exactly 2 reads;
# spin = hundreds.
n, out, src = capture([b"$GPGGA,only*00\r\n"], secs=60.0, step=0.1)
ok("capture: EOF breaks the loop instead of spinning on empty reads",
   n == 1 and src.chunks == [] and src.reads == 2,
   f"n={n} reads={src.reads}")

n, out, _ = capture([b"$GPGGA,before*00\r\n", KeyboardInterrupt,
                     b"$GPGGA,never*00\r\n"])
ok("capture: Ctrl-C keeps everything framed so far",
   n == 1 and lines_of(out)[0].endswith("$GPGGA,before*00"),
   f"n={n} out={out.getvalue()!r}")

n, out, _ = capture([b"$GPGGA,half", KeyboardInterrupt])
ok("capture: Ctrl-C mid-sentence drops only the unterminated tail",
   n == 0 and out.getvalue() == "", f"out={out.getvalue()!r}")

# --- the Windows door: an empty read is a timeout, not a goodbye -------------

# On a timeout'd serial port, b"" means "quiet quarter-second". Data arriving
# AFTER a quiet spell must still land — a loop that treats the first empty
# read as EOF drops everything past the gap and n==1 gives it away.
n, out, src = capture([b"$GPGGA,a*01\r\n", b"", b"", b"$GPGGA,b*02\r\n"],
                      secs=2.0, empty_is_eof=False)
ok("capture: a quiet spell on a timeout'd port is not EOF",
   n == 2 and lines_of(out)[1].endswith("$GPGGA,b*02"),
   f"n={n} out={out.getvalue()!r}")

# A port that never speaks again is ended by the DEADLINE on this door. The
# read count is again the discriminator: breaking on the first empty read
# also returns n==1, but it stops at 2 reads — listening to the deadline
# takes ten.
n, out, src = capture([b"$GPGGA,only*00\r\n"], secs=1.0, step=0.1,
                      empty_is_eof=False)
ok("capture: the deadline ends a silent timeout'd port",
   n == 1 and src.reads >= 5, f"n={n} reads={src.reads}")

# --- is_com_port: which door does a name open? -------------------------------

ok("doors: COM5 and com12 read as Windows serial",
   is_com_port("COM5") and is_com_port("com12"), "")
ok("doors: the \\\\.\\COM10 spelling counts too",
   is_com_port(r"\\.\COM10"), "")
ok("doors: /dev/cu.* and /dev/ttyUSB0 stay on the file side",
   not is_com_port("/dev/cu.XGPS160-A1B2C3") and not is_com_port("/dev/ttyUSB0"),
   "")
ok("doors: COMMON is a word, not a port",
   not is_com_port("COMMON") and not is_com_port("COM"), "")
ok("doors: the name ends at the digits — COM5x and COM5: are not ports",
   not is_com_port("COM5x") and not is_com_port("COM5:"), "")

# --- open_source: each door returns the flag it means ------------------------

# The wiring, not the classifier: ship-qa proved by mutation that a COM door
# returning empty_is_eof=True — every quiet quarter-second becoming EOF —
# passed the whole suite. So each door is opened for real and its flag
# asserted.

with tempfile.NamedTemporaryFile(delete=False) as tf:
    tf.write(b"$GPGGA,x*00\r\n")
    tmp_name = tf.name
src, flag = open_source(tmp_name)
ok("open_source: a path opens the file door, where empty means hangup",
   flag is True and src.read(4) == b"$GPG", f"flag={flag}")
src.close()
os.unlink(tmp_name)

# The COM door is witnessed through a stub serial module planted in
# sys.modules, so these assertions hold with or without pyserial installed:
# the flag, the polite baud, and the load-bearing timeout.


class _StubSerial:
    def __init__(self, port, baudrate, timeout=None):
        self.opened = (port, baudrate, timeout)


_stub = types.ModuleType("serial")
_stub.Serial = _StubSerial
_stub.SerialException = type("SerialException", (Exception,), {})
sys.modules["serial"] = _stub

src, flag = open_source("COM7")
ok("open_source: a COM name opens the serial door, where empty means quiet",
   flag is False and isinstance(src, _StubSerial), f"flag={flag} src={src!r}")
ok("open_source: baud 115200 and the load-bearing 0.25s timeout are passed",
   src.opened == ("COM7", 115200, 0.25), f"{src.opened}")

try:
    open_source("/no/such/port/anywhere")
    died = False
except SystemExit:
    died = True
ok("open_source: a bad path dies with a message, not a traceback", died, "")

# --- parse_args: the promised defaults ---------------------------------------

# Platform is injected so this suite gives the same verdicts on the car's
# Windows side as it does here: each default is asserted from both worlds.

port, secs = parse_args([], platform="darwin")
ok("args: no arguments on macOS means the cu-port hint and 60 seconds",
   port == "/dev/cu.XGPS160-XXXXXX" and secs == 60.0, f"{port} {secs}")

port, secs = parse_args([], platform="win32")
ok("args: no arguments on Windows means COM5 — the default the email promised",
   port == "COM5" and secs == 60.0, f"{port} {secs}")

port, secs = parse_args(["/dev/cu.XGPS160-A1B2C3", "15"])
ok("args: port and seconds both land, seconds as float",
   port == "/dev/cu.XGPS160-A1B2C3" and secs == 15.0, f"{port} {secs}")

port, secs = parse_args(["/dev/cu.XGPS160-A1B2C3"], platform="win32")
ok("args: a named port is taken at face value, whatever the platform",
   port == "/dev/cu.XGPS160-A1B2C3", f"{port}")

# ---------------------------------------------------------------------------------

print()
print(f"{'ALL PASS' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}  "
      f"({len(FAILED)} failed)")
sys.exit(1 if FAILED else 0)
