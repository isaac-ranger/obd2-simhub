/* Ego-centered GPS trail. Defaults are magic numbers for v1; some already
   accept URL params (?meters=, ?metersMax=, ?up=, ?smooth=, ?bg=, ?hud=, ?trailPause=).

   ?up=north   (default) — map North-up; arrow rotates with heading
   ?up=heading           — arrow mostly up; map/trail follow a slower camera heading
   ?headingTau=1200      — camera heading time constant in ms (heading-up). 0 or off:
                           old behaviour, arrow glued up, world uses body heading
   ?smooth=off           — draw /live's raw_lat/raw_lon, no bridging, no trail skip
   ?meters=200           — floor: metres across the short edge (default 200)
   ?metersMax=1000       — cap: ease out this far to keep the trail on screen
                           (equal to ?meters= locks the old fixed scale)
   ?bg=transparent       — default; OBS composites the trail over video
   ?bg=grey              — solid bench so the dark trail edge is visible
   ?bg=dark              — original near-black stage
   ?hud=on               — show the status line (hidden by default)
   ?trailPause=off       — age the trail by wall clock even while parked
                           (default pauses the ten-minute window while the
                           server says crawl)
   ?map=off|alidade|toner|terrain
                           — Stadia raster under the trail (default off).
                             alidade = Smooth Dark; toner / terrain = Stamen.
                             stadia and on are aliases for alidade.
   ?stadiaKey=           — optional API key; try 127.0.0.1 without it first

   Rates: the receiver speaks at ~10 Hz and the poll matches it, but the
   draw loop runs on requestAnimationFrame capped near 30 Hz. Repainting
   the same fix faster would look identical, so the frames between samples
   are earned: heading and position ease toward the newest fix, and a short
   dead-reckoning step carries the car forward along its last known course.
   Every new fix corrects it, so error cannot accumulate past one sample.
   If `t` goes stale past a second, or /live says ok:false, the HUD reads
   "signal lost Ns" instead of the last speed.

   Driving-scale note: this receiver pins one coordinate at a true stop
   and scribbles once you crawl. The server EMA-smooths while moving and
   freezes in that crawl (below 2 km/h), and that opinion is what /live
   publishes as lat/lon — and as `crawl`, so this page never has to
   compare speed against a threshold of its own. The same payload
   carries raw_lat/raw_lon so ?smooth=off can draw the receiver instead.
   The default trail still will not perfectly re-trace the same ground —
   that is the receiver, not the draw path. The faint circle is an
   HDOP-based honesty radius.
*/

