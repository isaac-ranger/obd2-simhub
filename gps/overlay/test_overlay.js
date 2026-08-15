/* test_overlay.js — the trail-logic checks for overlay.js, run under node.

     node gps/overlay/test_overlay.js                 (overlay.js sits next to this file)
     node gps/overlay/test_overlay.js some/overlay.js  (check a different copy)

   Node is a TEST dependency only. The simhub itself runs on Python; this file
   is the one thing in the repo that wants anything else, and what it wants is
   node's standard library (fs, path, vm) — nothing from npm. Skip it and you
   lose these checks and nothing else.

   Exit codes: 0 every check passed; 1 at least one check failed; 2 this file
   could not load overlay.js at all (see the coupling note below) — a 2 is a
   loader problem, never a trail bug.

   How it loads the page. overlay.js is a browser IIFE with no exports, so
   this file unwraps it by text: it strips the IIFE's opening two lines and
   replaces its closing `})();` with an export of the handful of internals the
   checks drive. That is a coupling to overlay.js's OUTER SHAPE and nothing
   else — OPEN and CLOSE just below are the two anchors — and unwrap() refuses
   loudly, naming this file and the anchor, if either stops matching exactly
   once. Reformat the wrapper and this fails as "update the loader", by name;
   rename one of the exported internals and it fails the same way. A test that
   fails for the wrong reason is worse than no test on someone else's machine.

   The browser is a small shim: window.performance.now() is a clock the checks
   tick by hand, fetch() is whatever the poll check installs, the canvas
   context swallows every call, and console.warn is recorded so the checks can
   count it. Coordinates below are bench values, not a place. */
"use strict";
const fs = require("fs"), vm = require("vm"), path = require("path");

// The two anchors this loader is coupled to. Line 1 of the IIFE:
//   (function () {
//     "use strict";
// and its last line:
//   })();
const OPEN = /\(function\s*\(\)\s*\{\s*"use strict";/;
const CLOSE = /\}\)\(\);\s*$/;
// The internals the checks reach for, exported in place of the closing line.
// Every name here is a top-level binding inside overlay.js's IIFE.
const EXPORTS = "\n__exports = {" +
  " poll: poll, maybeAppendTrail: maybeAppendTrail, draw: draw," +
  " trailState: function () { return { trail: trail, trailClockMs: trailClockMs, trailClockPaused: trailClockPaused }; }," +
  " setFix: function (f, at) { fix = f; fixAtMs = at; render = { lat: f.lat, lon: f.lon, heading: 0 }; }," +
  " status: function () { return statusEl.textContent; }," +
  " consts: { TRAIL_MAX_POINTS: TRAIL_MAX_POINTS, TRAIL_MAX_MS: TRAIL_MAX_MS, TRAIL_MIN_M: TRAIL_MIN_M, POLL_MS: POLL_MS }" +
  " };";

class LoaderError extends Error {}

function unwrap(src, file) {
  const opens = src.match(new RegExp(OPEN.source, "g")) || [];
  if (opens.length !== 1) {
    throw new LoaderError("cannot load " + file + ": expected the IIFE opener " +
      "`(function () { \"use strict\";` exactly once, found " + opens.length +
      ". overlay.js's wrapper moved; update OPEN at the top of test_overlay.js.");
  }
  if (!CLOSE.test(src)) {
    throw new LoaderError("cannot load " + file + ": expected the file to end " +
      "with the IIFE closer `})();`. overlay.js's wrapper moved; update CLOSE " +
      "at the top of test_overlay.js.");
  }
  return src.replace(OPEN, '"use strict";').replace(CLOSE, EXPORTS);
}

const FILE = process.argv[2] || path.join(__dirname, "overlay.js");
let BODY;
try {
  BODY = unwrap(fs.readFileSync(FILE, "utf8"), FILE);
} catch (e) {
  console.error("test_overlay.js: " + (e && e.message ? e.message : e));
  process.exit(2);
}

/* One fresh page per call: its own clock, its own trail, its own warnings.
   `query` is the URL search string the page would have been opened with. */
function load(query) {
  let now = 1000;
  const warns = [];
  const statusEl = { textContent: "" };
  const ctxStub = new Proxy({}, { get: () => () => {} });
  const sandbox = {
    __exports: null,
    console: { warn: (m) => warns.push(m), log: () => {} },
    window: { location: { search: query }, innerWidth: 800, innerHeight: 600,
              devicePixelRatio: 1, addEventListener: () => {},
              performance: { now: () => now }, requestAnimationFrame: () => {} },
    document: { documentElement: { style: {} }, body: { style: {} },
                getElementById: (id) => id === "map" ? { getContext: () => ctxStub, style: {} }
                                       : id === "status" ? statusEl : { style: {} } },
    URLSearchParams,   // a node global, not an ECMAScript builtin, so the vm context needs it handed in
    fetch: (...a) => sandbox.__fetch ? sandbox.__fetch(...a) : new Promise(() => {}),
    setInterval: () => {},
  };
  vm.createContext(sandbox);
  try {
    vm.runInContext(BODY, sandbox, { filename: FILE });
  } catch (e) {
    if (e instanceof ReferenceError) {
      throw new LoaderError("cannot load " + FILE + ": " + e.message + ". If that " +
        "name is one of EXPORTS at the top of test_overlay.js, overlay.js renamed " +
        "an internal this loader reaches for; update EXPORTS.");
    }
    throw e;
  }
  if (!sandbox.__exports) {
    throw new LoaderError("cannot load " + FILE + ": CLOSE matched but the export " +
      "never ran — the closer is not the last statement of the IIFE?");
  }
  return { api: sandbox.__exports, tick: (ms) => { now += ms; }, warns,
           setFetch: (f) => { sandbox.__fetch = f; } };
}

