# gps/ — the XGPS160 leg: a listening post that finally heard something

There is a listening post in this directory and it deliberately understands
nothing. `gps_capture.py` writes down whatever the XGPS160 says, timestamped,
and draws no conclusions. For weeks that restraint guarded an empty
notebook; then four real captures came back — a driveway control and two
drives — and the questions the timestamp column was built to answer got
answered with numbers instead of a spec sheet:

* The advertised 10 Hz belongs to **position only**, and here is the
  selector next to the count: GGA and RMC tick at 9.6–9.9 Hz measured
  from the first NMEA sentence onward. A whole-file census (what
  `gps_verify.py` prints) reads a shade lower on short captures, because
  its span includes the ~2 s binary hello before the first sentence.
  The satellite gossip rides far below either number: GSV bursts at
  2–3 Hz, GSA and the proprietary `$GPPWR` at about 1 Hz.
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

## Capture

Install the one dependency, if the MX+ work didn't already:

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

Ctrl-C ends a capture early and keeps everything framed so far; a short
capture is still a good capture.

## The output

The capture lands in `xgps160-capture.txt` in the directory
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
   The captures confirmed it: one unique coordinate across the whole
   driveway file. The overlay freeze below 2 km/h is therefore a crawl
   hold, not a parked one — the receiver already does the parked pin;
   it scribbles once you roll. The same box at speed looks like what it
   actually is, and that's the number we have to design around.

Send back both `xgps160-capture.txt` files (rename them so they don't
clobber each other — `parked.txt` and `moving.txt` works). If a capture
comes out empty or strange, send it anyway: a weird capture is data, and
for once it won't be user error — the usual empty file is the incoming
half of the SPP pair.

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
heading (last COG held in the crawl), trail scrolls under the car.
Scale starts at 200 m across the shorter window edge and eases out
toward a cap to keep the trail on screen. Browser owns the trail
(refresh clears it). Separate process from the OBD feed and from the
capture tool.

The live door is the same COM open the capture tool uses. Same
outgoing-port rule as above.

```
py gps\gps_overlay.py --list-ports
py gps\gps_overlay.py --port COM5
py gps\gps_overlay.py --replay runs\gps-last.txt
```

`--list-ports` names the Bluetooth peer when Windows will tell us, so the
outgoing XGPS is visible next to the incoming listen-only half of the pair.
Open in Chrome or an OBS browser source. Production is the bare URL;
add knobs for a bench or a different view:

```
http://127.0.0.1:8765/                 (OBS: transparent, no HUD)
http://127.0.0.1:8765/?bg=grey&hud=on  (browser bench)
http://127.0.0.1:8765/?bg=dark&hud=on&map=alidade
http://127.0.0.1:8765/?bg=grey&hud=on&map=toner
http://127.0.0.1:8765/?bg=grey&hud=on&map=terrain
```

```
?meters=200          floor: metres across the short edge
?metersMax=1000      zoom-out cap; same as ?meters= locks the scale
?up=heading          map/trail follow a slow camera heading; arrow may yaw a little
?headingTau=1200     that camera time constant in ms (default 1200). 0 or off:
                     old heading-up (arrow glued up, world uses body heading)
?bg=grey             solid bench (default transparent for OBS)
?bg=dark             original near-black stage
?hud=on              status line (hidden)
?trailPause=off      age by wall clock while parked (default pauses)
?smooth=off          raw fixes, no bridging (debug)
?map=off|alidade|toner|terrain
                     Stadia raster under the trail (default off).
                     alidade = Smooth Dark; toner / terrain = Stamen.
                     stadia and on still mean alidade.
?stadiaKey=          optional Stadia API key (try 127.0.0.1 without it first)
```

`?map=alidade` (or `toner` / `terrain`) is a prototype. `?up=heading` rotates
the basemap with a slower camera heading so GPS course jitter twists the
arrow instead of the world; `?headingTau=` is that time constant in
milliseconds (default 1200). `?headingTau=0` or `off` is the old
glued-arrow behaviour. The default URL stays transparent for OBS. Lock
the scale (`?meters=200&metersMax=200`) if you want a fixed zoom. If
tiles 401, add `?stadiaKey=` from the dashboard; do not put the key in a
tracked file. A tile that fails to load is a hole, not a life sentence:
it is asked for again after 4 s, and if it keeps failing the wait doubles
per failure up to a minute and holds there, per tile, so a dropped tile
on flaky LTE heals on the first ask while a wrong key or a dead link
costs one request per tile per minute instead of a storm. The console
says so once when the first tile reaches the minute, and once more when
tiles load again. Toner and Terrain are light; `?bg=grey` is a better
bench than `?bg=dark`.

