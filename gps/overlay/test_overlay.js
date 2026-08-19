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
   rename any internal the export list reaches for and it fails the same way,
   at load, naming the internal — never part-way through a run. A test that
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
const EXPORTS = "\nvoid [fix, fixAtMs, render, trail, trailClockMs, trailClockPaused, statusEl, lonLatToTileFrac, tileToLatLon, zoomForMPerPx, tileUrl, tileCache, mapOn, mapDraw, mapName, visibleTileRange, advanceRender, arrowHeadingDeg, headingCamCoupled, headingCamTauMs, getTile];" + // touch every lazily-used name NOW, so a rename fails at load, not mid-run
  "\n__exports = {" +
  " poll: poll, maybeAppendTrail: maybeAppendTrail, draw: draw," +
  " trailState: function () { return { trail: trail, trailClockMs: trailClockMs, trailClockPaused: trailClockPaused }; }," +
  " setFix: function (f, at) { var h = f.heading_deg || 0; fix = f; fixAtMs = at; render = { lat: f.lat, lon: f.lon, heading: h, cameraHeading: h }; }," +
  " pokeFix: function (f) { fix = f; }," +
  " advanceRender: advanceRender," +
  " arrowHeadingDeg: arrowHeadingDeg," +
  " renderState: function () { return render; }," +
  " headingCam: function () { return { coupled: headingCamCoupled, tauMs: headingCamTauMs }; }," +
  " status: function () { return statusEl.textContent; }," +
  " consts: { TRAIL_MAX_POINTS: TRAIL_MAX_POINTS, TRAIL_MAX_MS: TRAIL_MAX_MS, TRAIL_MIN_M: TRAIL_MIN_M, POLL_MS: POLL_MS, TILE_PX: TILE_PX, TILE_MAX_Z: TILE_MAX_Z, TILE_MAX_COUNT: TILE_MAX_COUNT, TILE_FAIL_RETRY_MS: TILE_FAIL_RETRY_MS, TILE_FAIL_RETRY_MAX_MS: TILE_FAIL_RETRY_MAX_MS, EQUATOR_M: EQUATOR_M, HEADING_TAU_MS: HEADING_TAU_MS, DEFAULT_HEADING_CAM_TAU_MS: DEFAULT_HEADING_CAM_TAU_MS, ARROW_RESIDUAL_MAX_DEG: ARROW_RESIDUAL_MAX_DEG }," +
  " lonLatToTileFrac: lonLatToTileFrac, tileToLatLon: tileToLatLon," +
  " zoomForMPerPx: zoomForMPerPx, tileUrl: tileUrl, getTile: getTile," +
  " visibleTileRange: visibleTileRange," +
  " mapFlags: function () { return { mapOn: mapOn, mapDraw: mapDraw, style: mapOn ? mapName : null }; }," +
  " tiles: function () { return tileCache; }" +
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
   `query` is the URL search string the page would have been opened with;
   `shim`, if given, is merged into the window before the page runs — for
   the one check that wants a browser global the default shim leaves out
   (an Image, so onload/onerror can be fired by hand). */
