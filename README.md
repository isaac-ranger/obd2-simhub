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
| 1 | **`probe/obd_probe.py`** — measure what *your* car + adapter can actually deliver | ✅ ready |
| 2 | **Extractor** — poll loop + SimHub UDP feed, built around the probe's findings | designed, pending phase 1 results |

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

The probe is **read-only**: Mode 01 data requests and ELM configuration only.
It never writes to the ECU and never touches trouble codes.

It reports: adapter identity (ELM vs. STN firmware), detected protocol,
number of responding ECUs, which dashboard-relevant PIDs the car advertises,
live decoded values as a sanity check, and measured request rates — single-PID
and batched (on CAN, up to 6 PIDs ride one request; that batching is the
difference between choppy and smooth gauges).

## Phase 2 — the extractor (design)

Built after phase 1 numbers are in:

- **Tiered polling.** Fast tier (RPM, speed, throttle) batched at the max rate
  the car sustains; slow tier (coolant, intake temp, fuel level, voltage)
  refreshed every few seconds. OBD2 is request/response — bandwidth is spent
  where the needles move.
- **Decoupled UDP send.** The feed to SimHub runs at a fixed rate at or above
  SimHub's stated 60 Hz minimum, sending last-known values, so SimHub's
  rendering never stutters on OBD latency; optional light interpolation on
  RPM/speed.
- **`.simdef` contract** authored in SimHub's built-in definition editor
  (Settings → enable *game definition authoring tools*), fields matching what
  the car actually provides. SimHub generates the exact C# packet struct
  ("copy demo code"), which drops into the extractor.
- **Derived channels** where OBD2 has gaps: gear ≈ f(RPM, speed, final-drive
  ratios), for example.

### What standard OBD2 can and can't give you

Available on nearly every 2008+ car (CAN): RPM, speed, throttle %, engine
load, coolant temp, intake temp, MAF, manifold pressure, module voltage;
often fuel level, barometric pressure, timing advance; sometimes oil temp and
fuel rate. **Not in standard Mode 01:** brake pressure, steering angle,
clutch, gear position (usually), individual wheel speeds, tire data. Those
live in OEM-specific PIDs — the MX+'s "OEM add-ons" reach them, but only
inside the OBDLink app; they're a research topic per manufacturer, not a
day-one feature.

Realistic rates on a CAN car with an MX+: a 6-PID batch at ~15–30 Hz
(≈ 100–180 channel-updates/s) is a sensible expectation — plenty for smooth
gauges. Pre-CAN cars (ISO 9141 / KWP / J1850): more like 4–10 PID reads/s
total; workable with tiered polling, but set expectations accordingly.

## Development without a car

The probe is developed and tested against
[ELM327-emulator](https://github.com/Ircama/ELM327-emulator):

```
pip install ELM327-emulator pyserial
python -m elm -n 35000 -s car          # terminal 1: fake car on TCP :35000
python probe/obd_probe.py --port socket://127.0.0.1:35000   # terminal 2
```

`serial_for_url` gives one code path for real COM ports and `socket://` test
targets. The parser's adversarial fixture tests (48 cases: single-frame,
batched, ISO-TP multi-frame, spaced/unspaced/lowercase, multi-ECU, negative
responses, the full ELM error vocabulary, truncation) live in
[`probe/test_parse.py`](probe/test_parse.py):

```
python probe/test_parse.py
```

## A note on "OBDwiz on GitHub"

OBDLink's **OBDwiz** software is closed-source. Official downloads come only
from the vendor sites — [obdlink.com](https://www.obdlink.com/) /
scantool.net (OBD Solutions) or the developer's
[obdsoftware.net](https://www.obdsoftware.net/) (OCTech). It has **no
official GitHub repository** — repos offering OBDwiz downloads (typically a
README with a Dropbox/password-protected archive) are malware lures. Don't
run them.

## License

MIT