OBS sets pixel Width × Height; the page fills the window. Scale stays
at the floor while the trail fits, eases out toward the cap when the
farthest point from the car would run off the short edge (world
radius, so heading-up does not pump the zoom), and eases back in more
slowly when it fits again. A ten-minute freeway trail will hit the
cap; that is the point of the cap. `?meters=50` is the old walking
yard for paddock testing.

The page polls `/live` at 10 Hz to match the receiver, but paints from
requestAnimationFrame at about 30 Hz. The extra frames are not repeats:
heading and position ease toward the newest fix, and between fixes the car
dead-reckons along its last course for at most a quarter second, so a
dropped sample coasts and a dead feed parks instead of driving off into
fiction. Every fix corrects it, so the error cannot outlive one sample.
If the Bluetooth link drops, the reader catches it and `/live` flips to
`ok: false` with a reason instead of serving the last speed forever. The
page ages the last `t` and paints `signal lost Ns` once the fix is older
than a second — the 250 ms dead-reckon still parks the car; this is the
long goodbye. A silent port that never raises (Windows timeout returning
empty) is the same HUD, driven only by the stamp.
`?smooth=off` paints the receiver's last fix (`raw_lat`/`raw_lon` on
the same `/live` payload), with no ease, no dead-reckon, and no 3 m
trail skip — that is the 10 Hz staircase the bridging exists to hide.
Both views keep the same ten-minute trail window (age, not a point
count), with one ceiling over it: never more than 6000 points, oldest
dropped first. The ceiling is not a window — it is the guarantee that
the array cannot grow without bound, whatever the poll rate does later.
6000 is that window at the receiver's nominal rate (ten minutes at
10 Hz; the page appends per new fix, not per poll), so a rolling raw
trail reaches the clock and the ceiling in the same breath and the
clock is the authority. In the default view the 3 m skip appends
nothing at a light and takes about half an hour of road speed to fill
the array, so the ceiling is a net it does not normally reach. Where
the ceiling still binds is where the clock does not run: parked in the
raw view the clock pauses and the scribble does not, so a long enough
sit at the grid pushes the approach out from the front. That is the
trade for letting the raw view scribble at all — it is the receiver's
diary, not the lap. The clock for the window pauses while `/live` says
`crawl`, so a driveway wait or pre-grid does not peel the lap you just
drew; `?trailPause=off` ages by wall clock even while parked. That
pause is an opinion of the default view only: `?smooth=off` keeps
appending while parked, because the idle scribble is the thing that
view exists to show. A long light in the default view still costs
nothing — the 3 m skip appends no points.
The default `lat`/`lon` is still the server's EMA, frozen in the crawl
(below 2 km/h, where this receiver releases its parked pin and
scribbles); that alpha is a constant in `gps_overlay.py`, not a URL
param. The same decision is published as `crawl: true|false` on `/live`
(`null` before the first fix), and the page reads it instead of keeping
a speed threshold of its own — one authority for "the car is crawling",
living next to the code that freezes the position. With `?hud=on` the
status line says `crawl` while it is true. `crawl?` on the HUD means the
server never sent the field — in practice a `gps_overlay.py` started
before a pull and still running under the newer page. The trail then
ages by wall clock, as `?trailPause=off` does, and the browser console
says so once; restart the server and it goes away. (A stale cached page
prints neither word — it is still running the old rule silently, and a
hard refresh is the fix for that one.)

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

Once it is up, the terminal gets one status line a second — the same
shape as the OBD feed's `t NNNs` line, so two windows side by side read
as one instrument:

```
  t    12s  GPS ok       age   0.1s  speed  45.2 km/h  crawl no   sats  9  acc  3.1 m
  t   140s  GPS LOST     age  27.4s  speed  45.2 km/h  crawl no   sats  9  acc  3.1 m  (signal lost: ClearCommError)
```