function load(query, shim) {
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
  if (shim) Object.assign(sandbox, shim);
  vm.createContext(sandbox);
  try {
    vm.runInContext(BODY, sandbox, { filename: FILE });
  } catch (e) {
    if (e && e.name === "ReferenceError") { // vm errors are another realm's — instanceof would never be true
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
    // the 3 m skip: 0.00002 deg of latitude is ~2.2 m, 0.00004 is ~4.4 m.
    // (trailState() hands back the live array, so take the lengths as numbers.)
    b.api.maybeAppendTrail(LAT + 60 * STEP + 0.00002, LON, false); b.tick(100);
    const lenAfter2m = b.api.trailState().trail.length;
    b.api.maybeAppendTrail(LAT + 60 * STEP + 0.00004, LON, false); b.tick(100);
    const lenAfter4m = b.api.trailState().trail.length;
    ok("smooth: a moving fix under 3 m from the last point is skipped, one over 3 m is kept",
       lenAfter2m === 3 && lenAfter4m === 4, "after 2.2 m: " + lenAfter2m + ", after 4.4 m: " + lenAfter4m);
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
    ok("raw: points appended while paused all carry the paused tMs", t.trail.every(p => p.tMs === pausedTMs), "points not at the paused tMs: " + t.trail.filter(p => p.tMs !== pausedTMs).length);
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
  // 7. slippy-map math (slice 1: north-up Stadia tiles, off by default)
  {
    const off = load("");
    ok("map: default is off", off.api.mapFlags().mapOn === false && off.api.mapFlags().style === null, JSON.stringify(off.api.mapFlags()));
    off.api.setFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 40, heading_deg: 0, crawl: false, accuracy_m: null, sats: null }, 0);
    off.api.draw();
    ok("map: default draw fetches no tiles", Object.keys(off.api.tiles()).length === 0, JSON.stringify(Object.keys(off.api.tiles())));
    ok("map: default HUD has no map= token", !/map=/.test(off.api.status()), off.api.status());

    const origin = load("?map=alidade");
    const t0 = origin.api.lonLatToTileFrac(0, 0, 4);
    ok("map: lon 0 lat 0 z=4 sits at the centre tile (8, 8)", Math.abs(t0.x - 8) < 1e-9 && Math.abs(t0.y - 8) < 1e-9, JSON.stringify(t0));
    const nw = origin.api.tileToLatLon(4, 8, 8);
    ok("map: tile (4,8,8) NW corner is the equator at lon 0", Math.abs(nw.lat) < 1e-9 && Math.abs(nw.lon) < 1e-9, JSON.stringify(nw));
    const tLon = origin.api.lonLatToTileFrac(0, -180, 3);
    ok("map: lon -180 is tile x=0", Math.abs(tLon.x) < 1e-9, "x=" + tLon.x);
    const mpp = origin.api.consts.EQUATOR_M / (origin.api.consts.TILE_PX * Math.pow(2, 10));
    ok("map: zoomForMPerPx at the equator recovers z=10", origin.api.zoomForMPerPx(mpp, 0) === 10, "z=" + origin.api.zoomForMPerPx(mpp, 0) + " mpp=" + mpp);
    ok("map: zoomForMPerPx clamps to 1..20", origin.api.zoomForMPerPx(1e-9, 0) === 20 && origin.api.zoomForMPerPx(1e9, 0) === 1, "hi=" + origin.api.zoomForMPerPx(1e-9, 0) + " lo=" + origin.api.zoomForMPerPx(1e9, 0));
    ok("map: tileUrl has no key when none was given", origin.api.tileUrl(17, 1, 2) === "https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/17/1/2.png", origin.api.tileUrl(17, 1, 2));
    origin.api.setFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 40, heading_deg: 0, crawl: false, accuracy_m: null, sats: null }, 0);
    origin.api.draw();
    ok("map: alidade north-up HUD names the layer", /map=alidade z\d+/.test(origin.api.status()), origin.api.status());
    ok("map: stadia draw asks for at least one tile", Object.keys(origin.api.tiles()).length > 0, "n=" + Object.keys(origin.api.tiles()).length);
    ok("map: a 200 m view stays under the fetch cap", Object.keys(origin.api.tiles()).length <= origin.api.consts.TILE_MAX_COUNT, "n=" + Object.keys(origin.api.tiles()).length);

    const keyed = load("?map=alidade&stadiaKey=test-key");
    ok("map: stadiaKey is appended, not baked into the path",
       keyed.api.tileUrl(18, 3, 4).indexOf("?api_key=test-key") >= 0 &&
       keyed.api.tileUrl(18, 3, 4).indexOf("/test-key") < 0,
       keyed.api.tileUrl(18, 3, 4));

    const hdg = load("?map=alidade&up=heading");
    ok("map: heading-up draws tiles too", hdg.api.mapFlags().mapOn === true && hdg.api.mapFlags().style === "alidade", JSON.stringify(hdg.api.mapFlags()));
    hdg.api.setFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 40, heading_deg: 90, crawl: false, accuracy_m: null, sats: null }, 0);
    hdg.api.draw();
    ok("map: heading-up fetches tiles", Object.keys(hdg.api.tiles()).length > 0, "n=" + Object.keys(hdg.api.tiles()).length);
    ok("map: heading-up HUD names the layer", /map=alidade z\d+/.test(hdg.api.status()) && /up=heading/.test(hdg.api.status()), hdg.api.status());
    const mppView = 200 / 600;
    const rN = origin.api.visibleTileRange(LAT, LON, 800, 600, mppView, 18, 0);
    const r45 = origin.api.visibleTileRange(LAT, LON, 800, 600, mppView, 18, Math.PI / 4);
    const nN = (rN.x1 - rN.x0 + 1) * (rN.y1 - rN.y0 + 1);
    const n45 = (r45.x1 - r45.x0 + 1) * (r45.y1 - r45.y0 + 1);
    ok("map: a 45° heading-up AABB is at least as large as north-up", n45 >= nN, "north=" + nN + " 45=" + n45);
    const r0h = origin.api.visibleTileRange(LAT, LON, 800, 600, mppView, 18, 0);
    const rNup = origin.api.visibleTileRange(LAT, LON, 800, 600, mppView, 18);
    ok("map: omitted heading matches heading 0 (north-up range)",
       r0h.x0 === rNup.x0 && r0h.x1 === rNup.x1 && r0h.y0 === rNup.y0 && r0h.y1 === rNup.y1,
       JSON.stringify({ r0h: r0h, rNup: rNup }));
    const toner = load("?map=toner");
    ok("map: toner uses the Stamen Toner slug",
       toner.api.tileUrl(17, 1, 2).indexOf("/stamen_toner/") >= 0 && toner.api.mapFlags().style === "toner",
       toner.api.tileUrl(17, 1, 2) + " " + JSON.stringify(toner.api.mapFlags()));
    const terrain = load("?map=terrain");
    ok("map: terrain uses the Stamen Terrain slug",
       terrain.api.tileUrl(17, 1, 2).indexOf("/stamen_terrain/") >= 0 && terrain.api.mapFlags().style === "terrain",
       terrain.api.tileUrl(17, 1, 2) + " " + JSON.stringify(terrain.api.mapFlags()));
    const alias = load("?map=stadia");
    ok("map: stadia is an alias for alidade",
       alias.api.mapFlags().style === "alidade" &&
       alias.api.tileUrl(17, 1, 2).indexOf("/alidade_smooth_dark/") >= 0,
       JSON.stringify(alias.api.mapFlags()) + " " + alias.api.tileUrl(17, 1, 2));
    const unknown = load("?map=nope");
    ok("map: an unknown style stays off", unknown.api.mapFlags().mapOn === false, JSON.stringify(unknown.api.mapFlags()));

    const retry = load("?map=alidade");
    const first = retry.api.getTile(1, 0, 0);
    ok("map: a failed tile is stamped, not a bare fail",
       first.status === "fail" && first.failedAt != null, JSON.stringify(first));
    ok("map: a failed tile is not retried on the next frame",
       retry.api.getTile(1, 0, 0) === first, "got a new record before the retry window");
    retry.tick(retry.api.consts.TILE_FAIL_RETRY_MS - 1);
    ok("map: still the same hole just inside the retry window",
       retry.api.getTile(1, 0, 0) === first, "retried early");
    retry.tick(2);
    const second = retry.api.getTile(1, 0, 0);
    ok("map: a failed tile is asked for again after a few seconds",
       second !== first && second.status === "fail" && second.failedAt > first.failedAt,
       JSON.stringify({ first: first.failedAt, second: second && second.failedAt }));

    // The retry's other arm: a tile that KEEPS failing is asked about less
    // and less — the 4 s doubles per failure up to a minute and holds there,
    // per tile, and the console says so once when the first tile reaches
    // the ceiling. Never a hard stop: the same tiles come round on a lap.
    const back = load("?map=alidade");
    ok("backoff: the ceiling is a minute", back.api.consts.TILE_FAIL_RETRY_MAX_MS === 60000,
       "max=" + back.api.consts.TILE_FAIL_RETRY_MAX_MS);
    const waits = [], warnsAt = [];
    let hole = back.api.getTile(2, 1, 1);
    for (let i = 0; i < 6; i++) {
      waits.push(hole.retryAt - hole.failedAt);
      warnsAt.push(back.warns.length);
      back.tick(hole.retryAt - hole.failedAt - 1);
      if (back.api.getTile(2, 1, 1) !== hole) { waits.push("early"); break; }
      back.tick(1);
      const next = back.api.getTile(2, 1, 1);
      if (next === hole) { waits.push("late"); break; }
      hole = next;
    }
    ok("backoff: the wait doubles per failure, 4 s to a minute, then holds",
       JSON.stringify(waits) === JSON.stringify([4000, 8000, 16000, 32000, 60000, 60000]), JSON.stringify(waits));
    ok("backoff: the failure count rides across the re-ask", hole.fails === 7, "fails=" + hole.fails);
    ok("backoff: the ceiling is announced once, when the first tile reaches it, not before",
       JSON.stringify(warnsAt) === JSON.stringify([1, 1, 1, 1, 2, 2]) && back.warns.length === 2 &&
       /once a minute/.test(back.warns[1]), JSON.stringify({ warnsAt: warnsAt, warns: back.warns }));
    const other = back.api.getTile(2, 1, 2);
    ok("backoff: a different tile starts at 4 s again (per tile, not global)",
       other.fails === 1 && other.retryAt - other.failedAt === 4000, JSON.stringify(other));
    let hole2 = other;
    for (let i = 0; i < 5; i++) { back.tick(hole2.retryAt - hole2.failedAt); hole2 = back.api.getTile(2, 1, 2); }
    ok("backoff: a second tile reaching the ceiling does not repeat the line",
       hole2.retryAt - hole2.failedAt === 60000 && back.warns.length === 2, JSON.stringify(back.warns));

    // Same arm on the browser path: a real-shaped Image whose onerror/onload
    // the check fires by hand, so the count is seen to ride across Images
    // and a load after the ceiling is seen to say so and re-arm the line.
    const images = [];
    class FakeImage { set src(v) { this.url = v; images.push(this); } get src() { return this.url; } }
    const live = load("?map=alidade", { Image: FakeImage });
    const r1 = live.api.getTile(3, 1, 1);
    ok("backoff (browser path): a fresh tile is loading and one Image was asked",
       r1.status === "loading" && images.length === 1 && /\/3\/1\/1\.png$/.test(images[0].src),
       JSON.stringify({ status: r1.status, images: images.length, src: images[0] && images[0].src }));
    ok("backoff (browser path): the same ask before onerror is the same record, no second Image",
       live.api.getTile(3, 1, 1) === r1 && images.length === 1, "images=" + images.length);
    images[0].onerror();
    ok("backoff (browser path): onerror stamps the record and schedules the 4 s ask",
       r1.status === "fail" && r1.fails === 1 && r1.retryAt - r1.failedAt === 4000 && live.warns.length === 1,
       JSON.stringify(r1));
    let cur = r1;
    for (let i = 0; i < 4; i++) {
      live.tick(cur.retryAt - cur.failedAt);
      cur = live.api.getTile(3, 1, 1);
      images[images.length - 1].onerror();
    }
    ok("backoff (browser path): the fifth failure reaches the minute, five Images asked, one line said",
       cur.fails === 5 && cur.retryAt - cur.failedAt === 60000 && images.length === 5 && live.warns.length === 2,
       JSON.stringify({ fails: cur.fails, images: images.length, warns: live.warns }));
    const fresh = live.api.getTile(3, 9, 9);
    images[images.length - 1].onload();
    ok("backoff (browser path): a tile that never failed loading beside the dead one is not 'loading again'",
       fresh.status === "ok" && live.warns.length === 2, JSON.stringify(live.warns));
    live.tick(60000);
    cur = live.api.getTile(3, 1, 1);
    images[images.length - 1].onload();
    ok("backoff (browser path): a load of the tile that HAD been failing says the tiles are back, once, and clears the count",
       cur.status === "ok" && cur.fails === 0 && live.warns.length === 3 && /loading again/.test(live.warns[2]),
       JSON.stringify({ status: cur.status, fails: cur.fails, warns: live.warns }));
    ok("backoff (browser path): a loaded tile stays loaded", live.api.getTile(3, 1, 1) === cur && images.length === 7,
       "images=" + images.length);
    let again = live.api.getTile(3, 2, 2);
    for (let i = 0; i < 5; i++) {
      images[images.length - 1].onerror();
      if (i < 4) { live.tick(again.retryAt - again.failedAt); again = live.api.getTile(3, 2, 2); }
    }
    ok("backoff (browser path): the next outage gets its own line (re-armed by the load)",
       again.fails === 5 && live.warns.length === 4 && /once a minute/.test(live.warns[3]), JSON.stringify(live.warns));
  }
  // 8. heading-up camera tau: world follows a slow heading, arrow the residual
  {
    const def = load("?up=heading");
    ok("headingTau: default is 1200 ms, not coupled",
       def.api.headingCam().tauMs === 1200 && def.api.headingCam().coupled === false,
       JSON.stringify(def.api.headingCam()));
    ok("headingTau: default constant matches the README",
       def.api.consts.DEFAULT_HEADING_CAM_TAU_MS === 1200, "DEFAULT=" + def.api.consts.DEFAULT_HEADING_CAM_TAU_MS);
    ok("headingTau: residual clamp is 15°", def.api.consts.ARROW_RESIDUAL_MAX_DEG === 15, "max=" + def.api.consts.ARROW_RESIDUAL_MAX_DEG);
    ok("headingTau: a 10° body lead is shown on the arrow", def.api.arrowHeadingDeg(10, 0) === 10, "arrow=" + def.api.arrowHeadingDeg(10, 0));
    ok("headingTau: a 90° body lead is clamped to 15°", def.api.arrowHeadingDeg(90, 0) === 15, "arrow=" + def.api.arrowHeadingDeg(90, 0));
    ok("headingTau: wrap 350→10 is a positive residual, clamped to 15° not −340", def.api.arrowHeadingDeg(10, 350) === 15, "arrow=" + def.api.arrowHeadingDeg(10, 350));

    const north = load("");
    ok("headingTau: north-up arrow is body heading, not a residual", north.api.arrowHeadingDeg(45, 0) === 45, "arrow=" + north.api.arrowHeadingDeg(45, 0));

    const glued = load("?up=heading&headingTau=0");
    ok("headingTau=0: coupled", glued.api.headingCam().coupled === true, JSON.stringify(glued.api.headingCam()));
    ok("headingTau=0: arrow stays screen-up", glued.api.arrowHeadingDeg(90, 0) === 0, "arrow=" + glued.api.arrowHeadingDeg(90, 0));
    const off = load("?up=heading&headingTau=off");
    ok("headingTau=off: coupled", off.api.headingCam().coupled === true, JSON.stringify(off.api.headingCam()));
    const fast = load("?up=heading&headingTau=50");
    ok("headingTau: a tau faster than body is clamped to HEADING_TAU_MS",
       fast.api.headingCam().tauMs === fast.api.consts.HEADING_TAU_MS && fast.api.headingCam().coupled === false,
       JSON.stringify(fast.api.headingCam()));

    const slow = load("?up=heading&headingTau=2000");
    const live = { ok: true, lat: LAT, lon: LON, speed_kmh: 40, heading_deg: 0, crawl: false, accuracy_m: null, sats: null };
    slow.api.setFix(live, 1000);
    slow.api.pokeFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 40, heading_deg: 90, crawl: false, accuracy_m: null, sats: null });
    let now = 1000;
    for (let i = 0; i < 5; i++) {
      now += 33;
      slow.api.advanceRender(now, 33);
    }
    const r = slow.api.renderState();
    ok("headingTau=2000: after 165 ms body has moved well toward 90", r.heading > 40 && r.heading < 90, "body=" + r.heading);
    ok("headingTau=2000: camera lags body (slow world, fast arrow)",
       r.cameraHeading < r.heading - 20, "body=" + r.heading + " cam=" + r.cameraHeading);
    slow.api.draw();
    ok("headingTau: heading-up HUD prints cam as well as hdg",
       /hdg /.test(slow.api.status()) && /cam /.test(slow.api.status()), slow.api.status());

    const coupledLive = load("?up=heading&headingTau=0");
    coupledLive.api.setFix(live, 1000);
    coupledLive.api.pokeFix({ ok: true, lat: LAT, lon: LON, speed_kmh: 40, heading_deg: 90, crawl: false, accuracy_m: null, sats: null });
    now = 1000;
    for (let i = 0; i < 5; i++) {
      now += 33;
      coupledLive.api.advanceRender(now, 33);
    }
    const rc = coupledLive.api.renderState();
    ok("headingTau=0: camera stays locked to body",
       Math.abs(rc.cameraHeading - rc.heading) < 1e-9, "body=" + rc.heading + " cam=" + rc.cameraHeading);
    coupledLive.api.draw();
    ok("headingTau=0: HUD has no cam token", !/cam /.test(coupledLive.api.status()), coupledLive.api.status());
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
