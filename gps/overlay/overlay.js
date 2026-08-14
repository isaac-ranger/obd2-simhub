/* Ego-centered GPS trail. Defaults are magic numbers for v1; some already
   accept URL params (?meters=, ?metersMax=, ?up=, ?smooth=, ?bg=, ?hud=, ?trailPause=).

   ?up=north   (default) — map North-up; arrow rotates with heading
   ?up=heading           — arrow fixed pointing up; map rotates with heading
   ?smooth=off           — draw /live's raw_lat/raw_lon, no bridging, no trail skip
   ?meters=200           — floor: metres across the short edge (default 200)
   ?metersMax=1000       — cap: ease out this far to keep the trail on screen
                           (equal to ?meters= locks the old fixed scale)
   ?bg=transparent       — default; OBS composites the trail over video
   ?bg=grey              — solid bench so the dark trail edge is visible
   ?bg=dark              — original near-black stage
   ?hud=on               — show the status line (hidden by default)
   ?trailPause=off       — age the trail by wall clock even while parked
                           (default pauses the ten-minute window when stopped)

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
   publishes as lat/lon. The same payload carries raw_lat/raw_lon so
   ?smooth=off can draw the receiver instead. The default trail still
   will not perfectly re-trace the same ground — that is the receiver,
   not the draw path. The faint circle is an HDOP-based honesty radius.
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
  // The trail is a time window, not a point budget: a count cap made the
  // two views cover different drives. Raw at 10 Hz filled 5000 points in
  // ~8 minutes; the 3 m gate stretched the default toward ~25 at road
  // speed and appended nothing at a light — so a long red light spent
  // the raw buffer on a two-meter circle and ate the approach.
  var TRAIL_MIN_M = 3;
  var TRAIL_MAX_MS = 10 * 60 * 1000;
  // Trail clock pauses below PAUSE, resumes above RESUME, so a 2 km/h
  // twitch does not restart the hourglass. Sitting does not consume
  // the ten-minute window; skipping expire without freezing now would
  // wipe the lap on throttle-up.
  var TRAIL_PAUSE_BELOW_KMH = 2.0;
  var TRAIL_RESUME_ABOVE_KMH = 3.0;
  var ARROW_PX = 28;
  var LIVE_URL = "/live";

  // Bridging between fixes. TAU is an exponential time constant: bigger is
  // smoother and laggier. DR_MAX_MS caps how far ahead of the last fix dead
  // reckoning will guess — a little over one sample interval, so a dropped
  // sample coasts instead of stalling, and a dead feed parks rather than
  // driving off into fiction. STALE_MS is the long goodbye: past a missed
  // sample, once the last `t` is this old the HUD stops looking healthy.
  var POS_TAU_MS = 120;
  var HEADING_TAU_MS = 150;
  var DR_MAX_MS = 250;
  var STALE_MS = 1000;
  var DR_MIN_KMH = 1.0;              // below this, standing still: no DR
  var SNAP_M = 25;                   // a jump this big is a teleport, not motion
  var TRAIL_CORE = "rgba(80, 200, 255, 0.85)";
  var TRAIL_CORE_W = 5;
  var TRAIL_EDGE = "#111111";
  var TRAIL_EDGE_W = 10;

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

  var canvas = document.getElementById("map");
  var ctx = canvas.getContext("2d");
  var hudEl = document.getElementById("hud");
  var statusEl = document.getElementById("status");
  if (hudEl && !showHud) hudEl.style.display = "none";

  var trail = [];      // [{lat, lon, tMs}, ...] tMs is trail-clock, not wall
  var trailClockMs = 0;
  var trailClockWallMs = 0;
  var trailClockPaused = true;
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

  /* Default view uses the server's EMA/frozen lat/lon. smooth=off draws
     the receiver's last fix from the same payload. */
  function displayPos(f) {
    if (!smooth && f.raw_lat != null && f.raw_lon != null) {
      return { lat: f.raw_lat, lon: f.raw_lon };
    }
    return { lat: f.lat, lon: f.lon };
  }

  function advanceTrailClock(nowMs, speed) {
    if (trailPause) {
      if (trailClockPaused) {
        if (speed >= TRAIL_RESUME_ABOVE_KMH) trailClockPaused = false;
      } else if (speed < TRAIL_PAUSE_BELOW_KMH) {
        trailClockPaused = true;
      }
    } else {
      trailClockPaused = false;
    }
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

  function maybeAppendTrail(lat, lon, speed) {
    var nowMs = window.performance.now();
    advanceTrailClock(nowMs, speed || 0);
    var p = { lat: lat, lon: lon, tMs: trailClockMs };
    if (trail.length === 0) {
      trail.push(p);
      return;
    }
    // Do not append while the clock is paused (even smooth=off): a
    // 10 Hz scribble at the grid would grow without bound. The 3 m
    // skip still applies while moving slowly with the clock running.
    if (trailClockPaused ||
        (smooth && distM(trail[trail.length - 1], p) < TRAIL_MIN_M)) {
      expireTrail();
      return;
    }
    trail.push(p);
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
      render = { lat: target.lat, lon: target.lon, heading: target.heading };
      return;
    }
    var kPos = 1 - Math.exp(-dtMs / POS_TAU_MS);
    var kHdg = 1 - Math.exp(-dtMs / HEADING_TAU_MS);
    render.lat += (target.lat - render.lat) * kPos;
    render.lon += (target.lon - render.lon) * kPos;
    render.heading = angleLerp(render.heading, target.heading, kHdg);
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

    // Subtle crosshair (screen axes: up/sides — body axes when up=heading)
    ctx.strokeStyle = "rgba(200,208,216,0.12)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(w / 2, 0);
    ctx.lineTo(w / 2, h);
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();

    // A last pose stays on the map when the feed dies; only the HUD
    // changes. Without that, draw() used to paint 55 km/h over the
    // fetch-error line every frame and look healthy.
    if (!render || !fix || fix.lat == null) {
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
    var heading = render.heading;
    var headingRad = (heading * Math.PI) / 180;
    var cx = w / 2;
    var cy = h / 2;

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

    // Arrow: rotates in north-up; fixed pointing up in heading-up
    var arrowHdg = (upMode === "heading") ? 0 : heading;
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
    statusEl.textContent =
      pos.lat.toFixed(6) + ", " + pos.lon.toFixed(6) +
      "  " + fix.speed_kmh.toFixed(1) + " km/h  hdg " +
      (Math.round(heading) % 360) + "°  up=" + upMode +
      (smooth ? "" : "  smooth=off") + "  " + Math.round(metersAcross) +
      " m  trail " + trail.length + acc + sats;
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
          maybeAppendTrail(pos.lat, pos.lon, data.speed_kmh);
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
