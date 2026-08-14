# gps/ — the XGPS160 leg, currently a listening post

There is exactly one tool in this directory and it deliberately understands
nothing. `gps_capture.py` writes down whatever the XGPS160 says, timestamped,
and draws no conclusions. We have not seen one byte out of this device yet,
and a parser written before the first capture comes back is a guess wearing
code — so the parser isn't here. It moves in once the captures tell us what
it has to parse: which sentences, at what real rate, and whether the
advertised 10Hz is a promise to every sentence or only to position.

This makes NMEA the third occupant of the trench coat: sentences designed
for boat autopilots, riding alongside ELM327 AT commands, both older than
the laptop carrying them, driving a 2025 Porsche dashboard. The rig remains
at peace with itself.

## A correction, from the author, about the author

The previous edition of this README described the wrong operating system
with perfect fidelity. The MacBook in the car is a Bootcamp machine and it
boots **Windows 10** — it has to; SimHub is Windows-only, a fact this
repo's own correspondence stated plainly before the README forgot it one
email later. Author error, not user error. The macOS material below
survives — the machine can boot either side of the fence, which is why it
was chosen — but it is now the secondary lane, and Windows leads, the way
reality insisted.

The tool itself no longer cares. One tool, two doors: a port named `COMn`
goes through pyserial, a `/dev/cu.*` path is a plain file open, and the
output is identical either way, so the future parser will never learn
which OS its bytes came in through.

## Windows — the real deployment

Install the one dependency, if the MZX+ work didn't already:

```
py -m pip install pyserial
```

Find the port: **Bluetooth settings -> More Bluetooth options -> COM Ports
tab**. The XGPS160 pairs over SPP and usually claims TWO ports; use the
**outgoing** one — that's the line the GPS actually answers. Device
Manager -> Ports (COM & LPT) shows the same list, in a section still named
half for printer plugs.

Then:

```
py gps\gps_capture.py COM5 60
```

Second argument is seconds (default 60). Baud never stopped being
decoration — a Bluetooth virtual port ignores it — but pyserial insists on
being told a number, so the tool says 115200 on your behalf, which is a
polite one.

The 0.25s read timeout is load-bearing, not decoration: it keeps Ctrl-C
responsive on Windows, and it means a port that opens but never speaks
produces an **empty capture file at the deadline** instead of a hang. An
empty file is itself a finding, and its usual meaning is "wrong half of
the COM pair" — go back to the COM Ports tab and try the other number.

Ctrl-C ends a capture early on either platform and keeps everything framed
so far; a short capture is still a good capture.

## macOS — the secondary lane

Pair the XGPS160 in Bluetooth settings, then:

```
ls /dev/cu.*
```

You're looking for something like `/dev/cu.XGPS160-A1B2C3`.

**Use `/dev/cu.*`, never `/dev/tty.*`.** Same device, two doors. The tty
door blocks waiting on carrier detect — a modem-etiquette question the GPS
will never answer — so opening it means sitting at a frozen cursor slowly
concluding the receiver is dead. It isn't. It's the door. The cu ("call-up")
door skips the question and just talks.

```
python3 gps/gps_capture.py /dev/cu.XGPS160-XXXXXX 60
```

No pyserial on this side — a paired Bluetooth serial port on macOS is a
virtual tty, and a plain `open()` is the entire I/O stack.

One honesty note, and it differs by door: on a plain file open the
deadline only ticks between reads, so a port that opens but never says
anything sits blocked in the first read — past any deadline — until you
Ctrl-C it. A capture that refuses to end on its own is this lane's version
of the empty file: the device paired, but it isn't talking.

## What to send back

**Two captures, not one:**

1. **Parked** — driveway, engine off, 60 seconds. This gives us the honest
   sentence mix and the true per-sentence rates.
2. **Moving** — a slow loop around the block is plenty. This gives us the
   honest *accuracy*, because the parked capture will lie about it:
   consumer chipsets do static-hold filtering that freezes the fix while
   you're stopped, so a parked receiver impersonates a survey instrument.
   The same box at speed looks like what it actually is, and that's the
   number we have to design around.

Send back both `xgps160-capture.txt` files (rename them so they don't
clobber each other — `parked.txt` and `moving.txt` works). If a capture
comes out empty or strange, send it anyway: a weird capture is data, and
for once it won't be user error — this README now has entire sections on
which door was wrong and which operating system its own author thought
you had.

## Tests

```
py gps\test_gps_capture.py        (Windows)
python3 gps/test_gps_capture.py   (macOS / anywhere)
```

Canned byte streams only — no device, no COM port, no pyserial needed. The
suite defends the framing (chunks split mid-sentence, `\r\n` vs `\n`, the
deadline, EOF, Ctrl-C), which is the only promise the capture tool makes —
plus the two-door dispatch, including the rule that an empty read on a
timeout'd port is a quiet quarter-second and not a goodbye.
