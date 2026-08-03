# obd2-simhub

Feed **real-car telemetry** (RPM, speed, throttle, temps, …) from an OBD2
Bluetooth adapter — built around the **OBDLink MX+**, works with any
ELM327/STN-compatible adapter — into **[SimHub](https://www.simhubdash.com/)**,
so its gauges, dashboards, and stream overlays run off a real vehicle.

```
 ┌─────────┐  Bluetooth SPP   ┌──────────────────┐   UDP (binary feed)   ┌────────┐
 │   Car   │ ───────────────► │  Extractor (PC)  │ ────────────────────► │ SimHub │
 │ OBD2    │   virtual COM    │  polls PIDs,     │   External Sim        │ dashes │
 │ port    │   port           │  decodes, sends  │   Integration         │ overlays│
 └─────────┘                  └──────────────────┘   (.simdef contract)  └────────┘
```

The SimHub side uses the official **[External Sim
Integration](https://manual.simhubdash.com/external-sim-integration)** feature
(SimHub ≥ 9.11.5, currently beta): a `.simdef` file declares the telemetry
contract, the extractor sends a matching binary UDP feed, and SimHub treats the
real car as just another sim — so standard dashboards and overlays bind to
RPM/speed/throttle exactly as they would in a game.

## Status

| Phase | What | State |
|-------|------|-------|
| 1 | **`probe/obd_probe.py`** — measure what *your* car + adapter can actually deliver | ✅ ready — [first real-car run in](#phase-1-results--2025-718-cayman-gts-40-982) |
| 2 | **`extractor/obd_feed.py`** — poll loop + gear readout + 60 Hz SimHub UDP feed | ✅ built & tested, speaking the **real SimHub contract** ([PR #2](https://github.com/isaac-ranger/obd2-simhub/pull/2)'s .simdef + generated constants, 101 bytes, verified byte-for-byte) — [run it](#phase-2--run-the-extractor). **Confirmed on the real car 2026-07-31** — speed, RPM, gear, fuel all correct on the road. |
| 3 | **`supervisor/supervisor.py`** — keeps the feed alive unattended, publishes a status file | ✅ new — [run it](#phase-3--the-supervisor-unattended-operation) |
| — | **`report.py`** — post-drive report from any run log | ✅ new — [after the drive](#after-the-drive-the-report) |

Phase 1 exists because the design hinges on three facts that vary per car:
which protocol the car speaks (CAN vs. pre-2008 buses are wildly different in
speed), which PIDs the ECU exposes, and the real achievable polling rate.
Guessing those makes a bad bridge; measuring them takes five minutes.

## Phase 1 — run the probe (Windows)

1. Get the code: **Code → Download ZIP** on this page and extract it (or
   `git clone https://github.com/isaac-ranger/obd2-simhub`), then open a
   terminal in the extracted folder.
2. Install Python 3.9+ from [python.org](https://www.python.org/downloads/)
   (check "Add python.exe to PATH"), then:
   ```
   pip install pyserial
   ```
3. Plug the OBDLink MX+ into the OBD2 port. Pair it in Windows Bluetooth
   settings (press the **Connect** button on the MX+ to make it discoverable).
4. Find the COM port — pairing creates **two**; you want the **outgoing** one
   (Bluetooth settings → *More Bluetooth settings* on Win 11, *...options* on
   Win 10 → *COM Ports*), or run:
   ```
   py probe\obd_probe.py --list-ports
   ```
5. Engine running (not just ignition — some PIDs sleep otherwise), then:
   ```
   py probe\obd_probe.py --port COM5 --json report.json
   ```
   For a gear-ratio learning drive instead (phase 2 prep):
   ```
   py probe\obd_probe.py --port COM5 --log drive.csv
   ```

Turn a learning drive into gear constants with:
```
python probe/learn_gears.py reports/2026-07-31-kris-drive_01.csv
python probe/learn_gears.py drive.csv --write calibration.json
```
`calibration.json` at the repo root is the file you edit by hand: when you
swap wheels, change `active_set` — one line. `speed_factor` corrects the
ECU's over-reported speed on smaller tires (true = reported × factor). The
gear constants themselves are **tire-independent** — OBD speed and rpm both
live upstream of actual tire size — so a wheel swap never re-learns gears.
The first real log (2026-07-31, 982 GTS street set, all six gears swept) and
the constants it produced ship in `reports/` and `calibration.json`.

The probe is **read-only**: Mode 01 data requests and ELM configuration only.
It never writes to the ECU and never touches trouble codes.

It reports: adapter identity (ELM vs. STN firmware), detected protocol,
number of responding ECUs, which dashboard-relevant PIDs the car advertises,
live decoded values as a sanity check, and measured request rates — single-PID
and batched (on CAN, up to 6 PIDs ride one request; that batching is the
difference between choppy and smooth gauges).

## Phase 1 results — 2025 718 Cayman GTS 4.0 (982)

First real-car run, 2026-07-30, OBDLink MX+ over Bluetooth SPP. Raw report:
[`reports/2026-07-30-kris-982-gts.json`](reports/2026-07-30-kris-982-gts.json).

| | |
|---|---|
| Adapter | ELM327 v1.4b / STN2255 v5.12.4, 14.1 V |
| Protocol | ISO 15765-4 CAN (11-bit, 500 kbaud), 1 ECU responding |
| Mode 01 PIDs advertised | 56 |
| Request rate | **5.0 Hz — identical at 1, 3, and 6 PIDs per request** |
| Best channel throughput | **30 channel-updates/s** (6 PIDs × 5.0 Hz, 6/6 answered) |

**Dashboard channels this car gives you:** engine load `04`, coolant temp `05`,
timing advance `0E`, RPM `0C`, speed `0D`, throttle `11`, fuel level `2F`,
barometric pressure `33`, module voltage `42`, **oil temp `5C`**.

**Gaps:** no MAF `10`, no manifold pressure `0B`, no intake air temp `0F`, no
fuel rate `5E`, and **no transmission gear `A4`** — gear has to be derived from
RPM ÷ speed against the car's ratios.

### The number that drives the whole design

The rate is **5.0 Hz whether you ask for one PID or six**. The cost is the
request round-trip, not the payload — so *batching is free*, and any request
that carries fewer than 6 PIDs is wasting the trip.

**This corrects an earlier claim in this file.** The pre-measurement guess here
was "a 6-PID batch at ~15–30 Hz (≈100–180 channel-updates/s)." The car returned
30 channel-updates/s — between 3× and 6× under. The request rate was the part
that was over-estimated; batching worked exactly as predicted (6/6 answered in
one message). Design accordingly, and treat rate guesses for other cars with
matching suspicion.

**5.0 Hz is a floor, and there is a known lever.** The probe deliberately omits
the ELM327 expected-response-count digit (e.g. `010C0D11… 1`), which lets the
adapter return as soon as it has what it was told to expect instead of waiting
out its response timer. Getting the digit right is itself an experiment: the
datasheet counts *responses*, and one ECU answering a 6-PID batch sends one
message — but that message spans three CAN frames, and if the chip counts
received lines the right digit is `3`, not `1`. (A too-large digit is a no-op —
the timer runs out exactly as with no digit; the datasheet warns a too-small
one can cut off the transfer.) The extractor's startup auto-tune now runs this
experiment on the live car and keeps whichever digit moves the number.

**Confession (found during phase 2, 2026-07-31): the floor was partly ours.**
The probe's serial read loop waited for data in 200 ms gulps — so no matter
how quickly the car answered, a request could never complete faster than
5.0/s. That suspiciously round "5.0 Hz at 1, 3 AND 6 PIDs" was the sound of
our own read timer, not the ECU's round-trip. (It took a fake car that
answers in microseconds still "measuring" 5.0 Hz to expose it. The fake then
did 8,900 requests/s.) Fixed — probe and extractor now return the moment the
adapter finishes talking. The measured numbers above stand as what phase 1
actually delivered, but the real ceiling of this car is **unmeasured and
higher**; the extractor's auto-tune will find it on the next drive. Gauge
design still assumes 5 Hz and treats anything better as a gift.

## Phase 2 — the extractor (design)

Design below is now settled against the measured numbers above:

- **Every request carries 6 PIDs — 3 fixed fast + 3 rotating slow.** Since a
  6-PID request costs the same as a 1-PID request, the scheduler never sends a
  short one. Fast slots are permanently RPM `0C`, speed `0D`, throttle `11`;
  the remaining three slots rotate through the seven slow channels (load,
  coolant, timing, fuel, baro, voltage, oil temp).

  The arithmetic on the measured 5.0 Hz: fast channels land the **full 5 Hz**,
  and seven slow channels through three rotating slots refresh every ⌈7/3⌉ = 3
  requests ≈ **0.6 s** — comfortably fresh for temperatures and fuel level.
  Naive round-robin over all ten channels would instead give RPM 3.0 Hz
  (6 slots × 5 Hz ÷ 10 channels). The batching structure *is* the tiering.
- **Decoupled UDP send — now load-bearing, not optional.** With the car
  delivering RPM at 5 Hz and SimHub wanting ≥60 Hz, the send loop is what stands
  between the measurement and a visibly stepping tach. It runs at a fixed 60 Hz
  off last-known values, and RPM/speed get **interpolation rather than
  sample-and-hold** — at 5 Hz the difference is the whole feel of the gauge.
  Interpolation must never be applied to the slow tier: a smoothly-drifting
  coolant needle would be fiction.
- **`.simdef` contract** authored in SimHub's built-in definition editor
  (Settings → enable *game definition authoring tools*), fields matching what
  the car actually provides. SimHub generates the exact C# packet struct
  ("copy demo code"), which drops into the extractor.
- **Derived gear, because `A4` is confirmed absent on this car.** Gear comes
  from the RPM ÷ speed ratio snapped to the nearest known ratio. **Gearbox
  confirmed by the owner 2026-07-30: 6-speed manual** (the 982 GTS 4.0 also
  ships as a 7-speed PDK; this one is not that). The manual is the favourable
  case: with the clutch out the driveline is locked, so the ratio is clean
  arithmetic. (A PDK's ratios are just as fixed — it is a dual-clutch box, no
  torque converter — but it creeps and launches on a slipping clutch under its
  own control, so its low-speed ratio data is garbage in a way a driver-operated
  clutch pedal at least announces.)
  - **Learn the ratios, don't table them.** Published gearbox ratios only
    become rpm-per-km/h through an assumed tire circumference and final drive
    — two more spec-sheet numbers to transcribe wrong. A drive log's ratio
    histogram clusters at six spikes whose centers *are* the gearbox as the
    ECU actually reports it — measured, no assumptions to get wrong.
  - **Clutch/neutral guard is mandatory on a manual.** Clutch in or in neutral,
    RPM ÷ speed is meaningless, and snap-to-nearest would flicker the readout
    through the whole H-pattern on every shift. Rule: a ratio not near one of
    the six learned clusters reads *neutral/shifting*, never a gear.
  - **The drive log is built: `--log` (2026-07-31).**
    ```
    py probe\obd_probe.py --port COM5 --log drive.csv
    ```
    Skips the survey, polls RPM + speed + throttle in one batched request at
    the car's measured rate, one CSV row per sample (`t_s,rpm,speed_kmh,
    throttle_pct`), every row flushed as written so a laptop dying mid-drive
    keeps everything up to its last sample. Ctrl-C ends it cleanly; transient
    hiccups are skipped rather than fatal.

    **The drive protocol:** one ordinary drive — no special route, no heroics.
    It just needs a few settled seconds in each of the six gears at some
    point, which any errand with a freeway on-ramp provides. Start the log
    before pulling out, ignore it completely while moving, Ctrl-C when
    parked, send back `drive.csv`. The ratio histogram then clusters at six
    spikes whose centers are the gearbox as the ECU actually reports it —
    measured off the car, no spec sheet involved. The tool's job is to be
    boring; the drive's job is to be a drive.

## Phase 2 — run the extractor

Everything above, built. `git pull`, then:

```
py extractor\obd_feed.py --port COM3
```

That is the whole thing: batched 6-PID polling (3 fast + rotating slow
slots), the response-count auto-tune on startup, live gear derived from your
own learned constants, true road speed via the active tire set, and a 60 Hz
UDP feed with RPM/speed interpolated between samples — a needle that sweeps
instead of stepping. Every run also logs itself to `runs/*.csv` as a side
effect, so every errand is free calibration data.

(Python 3.11+ recommended on Windows: older versions sleep in 15.6 ms
gulps, which fights a 60 Hz send loop. The sender spin-guards its last two
milliseconds either way, so 3.9/3.10 still work — they just pay a little
more CPU for their punctuality.)

No car handy? Feed it your own recorded drive and watch the pipeline call
your gears back at you:

```
py extractor\obd_feed.py --replay reports\2026-07-31-kris-drive_01.csv
```

(The test suite does exactly this — `python extractor/test_feed.py` replays
your actual drive and asserts the readout tells the same story you told us:
1-2-3-4-5-6, back down sequentially, and a 1st-gear pull to 7,622 rpm. Your
commute is now a regression test. `runs/` output, units, and everything else
are covered there too.)

**Units:** `calibration.json` now has `"units": {"display": "imperial"}` —
console output in mph and °F, as demanded by residents of countries that
have been to the Moon. The wire feed and CSV logs stay metric/SI because
SimHub and arithmetic prefer it; dashboards convert at the glass, where
conversion belongs. `--units metric` overrides per run if a passenger
objects.

**Dash gear:** the gear judge is honest — the moment the clutch breaks the
rpm/speed ratio it says N, and on your first real drive that was 57% of all
samples (a minute and a half standing at lights, another minute of clutch-in
coasting). Correct in the log; on a stream overlay it reads as a broken
widget flashing N through every shift. So the dash now wears a **held**
view by default: the last engaged gear stays up while the car is still
rolling, N only when actually stopped (or the engine is off). The run log
and the judge never see the hold — logs stay honest, always. `--dash-gear
honest` puts the raw judge on the dash if you'd rather watch it think.

### After the drive: the report

Every run logs itself (`runs/*.csv`). The log can testify to more than the
dashboard showed, and `report.py` takes its deposition:

```
python report.py runs/feed-20260731-204617.csv
```

Five sections, all auditable from the definitions in the file's docstring:
cadence and channel coverage; the gear ladder measured **this** drive
against calibration (your first street drive re-confirmed gears 2–4 within
half a percent and gear 1 within 1.5% — the 1st-gear samples sit at
12–16 km/h, where integer-km/h OBD speed floors the ratio math; from
16 km/h up, 1st agrees within 0.15% — all without knowing it was doing
calibration work); shifts — timed clutch
windows, separated from coasting, including downshifts rev-matched so
cleanly the judge never saw neutral; warm-up; extremes. Two findings from
your 2026-07-31 drive it would have handed you on the spot:

* your two quick upshifts had clutch windows of 0.75 s and 0.94 s, and the
  4→3 downshift never showed neutral at all — a clean rev-match, measured;
* when the coolant said *ready* (90 °C at t=160 s), the oil was at 62 °C —
  and it never reached 80 °C in the whole drive. The coolant gauge is not
  an oil gauge; the CSV has been recording the difference all along.

Both drives so far are in `reports/` (`drive_01` the calibration sweep,
`drive_02` the first street drive) and the test suites replay them — every
drive you log makes the tools harder to break.

### The SimHub contract (delivered before it was even asked for)

This section used to be a request: SimHub generates its
packet-identification constants inside its own UI, nobody has published the
format anywhere (we looked — GitHub-wide, zero examples; this feature is
that new), so someone with SimHub open needed to click **copy demo code**
and send the paste. By the time the request was pushed, [PR
#2](https://github.com/isaac-ranger/obd2-simhub/pull/2) had already arrived
carrying the whole thing: `simdef/obd2-simhub.simdef`, the logo, and the
generated sender in `extractor/demo_code_01.cs`. Fastest round-trip in this
project's history, and it never even left the repo.

`extractor/feed_layout.json` is now that generated struct transcribed —
Pack=1, little-endian, **101 bytes**, `GameSignature 0x51963903`,
`TelemetrySignature 0x8A3F0EE7`, port **35353** — and the test suite packs
a packet and checks it against the contract's own `ExpectedPacketLength`
and signature bytes. Three translations the generated comments dictated,
worth knowing about:

* **Throttle** goes on the wire as 0–1 (the PID speaks 0–100).
* **Gear** is an 8-byte NUL-terminated string — `N`, `1`…`6`. Never `R`:
  a learned-ratio gearbox cannot see reverse, and it would rather say
  nothing than guess.
* **Fuel** wants liters; PID `2F` gives percent. The tank size lives in
  `calibration.json` (`engine.fuel_tank_l` = 64 L / 16 gal, owner-stated —
  he flagged the liters-vs-percent conversion himself, in an email that
  crossed ours making the same point. This project has developed a habit
  of answering its own questions before they arrive).

What's left on the SimHub machine, once:

1. SimHub ≥ **9.11.5** (the feature is beta and arrived there).
2. `py extractor\obd_feed.py --register` — writes the `.simlink`
   registration pointing SimHub at the repo's `.simdef`. (The manual calls
   the file `.shlink`; the generated code says `.simlink`. We sided with
   the code that SimHub itself wrote.)
3. Activate **OBD2 SimHub** in SimHub's sim list. Auto-detection watches
   for a process named `Obd2SimHub`, which a Python script is not — either
   activate manually, or indulge in the one-liner:
   ```
   copy .venv\Scripts\python.exe .venv\Scripts\Obd2SimHub.exe
   ```
   and launch the feed with that. Windows names the process after the exe,
   SimHub sees its sim arrive, and nobody needs to know the sim is a
   Porsche with a Python interpreter in the passenger seat.
4. **Telemetry Receiver Tester** (in the definition editor) shows the live
   values if anything needs eyeballing.

### Phase 2 research notes (2026-07-30)

Findings that shape the design, banked here so they survive:

- **On a 982 (718 Cayman/Boxster), the OBD port's CAN pins are
  gateway-isolated** — essentially no broadcast data reaches them. Standard
  Mode 01 polling (this project's phase 1/2) works fine through the gateway;
  *chassis* channels (brake pressure, steering angle, wheel speeds) do not
  casually leak through. Plan accordingly.
- **A community DBC for the 982's internal CAN DRIVE bus exists:**
  [planetkris.com](https://planetkris.com/unlocking-the-porsche-718-can-bus/)
  reverse-engineered it (~40 hours): steering angle/speed, brake pressure,
  yaw rate, lateral/longitudinal G from the car's own ESP sensors, all four
  wheel speeds, clutch pedal, oil data. Requires physical access to the bus
  behind the gateway — a real decision on a warranty car, so it's an
  *optional future input*, not the plan. The extractor's `.simdef` will
  reserve fields for these channels so a tap (or any CAN source) can slot in
  without a contract change.
- **Reversible access beats tapping** (718forum, user *nothingman*, reported
  2026-07-30 — [thread](https://www.718forum.com/threads/successfully-hacked-my-718-gts-can-bus-for-racechrono-data.31918/)).
  Rather than splicing the DRIVE CAN wires under the dash, he builds a
  **pigtail**: back out the factory wires from their connector, run them
  through an adapter harness, plug it back in. Same approach the ASR/Cargraphic
  valve-control units use. Nothing is cut, and it un-does in minutes — which
  materially lowers the cost of the "should I tap the bus" decision on a car
  under warranty. This is the route to prefer if the chassis channels ever
  become worth having.
  (Investigative aside: planetkris is a Cayman-driving, CAN-dissecting
  tinkerer named Kris. This project's instigator is a Cayman-driving,
  debug-log-wielding tinkerer named Kris who bills himself "Vehicle
  Specialist / Mad Scientist." We are assured these are different people.
  The file remains open.)
- **No-splice motion data: RaceBox Mini** (~25 Hz GPS + IMU, documented BLE
  protocol, open-source client implementations exist). The right way to get
  a G-ball and real speed traces on a warranty car; the extractor can merge
  it into the same UDP feed. (Unrelated to "RaceBox SimRacing" button
  boxes — naming collision.)
- **SoloStorm** (Android) is the autocross community's established
  logger/analysis app (GPS + OBD + video + predictive timing). Complementary
  lane: SoloStorm for post-run analysis, this bridge for the live stream
  overlay.
- Extractor will log every run to CSV as a side effect — free data for
  later analysis, season stats, and co-hosts who enjoy quoting numbers.

### What standard OBD2 can and can't give you

Available on nearly every 2008+ car (CAN): RPM, speed, throttle %, engine
load, coolant temp, intake temp, MAF, manifold pressure, module voltage;
often fuel level, barometric pressure, timing advance; sometimes oil temp and
fuel rate. **Not in standard Mode 01:** brake pressure, steering angle,
clutch, gear position (usually), individual wheel speeds, tire data. Those
live in OEM-specific PIDs — the MX+'s "OEM add-ons" reach them, but only
inside the OBDLink app; they're a research topic per manufacturer, not a
day-one feature.

Realistic rates on a CAN car with an MX+: ~~a 6-PID batch at ~15–30 Hz
(≈ 100–180 channel-updates/s)~~ — **this guess was 3–6× too optimistic and the
one measurement we have says 5 Hz / 30 channel-updates/s** (see [phase 1
results](#phase-1-results--2025-718-cayman-gts-40-982)). Expect the request rate
to be the binding constraint and the batch width to be free. Pre-CAN cars
(ISO 9141 / KWP / J1850) are slower still. Run the probe; don't budget off
anyone's estimate, including this file's.

## Phase 3 — the supervisor (unattended operation)

Phase 2 answered *can the car talk to SimHub*. Phase 3 answers *can it keep
doing that for three hours while your hands are full* — which is a different
engineering problem, and the one that has to work at an event.

```
py supervisor\supervisor.py -- --port COM3
```

Everything after `--` goes to `obd_feed.py` untouched, so every flag the feed
has (or grows later) works here unchanged.

**Scope, deliberately narrow.** It owns the extractor's lifecycle and nothing
else — not OBS, not VoiceAttack, not SimHub, not the router. Those are other
people's processes with their own ideas about startup, and a supervisor that
tries to restart OBS mid-event can do more harm than the failure it's fixing.
This one babysits the piece where it can actually detect state and recover it.

**How it knows.** The feed already prints a status line every second with
`flush=True`. The supervisor reads the child's stdout and treats that line as
the liveness signal — so it needed no change to `obd_feed.py`, and the signal
means *data actually moved*, not *the process is still resident*. A process can
be alive and wedged; a printed sample cannot.

Four failures, four responses:

| what happens | what it does |
|---|---|
| feed exits (25 misses, sender death, an outright crash) | restarts it after a backoff that resets once a run has been healthy |
| feed alive but silent past `--stall-seconds` | reports `STALLED` — it may still come back on its own |
| feed alive but no data this run for `--stall-restart-seconds` (default 45) | judges the process wedged, kills it, restarts it — a serial write blocked on a dead Bluetooth handle never exits by itself (field-found 2026-08-02, MX+ unplugged mid-drive). `0` restores report-only |
| adapter was never there | reports `NO_ADAPTER`, keeps retrying forever |

**It states its own settings.** The first lines of every supervisor log name
the build (the commit, read straight out of `.git/HEAD`) and both watchdog
thresholds, and the same block rides in the status file under
`detail.supervisor`. That is there for one reason: when a log comes back from a
car in a driveway saying the watchdog didn't fire, "was it even in that copy,
and was it switched on?" has to be answerable from the attachment. A watchdog
that doesn't record its own threshold makes every field report ambiguous
between a bug and a setting.

That last row is the one that matters in pre-grid. The supervisor can't
power-cycle the adapter for you — but it never stops trying, so when you *do*
pull and replug the MX+, the feed comes back on its own and you never touch
the keyboard.

**It says so out loud.** Status goes to `status/obd2_status.json`, rewritten
atomically once a second, carrying a plain-English `summary` sentence meant to
be dropped straight into PitGirl's context. See
[`status/README.md`](status/README.md) for the field reference and — the one
rule that matters — the staleness check any reader must do, because a dead
supervisor leaves a file that still cheerfully says `LIVE`.

Tests: `python supervisor/test_supervisor.py`. They inject the four ways a real
feed fails (never starts, runs clean, dies mid-drive, dies instantly and
repeatedly) rather than asserting on internal state — the whole point of the
layer is recovering from failures nobody predicted, so the tests do the
predicting badly on purpose.

## Development without a car

**Phase 2 way:** the repo now ships its own fake car, because the emulator
below cannot answer the one thing the feed always sends (a batched multi-PID
request):

```
python extractor/fake_car.py --tcp 35000        # terminal 1
python extractor/obd_feed.py --port socket://127.0.0.1:35000   # terminal 2
```

It answers 6-PID batches the way the real 982 does — one ISO-TP reply
spanning three CAN frames — and it drives itself up and down through all six
gears using the learned constants from `calibration.json`, so the gear
readout has something honest to chase. It is a gear-shaped signal generator,
not a physics model; its idea of a downshift would not pass tech inspection.

**Phase 1 way:** the probe was originally developed against
[ELM327-emulator](https://github.com/Ircama/ELM327-emulator):

```
pip install ELM327-emulator==3.0.3 pyserial
python -m elm -n 35000 -s car          # terminal 1: fake car on TCP :35000
python probe/obd_probe.py --port socket://127.0.0.1:35000   # terminal 2
```

The version pin matters on Python 3.13 — later emulator releases fail to
install there (reported from the field, 2026-07-30).

**The emulator does not answer multi-PID requests**: the probe's 3-PID and
6-PID batch tests come back `NO DATA` against it, so its rate verdict is a
floor, not a prediction. **Settled on the car, 2026-07-30:** a real CAN car
answers the batched request in one message (an ISO-TP reply spanning three CAN
frames) — 6/6 PIDs, at exactly the same
request rate as a single PID. So the emulator understates *throughput* 6× while
getting the *request rate* right, which is the opposite of the usual
emulator-is-faster-than-hardware bias. Don't tune the poll schedule against it.

`serial_for_url` gives one code path for real COM ports and `socket://` test
targets. The parser's adversarial fixture tests (51 cases: single-frame,
batched, ISO-TP multi-frame, spaced/unspaced/lowercase, multi-ECU, negative
responses, the ELM error strings the parser knows, truncation) live in
[`probe/test_parse.py`](probe/test_parse.py):

```
python probe/test_parse.py
```

## License

MIT
