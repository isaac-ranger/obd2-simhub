# status/ — what the system says about itself

`supervisor.py` writes **`obd2_status.json`** here, once a second, for as long
as it is running. It is the one place anything else should look to find out
whether the car is actually talking to SimHub — and, when the supervisor is
run with `--gps`, whether the GPS overlay has a fix.

The file is written atomically (temp file, then rename), so a reader can open
it at any instant and never catch a half-written document. Read it as often as
you like; there is no lock to take and nothing to coordinate.

The live file is gitignored — it is machine state, not source. The example
below is committed so the VoiceAttack side can be built before the car is ever
plugged in.

## The one rule for anything that reads this file

**Check `updated_at` against `stale_after_s` before you believe `state`.**

If the file is older than `stale_after_s` seconds, the supervisor itself is
gone — crashed, killed, or never started — and everything in the file is a
photograph of the last moment it was alive. A dead supervisor leaves a file
that still cheerfully says `LIVE`, forever.

This is the oldest trap in monitoring and it can only be avoided on the reading
side, so both fields are in the file to make the check possible without knowing
anything about the program that wrote it:

```python
import json, time
s = json.load(open("status/obd2_status.json"))
if time.time() - s["updated_unix"] > s["stale_after_s"]:
    say("The OBD monitor itself is not running. I can't see the car at all.")
else:
    say(s["summary"])
```

A status light that cannot go out isn't a status light. Treating "the file is
stale" as its own reportable state is what makes the rest of it trustworthy.

## For PitGirl

`summary` is written to be spoken. It is one plain sentence with no state codes,
no jargon, and no numbers that need reading off a screen — drop it straight into
her context and she can answer "how's the car doing?" without anything else
from this file. Everything under `detail` is there if she needs to be precise.

With one leg it is the sentence it has always been: *The car is talking to
SimHub and data has been flowing for 47 minutes.* With `--gps` it is one
sentence about both, shortest form first: *OBD live for 47 minutes, GPS
crawling.* — or *OBD live for 47 minutes, GPS lost for 2 minutes.*, or *OBD
reconnecting (attempt 3), GPS live.* Each leg's own longer sentence lives on
in `gps.summary` (the GPS) — the OBD leg's is what the whole file used to say
and its parts are all in `detail`.

`healthy` is the quick boolean: `true` only when data is genuinely flowing.
It is the **OBD leg's** boolean, then and now; the GPS leg has its own at
`gps.healthy` (`true` in `LIVE` and `CRAWLING`).

## Fields

| field | meaning |
|---|---|
| `schema` | format version — currently `1`. Will only ever grow by adding fields. |
| `state` | `STARTING` · `LIVE` · `STALLED` · `RECONNECTING` · `NO_ADAPTER` · `STOPPED` — the OBD leg's state, then and now |
| `healthy` | `true` only in `LIVE` |
| `summary` | one speakable sentence |
| `updated_at` / `updated_unix` | when this file was last written — `updated_at` in local time with its offset (`2026-08-01T09:23:34-07:00`), `updated_unix` as the epoch for arithmetic |
| `stale_after_s` | older than this = the supervisor is gone. See above. |
| `detail.mode` | `live` or `replay` |
| `detail.seconds_since_data` | how long since the car last answered |
| `detail.samples_seen` | samples this supervisor has watched go by |
| `detail.restarts` | how many times the feed has been restarted |
| `detail.last_exit` | `{code, reason}` — the feed's own words on the way out |
| `detail.feed_pid` | pid of the running feed, `null` when nothing is running |
| `detail.supervisor_uptime_s` | how long the supervisor has been up |
| `legs` | which children this supervisor is running: `["obd"]` or `["obd", "gps"]` |
| `gps` | present only with `--gps`: the GPS leg's own stanza — `state`, `healthy`, `summary`, `detail` |
| `gps.state` | `STARTING` · `LIVE` · `CRAWLING` · `WAITING` · `LOST` · `STALLED` · `RECONNECTING` · `NO_ADAPTER` · `STOPPED` |
| `gps.detail.fix` | what the overlay's last status line said: `status` (`ok`/`LOST`/`waiting`), `age_s`, `speed_kmh`, `crawl`, `sats`, `accuracy_m`, `reason` |
| `gps.detail.seconds_since_line` | how long since the overlay last printed a status line — its pulse |
| `gps.detail.lines_seen` / `restarts` / `last_exit` / `pid` | as for the OBD leg |

## What the states mean, and what to do about each

| state | what happened | what you do |
|---|---|---|
| `STARTING` | connecting to the adapter | nothing, give it a few seconds |
| `LIVE` | data is flowing to SimHub | nothing — this is the one you want |
| `STALLED` | feed is running, car has gone quiet | usually the ignition; it recovers on its own when the car comes back |
| `RECONNECTING` | the feed died and is being restarted | nothing — it retries forever |
| `NO_ADAPTER` | the adapter isn't there | plug in / re-pair the MX+, then **nothing** — it will pick it up by itself |
| `STOPPED` | somebody stopped it deliberately | start it again |

The important row is `NO_ADAPTER`. The supervisor cannot power-cycle the
adapter for you, but it never stops trying — so when you do pull and replug it,
the feed comes back on its own and you never touch the keyboard.

### The GPS leg's states

Same words where they mean the same thing (`STARTING`, `STALLED`,
`RECONNECTING`, `NO_ADAPTER`, `STOPPED`). The live ones come from *reading*
the overlay's status line, not from the fact that it printed one:

| state | what happened | what you do |
|---|---|---|
| `LIVE` | fix is fresh, car is moving | nothing |
| `CRAWLING` | fix is fresh, car is parked or creeping (the overlay's own `crawl` field) | nothing — parked is not broken |
| `WAITING` | overlay is up, no fix yet this run | give the receiver a view of the sky |
| `LOST` | the overlay says the receiver is gone (its reason is in `gps.summary`), **or** it says `ok` about a fix older than `--gps-fix-age-seconds` — the port went quiet without saying so | check the XGPS is on and paired; the overlay recovers by itself and is **not** restarted for this |
| `STALLED` | no status line at all — the overlay is up but silent | nothing; past `--stall-restart-seconds` the supervisor kills and restarts it, because silence is the one thing that means wedged |

## Example

See `obd2_status.example.json` in this folder.
