# gps/ — the XGPS160 leg: a listening post that finally heard something

There is a listening post in this directory and it deliberately understands
nothing. `gps_capture.py` writes down whatever the XGPS160 says, timestamped,
and draws no conclusions. For weeks that restraint guarded an empty
notebook; then four real captures came back — a driveway control and two
drives — and the questions the timestamp column was built to answer got
answered with numbers instead of a spec sheet:

* The advertised 10 Hz belongs to **position only**. GGA and RMC arrive at
  9.5–9.8 Hz; the satellite gossip (GSV/GSA) and the proprietary `$GPPWR`
  ride along at 1–3 Hz.
* Every one of 26,104 sentences carried a checksum, and zero failed. Over
  Bluetooth SPP. The boat-autopilot people built well.
* The device opens every connection with **three binary status packets**
  (sync byte 0x55) glued to the front of the first NMEA sentences — see
  the framer note below — and then never speaks binary again.
* A parked XGPS160 pins its reported position: one unique coordinate
  across an entire stationary capture. The wander everyone smooths
  against is suppressed at zero speed by the receiver itself; it only
  scribbles once you crawl.

So the parser earned its way in: `nmea.py` (RMC + GGA, checksum-verified,
resyncs past the binary preamble). And with real bytes on file, the health
checker `gps_verify.py` below is how any future capture proves it is what
it claims to be.

This makes NMEA the third occupant of the trench coat: sentences designed
for boat autopilots, riding alongside ELM327 AT commands, both older than
the laptop carrying them, driving a 2025 Porsche dashboard. The rig remains
at peace with itself.

A second tool, `gps_overlay.py`, is a later and separate process: a browser
map fed from a live port or a replayed capture. It is not folded into the
listening post or into `obd_feed`. Capture stays dumb on purpose.

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
output format is the same either way — seconds, a tab, the sentence — so
the future parser never has to care which door was used.

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

## The output

Either door, the capture lands in `xgps160-capture.txt` in the directory
you ran from: one sentence per line, prefixed with seconds-since-start to
the millisecond and a tab. That first column is the entire point of the
tool — it's what turns "supports ~10Hz" from a spec-sheet claim into a
measurement.

The file is pure printable ASCII, by construction: any byte the device
says that isn't printable ASCII is spelled `\xNN` (and a literal
backslash doubles), so the three binary packets the XGPS160 opens every
connection with sit in the capture legible and byte-complete instead of
either crashing the run (edition one), losing a byte to a replacement
character (edition two), or turning the file into something grep refuses
to search (the binary-sink road not taken). The spelling has an inverse,
proven in the test suite, so a future binary-protocol parser inherits
exact bytes from any capture made today.

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

## Verifying a capture

`gps_verify.py` is the health report for any capture (or overlay run log —
same format): sentence census with measured rates, checksum coverage,
binary-damage count, fix validity, speed, and the track's footprint in
meters. Tell it what the capture claims to be and the exit code becomes a
verdict:

```
py gps\gps_verify.py xgps160-capture.txt --expect stationary
py gps\gps_verify.py runs\gps-last.txt --expect drive
```

A stationary capture must stay within GPS-wander range of its first fix
and never claim real speed; a drive must actually go somewhere. The
driveway control is the one that matters: a parser that reports motion
in a parked capture has failed before it ever sees a drive.

One deliberate manner: the report prints sizes, rates and counts — never
a coordinate. Captures tend to begin at someone's front curb, and a
health report should be safe to paste into an email without mailing
anyone your house.

## Overlay (v1)

Ego-centered, North-up map: arrow pinned to the center, rotates with
heading (last COG held when nearly stopped), trail scrolls under the car.
Fixed scale (default 200 m across the shorter window edge — street/track
driving). Browser owns the trail (refresh clears it). Separate process
from the OBD feed and from the capture tool.

The live door is the same two-door open the capture tool uses — a `COMn`
name goes through pyserial, a `/dev/cu.*` path is a plain file open. Same
outgoing-port rule as above.

```
py gps\gps_overlay.py --list-ports
py gps\gps_overlay.py --port COM5
py gps\gps_overlay.py --replay runs\gps-last.txt
```

`--list-ports` names the Bluetooth peer when Windows will tell us, so the
outgoing XGPS is visible next to the incoming listen-only half of the pair.
Open in Chrome or an OBS browser source:

```
http://127.0.0.1:8765/
http://127.0.0.1:8765/?meters=200
http://127.0.0.1:8765/?up=heading
http://127.0.0.1:8765/?meters=200&up=heading
http://127.0.0.1:8765/?smooth=off
```

OBS sets pixel Width × Height; the page fills the window. World scale is
`?meters=` (default 200). Use `?meters=50` if you want the old walking
yard for paddock testing. Orientation is `?up=north` (default — map
North-up, arrow rotates) or `?up=heading` (arrow fixed pointing up, map
rotates with course). Other knobs are constants in `overlay/overlay.js`
for now and will likely become URL params later.

The page polls `/live` at 10 Hz to match the receiver, but paints from
requestAnimationFrame at about 30 Hz. The extra frames are not repeats:
heading and position ease toward the newest fix, and between fixes the car
dead-reckons along its last course for at most a quarter second, so a
dropped sample coasts and a dead feed parks instead of driving off into
fiction. Every fix corrects it, so the error cannot outlive one sample.
`?smooth=off` paints raw fixes for an honest A/B — that is the 10 Hz
staircase the bridging exists to hide.

Every live overlay run also records the raw NMEA stream in the same format
as `gps_capture.py`, so it can be fed directly back to `--replay`. The
default `--run-log tail` keeps a size-capped `runs\gps-last.txt` and rotates
the previous data-bearing run to `runs\gps-prev.txt`; a start that receives
no sentences leaves both alone. Use `--run-log full` for a drive you intend
to keep (`runs\gps-YYYYMMDD-HHMMSS.txt`), or `--run-log off` for no logging.
`--log-dir` changes the directory. Replay input is never logged again.

```
py gps\gps_overlay.py --port COM5
py gps\gps_overlay.py --port COM5 --run-log full
py gps\gps_overlay.py --replay runs\gps-last.txt
```

`config.json` (overlay only — capture has no argparse, so it has no
config section):

```
{
  "gps_overlay": {
    "port": "COM5",
    "http_port": 8765,
    "run_log": "tail",
    "log_dir": "runs"
  }
}
```

The GPS is not the OBDLink. Put the XGPS port in `gps_overlay`, not in
`common`, or the overlay will open the adapter.

## Tests

```
py gps\test_gps_capture.py        (Windows)
python3 gps/test_gps_capture.py   (macOS / anywhere)
py gps\test_nmea.py
py gps\test_live_state.py
py gps\test_gps_verify.py
```

Canned byte streams only — no device, no COM port, no pyserial needed. The
capture suite defends the framing (chunks split mid-sentence, `\r\n` vs `\n`,
the deadline, EOF, Ctrl-C), which is the only promise the capture tool makes —
plus the byte-escape spelling and its inverse, walked over all 255 possible
line bytes, and the two-door dispatch: the name classifier, the flag each
door actually returns, and the rule that an empty read on a timeout'd port
is a quiet quarter-second, not a goodbye. The nmea and live-state suites
cover the overlay's RMC/GGA parser and the live-fix smoother; the verify
suite proves the health checker's controls actually discriminate — its
synthetic stationary capture must fail the drive claim and vice versa —
and that its report keeps the no-coordinates promise.
