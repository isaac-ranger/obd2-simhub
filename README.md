# obd2-simhub

Feed **real-car telemetry** (RPM, speed, throttle, temps, gear …) from an OBD2
Bluetooth adapter into **[SimHub](https://www.simhubdash.com/)**, so its gauges,
dashboards, and stream overlays run off a real vehicle instead of a game.

```
 ┌─────────┐  Bluetooth SPP   ┌──────────────────┐   UDP (binary feed)   ┌────────┐
 │   Car   │ ───────────────► │  Extractor (PC)  │ ────────────────────► │ SimHub │
 │ OBD2    │   virtual COM    │  polls PIDs,     │   External Sim        │ dashes │
 │ port    │   port           │  decodes, sends  │   Integration         │overlays│
 └─────────┘                  └──────────────────┘   (.simdef contract)  └────────┘
```

Built and proven on a **2025 718 Cayman GTS 4.0** with an **OBDLink MX+**, but
nothing here is Porsche-specific — any ELM327/STN-compatible adapter on any
CAN car should work, and step 1 tells you what *your* car actually gives you.

---

## What you need

| | |
|---|---|
| **Adapter** | OBDLink MX+ (what this was built on), or any ELM327/STN-compatible OBD2 Bluetooth adapter |
| **PC** | Windows, with Bluetooth |
| **Python** | 3.11+ recommended ([python.org](https://www.python.org/downloads/) — tick *Add python.exe to PATH*). 3.9/3.10 work, slightly less smoothly |
| **SimHub** | **9.11.5 or newer** — External Sim Integration is beta and arrived in that release |
| **A car** | …ideally. If you don't have one handy, see [No car? Try it anyway](#no-car-try-it-anyway) — the whole pipeline runs against a fake |

## Install

**Code → Download ZIP** on this page and extract it, or:

```
git clone https://github.com/isaac-ranger/obd2-simhub
cd obd2-simhub
pip install pyserial
```

That's the only dependency.

## Pair the adapter and find your COM port

1. Plug the adapter into the car's OBD2 port.
2. Pair it in Windows Bluetooth settings. On the MX+, press the **Connect**
   button to make it discoverable.
3. Pairing creates **two** COM ports — you want the **outgoing** one. Windows
   settings → *More Bluetooth settings* (Win 11) or *…options* (Win 10) →
   *COM Ports*. Or just ask:
   ```
   py probe\obd_probe.py --list-ports
   ```

Everything below uses `COM3` as the example. Substitute yours.

## Write it down once: config.json

The port never changes once a rig works, so no command below actually needs
it typed. Copy the example and put yours in:

```
copy config.example.json config.json
```

```json
{
  "common": { "port": "COM3" }
}
```

From then on `py extractor\obd_feed.py` alone is a complete command. The
rules, all four of them:

* **`common`** holds values shared by more than one tool (the port, the
  baud). A section named after a tool (`obd_feed`, `obd_probe`,
  `learn_gears`, `fake_car`, `supervisor`, `report`) applies to that tool
  only, and beats `common`.
* **The command line beats the file.** `--port COM7` on a config that says
  `COM3` means `COM7`, today only.
* **A key is the option name with underscores:** `--dash-gear` →
  `dash_gear`. Every option of every tool works — the keys come from the
  tools' own parsers, so there is no separate list to fall out of date.
* **A typo refuses to start.** A key or section nothing recognizes stops
  the tool with an error naming it (and usually the fix), because a setting
  that's silently ignored looks exactly like a setting that doesn't work.

`config.json` is yours: hand-edited, untracked. `calibration.json` stays
machine-written by `learn_gears.py`. Neither ever touches the other.

---

# Step 1 — Probe: what can your car actually do?

**Start the engine** (not just ignition — some PIDs sleep otherwise), then:

```
py probe\obd_probe.py --port COM3 --json report.json
```

The probe is **read-only**. It sends Mode 01 data requests and ELM
configuration, nothing else — it never writes to the ECU and never touches
trouble codes.

### What you should see

```
Adapter reset:     ELM327 v1.4b
Contacting vehicle (0100 supported-PID request)...
   56 PIDs; polling 3 fast + 7 rotating
```

…followed by a table of which dashboard channels your car advertises, live
decoded values, and measured request rates.

### Reading the result

* **Protocol + responding ECUs** — confirms the adapter is really talking to
  the car, not just to itself.
* **PIDs advertised** — which channels exist on *your* car. RPM, speed and
  throttle are near-universal. Oil temp, fuel level and timing advance vary.
* **Request rate** — the binding constraint on everything downstream. On the
  982 it measured 5 Hz, and *the same 5 Hz whether asking for one PID or six*,
  which is why the extractor always sends 6-PID batches.

**No `A4` (transmission gear) in the list?** Normal — most cars don't expose
it. Step 2 derives gear from RPM ÷ speed instead.

### If it doesn't work

| symptom | try |
|---|---|
| `could not open port` | Wrong COM port — you probably have the *incoming* one. Run `--list-ports` |
| Connects, then `NO DATA` everywhere | Engine not running, or the adapter isn't seated. Push it in firmly |
| Nothing at all | Re-pair the adapter; press **Connect** on the MX+ first |

---

# Step 2 — Calibrate: teach it your gearbox

Skip this if you only want RPM/speed/throttle. Do it if you want a **gear
readout**.

Because most cars don't publish gear, this project derives it from the RPM ÷
speed ratio — which means it needs to know your gearbox's ratios. It **learns**
them from a drive rather than reading them off a spec sheet, because published
ratios only become useful through an assumed tire size and final drive, and
those are two more numbers to transcribe wrong.

**Log an ordinary drive.** No special route, no heroics:

```
py probe\obd_probe.py --port COM3 --log drive.csv
```

Start it before pulling out, ignore it completely while moving, **Ctrl-C when
parked**. It just needs a few settled seconds in each gear at some point, which
any errand with a freeway on-ramp provides. Every row is flushed as it's
written, so a laptop dying mid-drive keeps everything up to its last sample.

**Turn the drive into constants:**

```
py probe\learn_gears.py drive.csv --write calibration.json
```

The ratio histogram clusters at one spike per gear, and those centers *are*
your gearbox as the ECU reports it.

### Then edit `calibration.json` by hand for:

* **`active_set`** — which tire set is on the car. Change one line when you
  swap wheels.
* **`speed_factor`** — corrects the ECU's over-reported speed on smaller tires
  (true = reported × factor).
* **`engine.fuel_tank_l`** — your tank size in liters. The fuel PID reports
  percent; SimHub wants liters.
* **`units.display`** — `imperial` or `metric` for console output.

The gear constants themselves are **tire-independent** (OBD speed and rpm both
live upstream of actual tire size), so a wheel swap never means re-learning
gears.

---

# Step 3 — Point SimHub at the feed

Three things, once, on the SimHub machine.

**1. Register the definition:**

```
py extractor\obd_feed.py --register
```

This writes a `.simlink` file telling SimHub where the repo's `.simdef`
contract lives.

**2. Activate "OBD2 SimHub"** in SimHub's sim list.

SimHub's auto-detection watches for a process named `Obd2SimHub`, which a
Python script is not — so either activate it manually, or indulge in the
one-liner:

```
copy .venv\Scripts\python.exe .venv\Scripts\Obd2SimHub.exe
```

and launch the feed with that instead. Windows names the process after the exe,
SimHub sees its sim arrive, and nobody needs to know the sim is a Porsche with
a Python interpreter in the passenger seat.

**3. Verify** with **Telemetry Receiver Tester** (in SimHub's definition
editor) — it shows the live values arriving.

---

# Step 4 — Run the feed on its own

Before automating anything, watch it work with your own eyes.

```
py extractor\obd_feed.py --port COM3
```

### What you should see

```
UDP feed  -> 127.0.0.1:35353  (101 bytes/packet at 60 Hz)
Run log   -> runs\feed-last.csv  (tail of this run; --run-log full to keep)
Units     -> imperial   tire set -> street_18 (speed factor 1)
Dash gear -> hold
Contract  -> SimHub definition 9a62309a-… (layout v1.0)

Adapter reset:     ELM327 v1.4b
Contacting vehicle (0100 supported-PID request)...
   56 PIDs; polling 3 fast + 7 rotating (slow tier refreshes ~every 0.7s)

Auto-tune: response-count digit (each candidate 2s)...
   no digit : 22.0 req/s, all 6 PIDs answered
   digit 3  : 39.0 req/s, all 6 PIDs answered
   keeping: digit 3 (39.0 req/s)

  t   42s  RPM  2100  speed  31  gear 3  throttle 18%  …
```

**That last line, once a second, is the thing to watch.** If the numbers move
when you blip the throttle, the whole chain works. Check SimHub's gauges are
moving too.

Ctrl-C to stop.

Every run logs itself to CSV as a side effect — but by default only the **last**
run survives: `runs\feed-last.csv`, overwritten at every start and size-capped,
so nothing accumulates no matter how long the season. Something strange happen
an hour in? The file has it — copy it out before the next start eats it. Three
settings (`--run-log`, or `run_log` in config.json):

* **`tail`** (default) — one overwritten file. Overlays are the product;
  the log is a diagnostic, not an archive.
* **`full`** — a timestamped file per run, kept forever. For drives you mean
  to keep: gear learning, development, data-making. The supervisor takes the
  same option for its own `runs\supervisor-*.log`.
* **`off`** — nothing at all.

### After the drive: the report

```
py report.py runs\feed-last.csv
```

Five sections: cadence and channel coverage; the gear ladder measured *this*
drive against your calibration; shifts, with clutch windows timed and
downshifts separated from coasting; warm-up; extremes.

Two things it caught on the first real street drive:

* two quick upshifts had clutch windows of 0.75 s and 0.94 s, and the 4→3
  downshift never showed neutral at all — a clean rev-match, measured;
* when the coolant said *ready* (90 °C at t=160 s), the oil was at 62 °C — and
  it never reached 80 °C in the whole drive. **The coolant gauge is not an oil
  gauge**, and the CSV had been recording the difference all along.

---

# Step 5 — Run the supervisor when it all works

Step 4 answers *can the car talk to SimHub*. The supervisor answers *can it
keep doing that for three hours while your hands are full* — which is the one
that matters at an event.

```
py supervisor\supervisor.py -- --port COM3
```

Everything after `--` is passed to `obd_feed.py` untouched, so every flag the
feed has works here unchanged.

### What it does

It reads the feed's once-a-second status line and treats that as the liveness
signal — meaning *data actually moved*, not merely *the process is still
running*. A process can be alive and wedged; a printed sample cannot.

| what happens | what it does |
|---|---|
| feed exits (crash, sender death, 25 misses) | restarts it after a backoff that resets once a run has been healthy |
| feed alive but silent past `--stall-seconds` (default 10) | reports `STALLED` — it may still come back on its own |
| feed alive, no data for `--stall-restart-seconds` (default 45) | judges it wedged, kills it, restarts it. `0` = report only |
| adapter was never there | reports `NO_ADAPTER`, keeps retrying forever |

That third row is the one that matters in pre-grid: a serial write blocked on a
dead Bluetooth handle **never exits by itself**. The supervisor can't
power-cycle the adapter for you — but it never stops trying, so when you *do*
pull and replug the MX+, the feed comes back on its own and you never touch the
keyboard.

### The status file

`status/obd2_status.json`, rewritten atomically once a second, carrying a
plain-English `summary` sentence meant to drop straight into a voice
assistant's context.

**The one rule for anything reading it:** check the clock. A dead supervisor
leaves behind a file that still cheerfully says `LIVE`. Compare `updated_unix`
against now, and against `stale_after_s`. Field reference:
[`status/README.md`](status/README.md).

---

## No car? Try it anyway

The repo ships its own fake car, so the whole pipeline runs on a desk:

```
py extractor\fake_car.py --tcp 35000                            # terminal 1
py extractor\obd_feed.py --port socket://127.0.0.1:35000        # terminal 2
```

It answers 6-PID batches the way the real 982 does and drives itself up and
down through all six gears using your `calibration.json`, so the gear readout
has something honest to chase. It's a gear-shaped signal generator, not a
physics model — its idea of a downshift would not pass tech inspection.

Or replay a real recorded drive:

```
py extractor\obd_feed.py --replay reports\2026-07-31-kris-drive_01.csv
```

## Tests

```
py probe\test_parse.py            py probe\test_learn_gears.py
py extractor\test_feed.py         py supervisor\test_supervisor.py
py test_report.py                 py test_config.py
```

The parser suite is 51 adversarial fixtures (single-frame, batched, ISO-TP
multi-frame, spaced/unspaced/lowercase, multi-ECU, negative responses, ELM
error strings, truncation). The feed suite **replays a real drive** and asserts
the readout tells the same story the driver did: 1-2-3-4-5-6, back down
sequentially, and a 1st-gear pull to 7,622 rpm. The supervisor suite injects
four ways a real feed fails rather than asserting on internal state — the whole
point of that layer is recovering from failures nobody predicted, so the tests
do the predicting badly on purpose.

Your commute is now a regression test.

## Repo layout

```
probe/         obd_probe.py     step 1 — survey + drive logging
               learn_gears.py   step 2 — drive log → gear constants
extractor/     obd_feed.py      step 4 — the live feed
               fake_car.py      a car-shaped thing for desk testing
supervisor/    supervisor.py    step 5 — keeps the feed alive
simdef/        the SimHub contract
calibration.json                your car: gears, tires, tank, units
config.example.json             copy to config.json: your rig (port, baud)
obd_config.py                   the config layer every tool loads
report.py                       post-drive analysis
docs/          DEVELOPMENT-NOTES.md
```

## Going deeper

**[docs/DEVELOPMENT-NOTES.md](docs/DEVELOPMENT-NOTES.md)** — measured phase 1
results from the 982, the design rationale (why 6-PID batches, why interpolate,
why learn gear ratios instead of tabling them), the SimHub contract details,
CAN-bus research notes, and what standard OBD2 can and can't give you. Also
several corrections this project made to its own earlier guesses, kept on
purpose.

## License

MIT