Every field is read from the same snapshot `/live` serves — the age is
now minus the fix's own stamp, `crawl` is the published field, `ok` /
`LOST` / `waiting` is `ok` plus `reason` — not from a counter kept
beside it, for the same reason the page reads `crawl` instead of
guessing: one authority for whether the GPS is alive. The line never
goes quiet on purpose. A dropped receiver prints `LOST` with the reason
and an age that keeps climbing; a silent port that never raises prints
`ok` with an age in minutes, which is the true state of affairs. That is
the whole point of it: the supervisor's liveness rule is "the child
printed a line this second, so data moved," and until now this process
printed a banner and then nothing for hours while working perfectly —
point the supervisor at it and it would have read STALLED forever about
a process doing its job. The line is the prerequisite, and the supervisor
now reads it: `py supervisor\supervisor.py --gps -- --port COM3` runs both
legs in one window, reads the age and `LOST` off this line, and never
punishes quiet that the line explains — it restarts the overlay only if it
exits or prints nothing at all (root README, *Step 5*). There is no flag to
turn the line off, also on purpose — a config knob is a place for the
supervisor to lose its signal.

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
py gps\test_gps_capture.py
py gps\test_nmea.py
py gps\test_live_state.py
py gps\test_gps_verify.py
```

Canned byte streams only — no device, no COM port, no pyserial needed. The
capture suite defends the framing (chunks split mid-sentence, `\r\n` vs `\n`,
the deadline, EOF, Ctrl-C), which is the only promise the capture tool makes —
plus the byte-escape spelling and its inverse, walked over all 255 possible
line bytes, and the COM door: the name classifier, the flag it actually
returns, and the rule that an empty read on a timeout'd port is a quiet
quarter-second, not a goodbye. The nmea and live-state suites
cover the overlay's RMC/GGA parser, the live-fix smoother, the
`crawl` field it publishes (both sides of the 2 km/h line, and that it
is the same decision that freezes the EMA, not a twin), the
dropout door: a raised read marks the last pose lost, an empty timeout
does not — and the status line: every field from the `/live` snapshot
(a mutant ticker keeping its own last-fix stamp cannot print the ages
the tests ask for), one line per interval rather than per fix, `LOST`
with the reason once the source is gone, and still printing after it. The verify suite proves the health checker's controls
actually discriminate — its synthetic stationary capture must fail the
drive claim and vice versa — and that its report keeps the
no-coordinates promise.

The overlay page has its own checks, and they run under **node**, not
Python — the one place in this repo that asks for anything but Python:

```
node gps\overlay\test_overlay.js
```

Node is a *test* dependency, full stop. The simhub, the capture tool and
the overlay server run on Python and pyserial; the overlay page itself
needs nothing but a browser. Node exists here only to run `overlay.js` on
a bench, with a small shim standing in for the window, and if you skip
installing it you lose these 80 checks and nothing else — nothing that
drives, records or draws depends on it. Any node from the last few years
will do (written against 22; it uses node's standard library and nothing
from npm): install from nodejs.org, make sure `node --version` answers
from a fresh terminal, run the line above from the repo root. A pass
ends with a line like `all 80 checks passed` and exits 0. A failing
check prints `FAIL`, the check's name and what it actually saw, and
exits 1. Exit 2 means the file could not load `overlay.js` at all —
that is the bench's coupling to the IIFE wrapper, not a trail bug, and
the message names which anchor to update. What it defends: the trail
clock pauses on the server's `crawl` and nothing else; the 3 m skip
drops a fix under three metres from the last point and keeps one over;
the raw view scribbles while parked and never past the ceiling; the
ceiling is 6000 and never binds before the ten-minute clock at 10 Hz;
parked points age out by clock, not by being pushed off the front; a
`/live` with no crawl field warns once, never pauses and reads
`crawl?`; the HUD prints `crawl` when the field says so; `poll()`
hands the trail `data.crawl`, not the speed; `?map=` stays off unless
asked; slippy-map math inverts at the equator; heading-up fetches
the extra corner tiles a rotated view needs; a failed tile is re-asked
after 4 s and a tile that keeps failing backs off, doubling to a minute
and holding, per tile, with the console told once per outage; and
`?headingTau=` keeps a slow camera heading so the arrow, not the world,
takes the COG jitter.