(function () {
  "use strict";

  // --- v1 constants (street/track driving; URL params later) ---
  var DEFAULT_METERS_ACROSS = 200;   // shorter canvas edge (floor)
  var DEFAULT_METERS_MAX = 1000;     // zoom-out cap; freeway trails will hit it
  var TRAIL_FIT_PAD = 1.1;           // keep the farthest point off the bezel
  var SCALE_OUT_TAU_MS = 400;        // zoom out promptly
  var SCALE_IN_TAU_MS = 2500;        // ease back toward the floor more slowly
  var POLL_MS = 100;                 // match ~10 Hz GPS
  var DRAW_HZ = 30;                  // paint rate, decoupled from the GPS
  // Skip trail points closer than this so idle GPS wander does not scribble.
  // The trail is a time window (TRAIL_MAX_MS of trail-clock time) with a
  // ceiling (TRAIL_MAX_POINTS) over it. The ceiling is not a window: it is
  // the guarantee that the array cannot grow without bound whatever the
  // poll rate does later. 6000 is the window at the receiver's nominal
  // rate (10 min x 10 Hz; appends are per new fix, not per poll), so a
  // rolling raw trail reaches the clock and the ceiling together and the
  // clock is the authority. The ceiling still binds where the clock does
  // not run: parked in the raw view the clock pauses and the scribble
  // does not, so a long enough sit pushes the approach out from the
  // front. That is the trade for letting the raw view scribble at all —
  // it is the receiver's diary, not the lap. In the smoothed view the
  // 3 m gate appends nothing at a light and needs about half an hour of
  // road speed to fill the array, so the window binds and the ceiling is
  // a net it does not normally reach.
  var TRAIL_MIN_M = 3;
  var TRAIL_MAX_MS = 10 * 60 * 1000;
  var TRAIL_MAX_POINTS = 6000;
  // The trail clock stops while /live says crawl (the server's freeze
  // band, one authority — this page used to resume at 3 km/h against
  // a server that unfroze at 2, and the paddock crawl in between drew
  // as a chord). Sitting does not consume the ten-minute window;
  // skipping expire without freezing now would wipe the lap on
  // throttle-up.
  var ARROW_PX = 28;
  var LIVE_URL = "/live";

  // Bridging between fixes. TAU is an exponential time constant: bigger is
  // smoother and laggier. DR_MAX_MS caps how far ahead of the last fix dead
  // reckoning will guess — a little over one sample interval, so a dropped
  // sample coasts instead of stalling, and a dead feed parks rather than
  // driving off into fiction. STALE_MS is the long goodbye: past a missed
  // sample, once the last `t` is this old the HUD stops looking healthy.
  var POS_TAU_MS = 120;
  var HEADING_TAU_MS = 150;          // body heading; arrow residual uses this
  var DEFAULT_HEADING_CAM_TAU_MS = 1200; // map/trail in heading-up; ?headingTau= overrides
  var ARROW_RESIDUAL_MAX_DEG = 15;   // how far the arrow may yaw off screen-up
  var DR_MAX_MS = 250;
  var STALE_MS = 1000;
  var DR_MIN_KMH = 1.0;              // below this, standing still: no DR
  var SNAP_M = 25;                   // a jump this big is a teleport, not motion
  var TRAIL_CORE = "rgba(80, 200, 255, 0.85)";
  var TRAIL_CORE_W = 5;
  var TRAIL_EDGE = "#111111";
  var TRAIL_EDGE_W = 10;
  var TILE_PX = 256;
  var TILE_MAX_Z = 20;
  var TILE_MAX_COUNT = 160;          // viewport plus pad, and √2 more when heading-up rotates the AABB
  var TILE_FAIL_RETRY_MS = 4000;     // a failed tile is a hole, not a life sentence
  var EQUATOR_M = 40075016.686;      // WGS84 circumference, for mercator metres/pixel
  var TILE_HOST = "https://tiles.stadiamaps.com/tiles/";
  var MAP_STYLES = {
    alidade: { slug: "alidade_smooth_dark", fill: "#1a1a1a", stamen: false },
    toner:   { slug: "stamen_toner",        fill: "#f0f0f0", stamen: true },
    terrain: { slug: "stamen_terrain",      fill: "#e8e4d8", stamen: true }
  };

  var params = new URLSearchParams(window.location.search);
  var metersFloor = parseFloat(params.get("meters"));
  if (!isFinite(metersFloor) || metersFloor <= 0) {
    metersFloor = DEFAULT_METERS_ACROSS;
  }
  var metersMax = parseFloat(params.get("metersMax"));
  if (!isFinite(metersMax) || metersMax <= 0) {
    metersMax = DEFAULT_METERS_MAX;
  }
  if (metersMax < metersFloor) metersMax = metersFloor;
  var metersAcross = metersFloor;    // live scale; eases between floor and cap
  var upMode = (params.get("up") || "north").toLowerCase();
  if (upMode !== "north" && upMode !== "heading") {
    upMode = "north";
  }
  var smooth = (params.get("smooth") || "on").toLowerCase() !== "off";
  var bgName = (params.get("bg") || "transparent").toLowerCase();
  var bgColor = "transparent";
  if (bgName === "grey" || bgName === "gray") bgColor = "#c5ccd4";
  else if (bgName === "dark") bgColor = "#0b0f14";
  document.documentElement.style.background = bgColor;
  document.body.style.background = bgColor;
  var showHud = (params.get("hud") || "off").toLowerCase() === "on";
  var trailPause = (params.get("trailPause") || "on").toLowerCase() !== "off";
  var headingTauParam = params.get("headingTau");
  var headingCamCoupled = false;
  var headingCamTauMs = DEFAULT_HEADING_CAM_TAU_MS;
  if (headingTauParam != null && headingTauParam !== "") {
    if (String(headingTauParam).toLowerCase() === "off") {
      headingCamCoupled = true;
    } else {
      var ht = parseFloat(headingTauParam);
      if (isFinite(ht) && ht <= 0) headingCamCoupled = true;
      else if (isFinite(ht)) {
        headingCamTauMs = ht;
        if (headingCamTauMs < HEADING_TAU_MS) headingCamTauMs = HEADING_TAU_MS;
      }
    }
  }
  var mapName = (params.get("map") || "off").toLowerCase();
  if (mapName === "stadia" || mapName === "on") mapName = "alidade";
  var mapStyle = MAP_STYLES[mapName] || null;
  var mapOn = !!mapStyle;
  var stadiaKey = params.get("stadiaKey") || "";
  var mapDraw = mapOn;

  var canvas = document.getElementById("map");
  var ctx = canvas.getContext("2d");
  var hudEl = document.getElementById("hud");
  var statusEl = document.getElementById("status");
  var attribEl = document.getElementById("attrib");
  var attribStamen = document.getElementById("attrib-stamen");
  if (hudEl && !showHud) hudEl.style.display = "none";
  if (attribEl) attribEl.style.display = mapDraw ? "block" : "none";
  if (attribStamen) attribStamen.style.display = (mapDraw && mapStyle.stamen) ? "inline" : "none";

  var trail = [];      // [{lat, lon, tMs}, ...] tMs is trail-clock, not wall
  var trailClockMs = 0;
  var trailClockWallMs = 0;
  var trailClockPaused = true;
  var crawlWarned = false;  // said "no crawl field" to the console once
  var fix = null;      // newest server snapshot
  var fixAtMs = 0;     // performance.now() when a NEW fix landed
  var fixT = null;     // server timestamp of that fix, to spot repeats
  var render = null;   // {lat, lon, heading} the camera actually draws
  var lastFrameMs = 0;
  var feedError = null;  // last /live fetch failure; draw() owns the HUD

  function resize() {
    var dpr = window.devicePixelRatio || 1;
    var w = window.innerWidth;
    var h = window.innerHeight;
    canvas.width = Math.max(1, Math.round(w * dpr));
    canvas.height = Math.max(1, Math.round(h * dpr));
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener("resize", resize);
  resize();

  function enuMeters(lat0, lon0, lat, lon) {
    var north = (lat - lat0) * 111320.0;
    var east = (lon - lon0) * 111320.0 * Math.cos((lat0 * Math.PI) / 180);
    return { east: east, north: north };
  }

  function distM(a, b) {
    var en = enuMeters(a.lat, a.lon, b.lat, b.lon);
    return Math.hypot(en.east, en.north);
  }

  function offsetLatLon(lat, lon, headingDeg, meters) {
    var rad = (headingDeg * Math.PI) / 180;
    var north = Math.cos(rad) * meters;
    var east = Math.sin(rad) * meters;
    return {
      lat: lat + north / 111320.0,
      lon: lon + east / (111320.0 * Math.cos((lat * Math.PI) / 180))
    };
  }

  // Shortest-way-around interpolation: 350 -> 10 turns 20 degrees, not 340.
  function angleLerp(from, to, k) {
    var delta = ((to - from + 540) % 360) - 180;
    return (from + delta * k + 360) % 360;
  }

  function angleDelta(from, to) {
    return ((to - from + 540) % 360) - 180;
  }

  // Heading-up: arrow shows body minus camera, clamped. North-up: body.
  // Coupled (headingTau=0/off): arrow glued to screen-up, old behaviour.
  function arrowHeadingDeg(bodyHdg, camHdg) {
    if (upMode !== "heading") return bodyHdg;
    if (headingCamCoupled) return 0;
    var d = angleDelta(camHdg, bodyHdg);
    if (d > ARROW_RESIDUAL_MAX_DEG) return ARROW_RESIDUAL_MAX_DEG;
    if (d < -ARROW_RESIDUAL_MAX_DEG) return -ARROW_RESIDUAL_MAX_DEG;
    return d;
  }

  // Project EN meters to canvas. headingRad used only for up=heading:
  // rotate so the current course points to screen-up.
  function enToScreen(east, north, cx, cy, mPerPx, headingRad) {
    var e = east;
    var n = north;
    if (upMode === "heading") {
      var c = Math.cos(headingRad);
      var s = Math.sin(headingRad);
      var e2 = e * c - n * s;
      var n2 = e * s + n * c;
      e = e2;
      n = n2;
    }
    return { x: cx + e / mPerPx, y: cy - n / mPerPx };
  }

  // Slippy-map (Web Mercator) helpers. Tiles are drawn in the same ENU
  // frame as the trail; at a few hundred metres the two agree closely.
  function lonLatToTileFrac(lat, lon, z) {
    var n = Math.pow(2, z);
    var x = (lon + 180) / 360 * n;
    var latRad = (lat * Math.PI) / 180;
    var y = (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n;
    return { x: x, y: y };
  }

  function tileToLatLon(z, x, y) {
    var n = Math.pow(2, z);
    var lon = x / n * 360 - 180;
    var latRad = Math.atan(Math.sinh(Math.PI * (1 - 2 * y / n)));
    return { lat: (latRad * 180) / Math.PI, lon: lon };
  }

  function zoomForMPerPx(mPerPx, lat) {
    var cos = Math.cos((lat * Math.PI) / 180);
    if (cos < 0.01) cos = 0.01;
    var z = Math.round(Math.log(EQUATOR_M * cos / (TILE_PX * mPerPx)) / Math.LN2);
    if (z < 1) z = 1;
    if (z > TILE_MAX_Z) z = TILE_MAX_Z;
    return z;
  }

  function tileUrl(z, x, y) {
    var slug = mapStyle ? mapStyle.slug : "alidade_smooth_dark";
    var url = TILE_HOST + slug + "/" + z + "/" + x + "/" + y + ".png";
    if (stadiaKey) url += "?api_key=" + encodeURIComponent(stadiaKey);
    return url;
  }

  var tileCache = {};
  var tileFailWarned = false;

  function getTile(z, x, y) {
    var k = (mapStyle ? mapStyle.slug : "") + "/" + z + "/" + x + "/" + y;
    var now = window.performance.now();
    var rec = tileCache[k];
    if (rec) {
      if (rec.status === "fail") {
        var age = (rec.failedAt != null) ? (now - rec.failedAt) : TILE_FAIL_RETRY_MS;
        if (age < TILE_FAIL_RETRY_MS) return rec;
        delete tileCache[k];
      } else {
        return rec;
      }
    }
    if (typeof Image === "undefined") {
      tileCache[k] = { status: "fail", failedAt: now };
      return tileCache[k];
    }
    rec = { img: new Image(), status: "loading" };
    rec.img.onload = function () { rec.status = "ok"; };
    rec.img.onerror = function () {
      rec.status = "fail";
      rec.failedAt = window.performance.now();
      if (!tileFailWarned) {
        tileFailWarned = true;
        console.warn("map tile failed to load; on 127.0.0.1 try without a key, or add ?stadiaKey=");
      }
    };
    rec.img.src = tileUrl(z, x, y);
    tileCache[k] = rec;
    return rec;
  }

  function visibleTileRange(lat0, lon0, w, h, mPerPx, z, headingRad) {
    headingRad = headingRad || 0;
    var cx = w / 2;
    var cy = h / 2;
    var c = Math.cos(headingRad);
    var s = Math.sin(headingRad);
    var screen = [[0, 0], [w, 0], [0, h], [w, h]];
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    var cosLat = Math.cos((lat0 * Math.PI) / 180);
    for (var i = 0; i < 4; i++) {
      // Screen → EN: inverse of enToScreen's heading rotation so a
      // heading-up view still fetches the tiles that fill the corners.
      var e2 = (screen[i][0] - cx) * mPerPx;
      var n2 = (cy - screen[i][1]) * mPerPx;
      var east = e2 * c + n2 * s;
      var north = -e2 * s + n2 * c;
      var lat = lat0 + north / 111320.0;
      var lon = lon0 + east / (111320.0 * cosLat);
      var t = lonLatToTileFrac(lat, lon, z);
      if (t.x < minX) minX = t.x;
      if (t.x > maxX) maxX = t.x;
      if (t.y < minY) minY = t.y;
      if (t.y > maxY) maxY = t.y;
    }
    var n = Math.pow(2, z);
    var x0 = Math.floor(minX) - 1;
    var y0 = Math.floor(minY) - 1;
    var x1 = Math.ceil(maxX) + 1;
    var y1 = Math.ceil(maxY) + 1;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > n - 1) x1 = n - 1;
    if (y1 > n - 1) y1 = n - 1;
    return { x0: x0, y0: y0, x1: x1, y1: y1 };
  }

  function drawTiles(lat0, lon0, cx, cy, mPerPx, w, h, headingRad) {
    headingRad = headingRad || 0;
    var z = zoomForMPerPx(mPerPx, lat0);
    var r = visibleTileRange(lat0, lon0, w, h, mPerPx, z, headingRad);
    var count = (r.x1 - r.x0 + 1) * (r.y1 - r.y0 + 1);
    if (count <= 0 || count > TILE_MAX_COUNT) return z;
    ctx.save();
    if (headingRad) {
      // Same rotation as enToScreen (φ = −heading): draw tiles north-up
      // in this frame so they turn with the trail.
      ctx.translate(cx, cy);
      ctx.rotate(-headingRad);
      ctx.translate(-cx, -cy);
    }
    for (var x = r.x0; x <= r.x1; x++) {
      for (var y = r.y0; y <= r.y1; y++) {
        var rec = getTile(z, x, y);
        if (!rec || rec.status !== "ok") continue;
        var nw = tileToLatLon(z, x, y);
        var se = tileToLatLon(z, x + 1, y + 1);
        var enNW = enuMeters(lat0, lon0, nw.lat, nw.lon);
        var enSE = enuMeters(lat0, lon0, se.lat, se.lon);
        var pNW = enToScreen(enNW.east, enNW.north, cx, cy, mPerPx, 0);
        var pSE = enToScreen(enSE.east, enSE.north, cx, cy, mPerPx, 0);
        var dw = pSE.x - pNW.x;
        var dh = pSE.y - pNW.y;
        if (dw > 0 && dh > 0) {
          ctx.drawImage(rec.img, pNW.x, pNW.y, dw, dh);
        }
      }
    }
    ctx.restore();
    return z;
  }

  /* Default view uses the server's EMA/frozen lat/lon. smooth=off draws
     the receiver's last fix from the same payload. */
  function displayPos(f) {
    if (!smooth && f.raw_lat != null && f.raw_lon != null) {
      return { lat: f.raw_lat, lon: f.raw_lon };
    }
    return { lat: f.lat, lon: f.lon };
  }

  /* crawl is /live's own decision (true = frozen band, false = moving).
     A server that never sends it — in practice a gps_overlay.py started
     before a pull and still running under this newer page — gets no pause
     at all: the clock ages by wall time exactly as ?trailPause=off does,
     the HUD reads "crawl?", and the console says so once. Restart the
     server to fix it. (A stale cached page has no such string; it runs
     its own old rule silently.) That is the pre-crawl-field behaviour,
     chosen over inventing a speed threshold here — a second opinion on
     the crawl is the bug this field removed. */
  function advanceTrailClock(nowMs, crawl) {
    if (typeof crawl !== "boolean" && !crawlWarned) {
      crawlWarned = true;
      console.warn("/live has no crawl field: trail clock will not pause " +
                   "while parked (older gps_overlay.py?)");
    }
    trailClockPaused = trailPause && crawl === true;
    if (!trailClockPaused && trailClockWallMs > 0) {
      trailClockMs += nowMs - trailClockWallMs;
    }
    trailClockWallMs = nowMs;
  }

  function expireTrail() {
    var cutoff = trailClockMs - TRAIL_MAX_MS;
    var i = 0;
    while (i < trail.length && trail[i].tMs < cutoff) i++;
    if (i > 0) trail.splice(0, i);
  }

  function capTrail() {
    if (trail.length > TRAIL_MAX_POINTS) {
      trail.splice(0, trail.length - TRAIL_MAX_POINTS);
    }
  }

  function maybeAppendTrail(lat, lon, crawl) {
    var nowMs = window.performance.now();
    advanceTrailClock(nowMs, crawl);
    var p = { lat: lat, lon: lon, tMs: trailClockMs };
    if (trail.length === 0) {
      trail.push(p);
      return;
    }
    // The pause and the 3 m skip are both opinions of the default view.
    // smooth=off appends every sample, parked or not — the idle scribble
    // is the phenomenon that view exists to show — and the point ceiling
    // is what keeps a 10 Hz scribble at the grid from growing without
    // bound. Points appended while the clock is paused all carry the
    // paused tMs, so they age out together once the car rolls.
    if (smooth &&
        (trailClockPaused ||
         distM(trail[trail.length - 1], p) < TRAIL_MIN_M)) {
      expireTrail();
      return;
    }
    trail.push(p);
    capTrail();
    expireTrail();
  }

  /* Where the camera should be right now, given the newest fix and how long
     ago it landed. Returns the (raw or smoothed) fix itself when
     smoothing is off. */
  function targetState(nowMs) {
    var pos = displayPos(fix);
    var target = { lat: pos.lat, lon: pos.lon, heading: fix.heading_deg };
    if (!smooth) return target;
    var ageMs = Math.min(nowMs - fixAtMs, DR_MAX_MS);
    if (ageMs > 0 && fix.speed_kmh >= DR_MIN_KMH) {
      var metres = (fix.speed_kmh / 3.6) * (ageMs / 1000);
      var dr = offsetLatLon(pos.lat, pos.lon, fix.heading_deg, metres);
      target.lat = dr.lat;
      target.lon = dr.lon;
    }
    return target;
  }

  function advanceRender(nowMs, dtMs) {
    var target = targetState(nowMs);
    if (!render || !smooth || distM(render, target) > SNAP_M) {
      render = {
        lat: target.lat,
        lon: target.lon,
        heading: target.heading,
        cameraHeading: target.heading
      };
      return;
    }
    var kPos = 1 - Math.exp(-dtMs / POS_TAU_MS);
    var kHdg = 1 - Math.exp(-dtMs / HEADING_TAU_MS);
    render.lat += (target.lat - render.lat) * kPos;
    render.lon += (target.lon - render.lon) * kPos;
    render.heading = angleLerp(render.heading, target.heading, kHdg);
    if (headingCamCoupled) {
      render.cameraHeading = render.heading;
    } else {
      var kCam = 1 - Math.exp(-dtMs / headingCamTauMs);
      var camFrom = (render.cameraHeading != null) ? render.cameraHeading : render.heading;
      render.cameraHeading = angleLerp(camFrom, target.heading, kCam);
    }
  }

  function trailRadiusM(lat0, lon0) {
    var r = 0;
    var origin = { lat: lat0, lon: lon0 };
    for (var i = 0; i < trail.length; i++) {
      var d = distM(origin, trail[i]);
      if (d > r) r = d;
    }
    return r;
  }

  /* Metres-across that would fit the trail on the short edge, clamped
     to the floor/cap. World radius from the car, not a screen box, so
     heading-up rotation does not pump the zoom. */
  function neededMeters(lat0, lon0) {
    var r = trailRadiusM(lat0, lon0);
    if (r <= 0) return metersFloor;
    var fit = 2 * r * TRAIL_FIT_PAD;
    if (fit <= metersFloor) return metersFloor;
    if (fit >= metersMax) return metersMax;
    return fit;
  }

  function advanceScale(dtMs) {
    if (!render || metersMax <= metersFloor) return;
    var needed = neededMeters(render.lat, render.lon);
    if (Math.abs(needed - metersAcross) < 0.5) {
      metersAcross = needed;
      return;
    }
    var tau = (needed > metersAcross) ? SCALE_OUT_TAU_MS : SCALE_IN_TAU_MS;
    var k = 1 - Math.exp(-dtMs / tau);
    metersAcross += (needed - metersAcross) * k;
  }

  function draw() {
    var w = window.innerWidth;
    var h = window.innerHeight;
    ctx.clearRect(0, 0, w, h);

    function drawCrosshair() {
      // Subtle crosshair (screen axes: up/sides — body axes when up=heading)
      ctx.strokeStyle = "rgba(200,208,216,0.12)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(w / 2, 0);
      ctx.lineTo(w / 2, h);
      ctx.moveTo(0, h / 2);
      ctx.lineTo(w, h / 2);
      ctx.stroke();
    }

    // A last pose stays on the map when the feed dies; only the HUD
    // changes. Without that, draw() used to paint 55 km/h over the
    // fetch-error line every frame and look healthy.
    if (!render || !fix || fix.lat == null) {
      drawCrosshair();
      if (feedError) {
        statusEl.textContent = "live feed: " + feedError;
      } else if (fix && fix.reason) {
        statusEl.textContent = fix.reason;
      } else {
        statusEl.textContent = "waiting for fix…";
      }
      return;
    }

    var shortEdge = Math.min(w, h);
    var mPerPx = metersAcross / shortEdge;
    var lat0 = render.lat;
    var lon0 = render.lon;
    var bodyHdg = render.heading;
    var camHdg = render.cameraHeading;
    if (camHdg == null || headingCamCoupled || upMode !== "heading") {
      camHdg = bodyHdg;
    }
    var heading = bodyHdg;
    var headingRad = ((upMode === "heading" ? camHdg : bodyHdg) * Math.PI) / 180;
    var tileHdgRad = (upMode === "heading") ? (camHdg * Math.PI) / 180 : 0;
    var cx = w / 2;
    var cy = h / 2;
    var mapZ = null;

    if (mapDraw) {
      ctx.fillStyle = mapStyle.fill;
      ctx.fillRect(0, 0, w, h);
      mapZ = drawTiles(lat0, lon0, cx, cy, mPerPx, w, h, tileHdgRad);
    }

    drawCrosshair();

    // Honesty circle: typical horizontal error band from HDOP (rotation-invariant)
    if (fix.accuracy_m && fix.accuracy_m > 0) {
      var rPx = fix.accuracy_m / mPerPx;
      ctx.beginPath();
      ctx.arc(cx, cy, rPx, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255, 204, 51, 0.35)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.fillStyle = "rgba(255, 204, 51, 0.06)";
      ctx.fill();
    }

    // Trail: same path twice — dark edge under the cyan core so the
    // line still reads when a similar colour sits behind it.
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    var started = false;
    for (var i = 0; i < trail.length; i++) {
      var en = enuMeters(lat0, lon0, trail[i].lat, trail[i].lon);
      var pt = enToScreen(en.east, en.north, cx, cy, mPerPx, headingRad);
      if (!started) {
        ctx.moveTo(pt.x, pt.y);
        started = true;
      } else {
        ctx.lineTo(pt.x, pt.y);
      }
    }
    if (started) {
      ctx.strokeStyle = TRAIL_EDGE;
      ctx.lineWidth = TRAIL_EDGE_W;
      ctx.stroke();
      ctx.strokeStyle = TRAIL_CORE;
      ctx.lineWidth = TRAIL_CORE_W;
      ctx.stroke();
    }

    // Arrow: north-up follows body heading. Heading-up is screen-up plus
    // a clamped residual (body − camera) so COG jitter twists the arrow
    // instead of the world. headingTau=0 glues it up again.
    var arrowHdg = arrowHeadingDeg(bodyHdg, camHdg);
    drawArrow(cx, cy, arrowHdg, ARROW_PX);

    var ageMs = fixAtMs ? (window.performance.now() - fixAtMs) : 0;
    var lost = feedError || fix.ok === false || (fixAtMs && ageMs > STALE_MS);
    if (lost) {
      var sec = Math.max(1, Math.floor(ageMs / 1000));
      statusEl.textContent = "signal lost " + sec + "s";
      return;
    }

    var pos = displayPos(fix);
    var acc = (fix.accuracy_m != null)
      ? ("  ±" + fix.accuracy_m.toFixed(1) + " m")
      : "";
    var sats = (fix.sats != null) ? ("  " + fix.sats + " sats") : "";
    // "crawl" while the server says so; "crawl?" when it never says —
    // the older-server case, where the trail clock is not pausing.
    var crawl = (fix.crawl === true) ? "  crawl"
      : (fix.crawl === false) ? "" : "  crawl?";
    var mapHud = mapDraw
      ? ("  map=" + mapName + (mapZ != null ? " z" + mapZ : ""))
      : "";
    var camHud = (upMode === "heading" && !headingCamCoupled)
      ? (" cam " + (Math.round(camHdg) % 360) + "°")
      : "";
    statusEl.textContent =
      pos.lat.toFixed(6) + ", " + pos.lon.toFixed(6) +
      "  " + fix.speed_kmh.toFixed(1) + " km/h" + crawl + "  hdg " +
      (Math.round(heading) % 360) + "°" + camHud + "  up=" + upMode +
      (smooth ? "" : "  smooth=off") + "  " + Math.round(metersAcross) +
      " m  trail " + trail.length + acc + sats + mapHud;
  }

  function drawArrow(cx, cy, headingDeg, size) {
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate((headingDeg * Math.PI) / 180);
    ctx.beginPath();
    // Nose toward -Y (screen up) before rotation
    ctx.moveTo(0, -size);
    ctx.lineTo(size * 0.55, size * 0.65);
    ctx.lineTo(0, size * 0.25);
    ctx.lineTo(-size * 0.55, size * 0.65);
    ctx.closePath();
    ctx.fillStyle = "#ffcc33";
    ctx.strokeStyle = "#1a1400";
    ctx.lineWidth = 2;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  /* rAF, throttled to DRAW_HZ. rAF (not setInterval) so the browser can
     skip frames while the tab or OBS source is hidden instead of queueing
     a backlog of paints nobody saw. */
  function frame(nowMs) {
    window.requestAnimationFrame(frame);
    var dtMs = nowMs - lastFrameMs;
    if (dtMs < 1000 / DRAW_HZ - 1) return;
    lastFrameMs = nowMs;
    if (fix && fix.ok) advanceRender(nowMs, dtMs);
    if (render) advanceScale(dtMs);
    draw();
  }

  function poll() {
    fetch(LIVE_URL, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        feedError = null;
        // Polls and fixes are not in step, so a poll often returns the fix
        // we already have. Restarting the dead-reckoning clock on those
        // would rewind the car to the raw fix and undo the bridging, so the
        // clock only moves when the server's timestamp does.
        if (data && data.ok && data.t !== fixT) {
          fixT = data.t;
          fixAtMs = window.performance.now();
          var pos = displayPos(data);
          maybeAppendTrail(pos.lat, pos.lon, data.crawl);
        }
        fix = data;
      })
      .catch(function (err) {
        feedError = err.message || String(err);
      });
  }

  setInterval(poll, POLL_MS);
  poll();
  window.requestAnimationFrame(frame);
})();
