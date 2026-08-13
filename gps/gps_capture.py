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

Usage (macOS — the side of the Bootcamp fence where the GPS pairs):

  ls /dev/cu.*
  python3 gps/gps_capture.py /dev/cu.XGPS160-XXXXXX 60

Use /dev/cu.*, NEVER /dev/tty.* — same device, two doors. The tty door
blocks waiting on carrier detect, and you will sit staring at a cursor
concluding the GPS is dead. It isn't. It's the door. (The gory details are
in gps/README.md, written down so nobody rediscovers this in a parking lot.)

No pyserial, no dependencies, nothing to install: a paired Bluetooth serial
port on macOS is a virtual tty, baud is meaningless, and a plain binary
open() is the entire I/O stack. Ctrl-C whenever — output is flushed as it
arrives, so a short capture is still a good capture.

The first column is seconds since start, to three decimals. That column is
the entire reason this file exists: it will tell us whether the advertised
10Hz applies to every sentence or only to position, and no spec sheet will.
"""

import sys
import time

OUT_NAME = "xgps160-capture.txt"


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


def run_capture(src, out, secs, clock=time.time):
    """Pump src (binary file-like) into out (text file-like) for secs seconds.

    Returns the number of lines written. Each line is '%.3f\\t%s\\n' —
    seconds since start, a tab, the sentence. Flushes after every drain so
    an interrupted capture keeps everything framed so far. Stops early on
    EOF (an empty read from a blocking fd means the device hung up — looping
    on it would spin a CPU core in a parking lot) and on Ctrl-C, which is a
    supported way to end a capture, not an error.
    """
    framer = LineFramer()
    t0 = clock()
    n = 0
    try:
        while clock() - t0 < secs:
            chunk = src.read(256)
            if not chunk:
                break
            for line in framer.feed(chunk):
                out.write("%.3f\t%s\n" % (clock() - t0, line))
                n += 1
            out.flush()
    except KeyboardInterrupt:
        pass
    return n


def parse_args(argv):
    """(port, secs) from argv, with the defaults the email promised."""
    port = argv[0] if len(argv) > 0 else "/dev/cu.XGPS160-XXXXXX"
    secs = float(argv[1]) if len(argv) > 1 else 60.0
    return port, secs


def main(argv=None):
    port, secs = parse_args(sys.argv[1:] if argv is None else argv)
    with open(port, "rb", buffering=0) as f, open(OUT_NAME, "w") as out:
        n = run_capture(f, out, secs)
    print("wrote %s (%d lines)" % (OUT_NAME, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
