#!/usr/bin/env python3
"""
gps_capture.py — raw XGPS160 capture: sixty seconds of dumb bytes, timestamped
===============================================================================

Reads whatever the XGPS160 says over its Bluetooth serial port and writes it
to xgps160-capture.txt, one line per sentence, each stamped with seconds
since start. That's the whole program, and the restraint is the point: we
have not seen one byte out of this device yet, and anything that interprets
sentences before a real capture comes back is a guess wearing code. The
parser earns its place in this directory only after this tool tells us what
there is to parse.

One tool, two doors — and unlike the last pair of doors this file wrote
about, both of these work. A port named like COMn goes through pyserial;
anything else is opened as a plain file. The output format is the same
either way — seconds, a tab, the sentence — so the future parser never
has to care which door was used.

Usage (Windows — the side of the Bootcamp fence the car actually boots):

  py -m pip install pyserial
  py gps\\gps_capture.py COM5 60

The COM number lives in Bluetooth settings -> More Bluetooth options ->
COM Ports tab; the XGPS160 usually claims TWO ports, and the OUTGOING one
is the one that talks. Device Manager -> Ports (COM & LPT) shows the same
list, in the section named for the printer plug. gps/README.md has the
full tour.

Usage (macOS — the fence's other side, kept because the machine can boot it):

  ls /dev/cu.*
  python3 gps/gps_capture.py /dev/cu.XGPS160-XXXXXX 60

Use /dev/cu.*, NEVER /dev/tty.* — same device, two doors, and the tty door
blocks forever on a carrier-detect question the GPS will never answer. (The
gory details are in gps/README.md, written down so nobody rediscovers this
in a parking lot.)

The first column is seconds since start, to three decimals. That column is
the entire reason this file exists: it will tell us whether the advertised
10Hz applies to every sentence or only to position, and no spec sheet will.
"""

import re
import sys
import time

OUT_NAME = "xgps160-capture.txt"

# COM5, com12, or the \\.\COM10 spelling that Windows tooling hands out.
_COM_NAME = re.compile(r"^(\\\\\.\\)?COM\d+$", re.IGNORECASE)


def is_com_port(port):
    """True when the port NAME says Windows serial.

    Deliberately a test of the name, not of sys.platform: the car's MacBook
    boots either OS, and what the user typed is the only statement of which
    world they're standing in right now.
    """
    return _COM_NAME.match(port) is not None


class LineFramer:
    """Reassembles a byte stream into text lines, whatever the chunking.

    A serial read boundary lands wherever it pleases — mid-sentence, mid-
    checksum, between the \\r and the \\n — so the framer owns the only
    buffer and hands back complete lines only. Decoding is ascii-with-
    replacement because the capture must survive whatever the device
    actually says, printable or not; a byte we can't decode becomes U+FFFD
    and stays in the record instead of killing the run.
    """

    def __init__(self):
        self.buf = b""

    def feed(self, chunk):
        """Absorb one chunk of bytes; return the list of completed lines.

        Lines are split on \\n; a trailing \\r is stripped, so \\r\\n and
        bare \\n both frame cleanly. Whatever follows the last \\n stays
        buffered for the next feed.
        """
        self.buf += chunk
        lines = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            lines.append(raw.decode("ascii", "replace").rstrip("\r"))
        return lines


def run_capture(src, out, secs, clock=time.time, empty_is_eof=True):
    """Pump src (binary file-like) into out (text file-like) for secs seconds.

    Returns the number of lines written. Each line is '%.3f\\t%s\\n' —
    seconds since start, a tab, the sentence. Flushes after every drain so
    an interrupted capture keeps everything framed so far. Ctrl-C is a
    supported way to end a capture, not an error.

    empty_is_eof is the one honest difference between the two doors. On a
    blocking fd an empty read means the device hung up, and looping on it
    would spin a CPU core in a parking lot — so we break. On a timeout'd
    serial port an empty read means a quiet quarter-second, which is not a
    goodbye — so we keep listening until the deadline.
    """
    framer = LineFramer()
    t0 = clock()
    n = 0
    try:
        while clock() - t0 < secs:
            chunk = src.read(256)
            if not chunk:
                if empty_is_eof:
                    break
                continue
            for line in framer.feed(chunk):
                out.write("%.3f\t%s\n" % (clock() - t0, line))
                n += 1
            out.flush()
    except KeyboardInterrupt:
        pass
    return n


def open_sink(path):
    """Text file the capture writes to.

    UTF-8, not the locale encoding: LineFramer turns undecodable bytes into
    U+FFFD so they stay in the record, and cp1252 (Windows' usual default
    for open(..., "w")) refuses that character — which is how a binary
    preamble at the start of a GPS feed became a crash instead of a line.
    """
    return open(path, "w", encoding="utf-8")


def open_source(port):
    """Open the right door for the port name; returns (source, empty_is_eof).

    A COM name goes through pyserial. Baud is still decoration on a
    Bluetooth virtual port, but pyserial insists on being told a number, so
    it gets a polite one. The timeout is load-bearing, not decoration: it
    is what keeps Ctrl-C answerable on Windows and turns a dead port into
    an empty capture instead of a hung one. The import lives down here so
    the macOS lane keeps its nothing-to-install promise.

    Anything else is a plain blocking open() — on macOS a paired Bluetooth
    serial port is a virtual tty, and that call is the entire I/O stack.
    """
    if is_com_port(port):
        try:
            import serial
        except ImportError:
            sys.exit("COM ports need pyserial: py -m pip install pyserial")
        try:
            return serial.Serial(port, 115200, timeout=0.25), False
        except serial.SerialException as e:
            sys.exit("could not open %s: %s\n"
                     "(right COM number? the OUTGOING one? "
                     "gps/README.md has the tour)" % (port, e))
    try:
        return open(port, "rb", buffering=0), True
    except OSError as e:
        sys.exit("could not open %s: %s\n"
                 "(on macOS: ls /dev/cu.* and take the cu door — "
                 "gps/README.md)" % (port, e))


def parse_args(argv, platform=sys.platform):
    """(port, secs) from argv, defaulting to the door the platform suggests.

    Only the DEFAULT consults the platform; a port you name is taken at
    face value on any OS. COM5 is the default the email promised; the cu
    hint is a pattern to fill in, not a port that exists.
    """
    if len(argv) > 0:
        port = argv[0]
    elif platform.startswith("win"):
        port = "COM5"
    else:
        port = "/dev/cu.XGPS160-XXXXXX"
    secs = float(argv[1]) if len(argv) > 1 else 60.0
    return port, secs


def main(argv=None):
    port, secs = parse_args(sys.argv[1:] if argv is None else argv)
    src, empty_is_eof = open_source(port)
    with src, open_sink(OUT_NAME) as out:
        n = run_capture(src, out, secs, empty_is_eof=empty_is_eof)
    print("wrote %s (%d lines)" % (OUT_NAME, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