let passed = 0, failed = 0;
function ok(name, cond, detail) {
  console.log((cond ? "PASS  " : "FAIL  ") + name + (cond ? "" : "  " + detail));
  if (cond) passed++; else failed++;
}
const LAT = 32.8, LON = -117.2, STEP = 0.0001; // ~11 m per step; bench values, not a place
const NOMINAL_FIX_MS = 100; // the XGPS160 speaks at ~10 Hz; the page appends per new fix

async function main() {
  // 1. smooth view: crawl=true pauses the clock and gates appends
  {
    const b = load("?smooth=on");
    b.api.maybeAppendTrail(LAT, LON, false); b.tick(100);
    b.api.maybeAppendTrail(LAT + STEP, LON, false); b.tick(100);
    const t1 = b.api.trailState();
    ok("smooth: moving fixes append", t1.trail.length === 2, JSON.stringify(t1));
    const clock1 = t1.trailClockMs;
    for (let i = 0; i < 50; i++) { b.api.maybeAppendTrail(LAT + 2 * STEP + i * STEP, LON, true); b.tick(100); }
    const t2 = b.api.trailState();
    ok("smooth: crawl=true appends nothing even when the fix moves", t2.trail.length === 2, "len=" + t2.trail.length);
    ok("smooth: crawl=true freezes the trail clock", t2.trailClockMs === clock1 && t2.trailClockPaused === true, JSON.stringify(t2));
    b.api.maybeAppendTrail(LAT + 60 * STEP, LON, false); b.tick(100);
    const t3 = b.api.trailState();
    ok("smooth: crawl=false resumes and appends", t3.trail.length === 3 && t3.trailClockPaused === false, JSON.stringify(t3));
    ok("bench: no crawl warning when the field is a boolean", b.warns.length === 0, JSON.stringify(b.warns));
  }
  // 2. the ceiling: what it is, and that it is a ceiling and not the window
  {
    const b = load("?smooth=off");
    const c = b.api.consts;
    ok("ceiling: TRAIL_MAX_POINTS is 6000, the number the README promises", c.TRAIL_MAX_POINTS === 6000, "TRAIL_MAX_POINTS=" + c.TRAIL_MAX_POINTS);
    ok("ceiling: it never binds before the ten-minute clock at the nominal 10 Hz (points x 100 ms >= TRAIL_MAX_MS)",
       c.TRAIL_MAX_POINTS * NOMINAL_FIX_MS >= c.TRAIL_MAX_MS,
       c.TRAIL_MAX_POINTS + " x " + NOMINAL_FIX_MS + " ms = " + (c.TRAIL_MAX_POINTS * NOMINAL_FIX_MS / 60000) + " min < " + (c.TRAIL_MAX_MS / 60000) + " min");
    // roll for 2 s so the clock reads something other than zero at the pause
    b.api.maybeAppendTrail(LAT, LON, false); b.tick(100);
    for (let i = 1; i <= 20; i++) { b.api.maybeAppendTrail(LAT + i * STEP, LON, false); b.tick(100); }
    // then park: raw view keeps appending while crawl=true, more than the ceiling can hold
    for (let i = 0; i < c.TRAIL_MAX_POINTS + 1000; i++) { b.api.maybeAppendTrail(LAT + 20 * STEP, LON, true); b.tick(100); }
    const t = b.api.trailState();
    ok("raw: appends while crawl=true (the scribble is the point)", t.trail.length > 21, "len=" + t.trail.length);
    ok("raw: the ceiling holds, never one point more", t.trail.length === c.TRAIL_MAX_POINTS, "len=" + t.trail.length);
    ok("raw: the clock is still paused while crawling", t.trailClockPaused === true && t.trailClockMs === 2000, JSON.stringify({ paused: t.trailClockPaused, clock: t.trailClockMs }));
    const pausedTMs = t.trailClockMs;
    ok("raw: points appended while paused all carry the paused tMs", t.trail.every(p => p.tMs === pausedTMs), "");
    // Roll for just over ten minutes of trail time at 5 Hz: 3050 fixes x 200 ms.
    // Fewer new points than the ceiling on purpose — if the parked points
    // vanish it is because they AGED OUT, not because the ceiling pushed
    // them off the front (a ceiling-only overlay would keep 2950 of them).
    for (let i = 0; i < 3050; i++) { b.api.maybeAppendTrail(LAT + 21 * STEP + i * STEP, LON, false); b.tick(200); }
    const t2 = b.api.trailState();
    ok("raw: after ten minutes of rolling the parked scribble has expired by age, not by the ceiling",
       t2.trail.every(p => p.tMs !== pausedTMs), "parked points surviving: " + t2.trail.filter(p => p.tMs === pausedTMs).length);
    // clock at the end: 2000 + 100 (first roll step) + 3049 x 200 = 611,900; cutoff 11,900;
    // rolling points at 2100, 2300, ... — the 49 below the cutoff have aged out too
    ok("raw: exactly the rolling points inside the window remain (3001)", t2.trail.length === 3001, "len=" + t2.trail.length + " clock=" + t2.trailClockMs);
    ok("raw: and the ceiling still holds", t2.trail.length <= c.TRAIL_MAX_POINTS, "len=" + t2.trail.length);
  }
  // 3. absent crawl: no pause, wall-clock aging, one warning
  {
    const b = load("");
    b.api.maybeAppendTrail(LAT, LON, undefined); b.tick(100);
    for (let i = 1; i < 20; i++) { b.api.maybeAppendTrail(LAT + i * STEP, LON, undefined); b.tick(100); }
    const t = b.api.trailState();
    ok("absent: never paused", t.trailClockPaused === false, JSON.stringify(t));
    ok("absent: clock advanced by wall time (1900 ms)", t.trailClockMs === 1900, "clock=" + t.trailClockMs);
    ok("absent: exactly one console.warn", b.warns.length === 1 && /no crawl field/.test(b.warns[0]), JSON.stringify(b.warns));
    b.api.setFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 5, heading_deg: 0, accuracy_m: null, sats: null }, 2000);
    b.api.draw();
    ok("absent: HUD reads crawl?", /km\/h  crawl\?  hdg/.test(b.api.status()), b.api.status());
  }
  // 4. HUD with the field
  {
    const b = load("?hud=on");
    b.api.setFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 0.4, heading_deg: 0, crawl: true, accuracy_m: null, sats: null }, 0);
    b.api.draw();
    ok("hud: crawl=true prints crawl after the speed", /0\.4 km\/h  crawl  hdg/.test(b.api.status()), b.api.status());
    b.api.setFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 40, heading_deg: 0, crawl: false, accuracy_m: null, sats: null }, 0);
    b.api.draw();
    ok("hud: crawl=false prints nothing extra", /40\.0 km\/h  hdg/.test(b.api.status()), b.api.status());
  }
  // 5. trailPause=off: crawl=true does not pause
  {
    const b = load("?trailPause=off");
    b.api.maybeAppendTrail(LAT, LON, true); b.tick(100);
    b.api.maybeAppendTrail(LAT + STEP, LON, true); b.tick(100);
    const t = b.api.trailState();
    ok("trailPause=off: crawl=true does not pause the clock", t.trailClockPaused === false && t.trailClockMs === 100 && t.trail.length === 2, JSON.stringify(t));
  }
  // 6. poll() wiring: the call site must hand data.crawl (not speed) to the trail
  {
    const b = load("?smooth=on");
    let payload = null;
    b.setFetch(() => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }));
    const settle = () => new Promise(r => setImmediate(r));
    // parked, crawl=true, but speed_kmh 0 — a wiring mistake passing speed would read as "not paused"
    payload = { ok: true, t: 1, lat: LAT, lon: LON, speed_kmh: 0.0, crawl: true, heading_deg: 0 };
    b.api.poll(); await settle(); b.tick(100);
    payload = { ok: true, t: 2, lat: LAT + STEP, lon: LON, speed_kmh: 0.0, crawl: true, heading_deg: 0 };
    b.api.poll(); await settle(); b.tick(100);
    payload = { ok: true, t: 3, lat: LAT + 2 * STEP, lon: LON, speed_kmh: 0.0, crawl: true, heading_deg: 0 };
    b.api.poll(); await settle(); b.tick(100);
    let t = b.api.trailState();
    ok("poll: crawl=true from /live pauses the clock and gates the smoothed append",
       t.trailClockPaused === true && t.trail.length === 1, JSON.stringify(t));
    // moving, crawl=false, speed 50: a wiring mistake passing speed (50, not boolean) would never pause AND would warn
    payload = { ok: true, t: 4, lat: LAT + 3 * STEP, lon: LON, speed_kmh: 50.0, crawl: false, heading_deg: 0 };
    b.api.poll(); await settle(); b.tick(100);
    t = b.api.trailState();
    ok("poll: crawl=false from /live resumes and appends", t.trailClockPaused === false && t.trail.length === 2, JSON.stringify(t));
    ok("poll: a boolean field arrived, so no 'no crawl field' warning", b.warns.length === 0, JSON.stringify(b.warns));
  }
}

main().then(() => {
  const total = passed + failed;
  console.log(failed ? failed + " of " + total + " FAILED" : "all " + total + " checks passed");
  process.exit(failed ? 1 : 0);
}, (e) => {
  console.error("test_overlay.js: " + (e && e.message ? e.message : e));
  if (!(e instanceof LoaderError) && e && e.stack) console.error(e.stack);
  process.exit(2);
});
