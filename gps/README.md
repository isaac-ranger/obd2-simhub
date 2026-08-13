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

## Finding the port (macOS)

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

Baud never comes up: a Bluetooth serial port is a virtual tty and ignores
it completely. This is the only mercy this stack has shown us so far.

## Running a capture

```
python3 gps/gps_capture.py /dev/cu.XGPS160-XXXXXX 60
```

No pyserial, no dependencies — plain python3 as it ships on macOS. Second
argument is seconds (default 60). Ctrl-C ends a capture early and keeps
everything framed so far; a short capture is still a good capture.

Output lands in `xgps160-capture.txt` in the directory you ran from: one
sentence per line, each prefixed with seconds-since-start to the
millisecond and a tab. That first column is the entire point of the tool —
it's what turns "supports ~10Hz" from a spec-sheet claim into a measurement.

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
for once it won't be user error — there's an entire section above about
which door was the wrong one.

## Tests

```
python3 gps/test_gps_capture.py
```

Canned byte streams only — no device needed. The suite defends the framing
(chunks split mid-sentence, `\r\n` vs `\n`, the deadline, EOF, Ctrl-C),
which is the only promise the capture tool makes.
