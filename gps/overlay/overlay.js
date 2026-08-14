/* Ego-centered GPS trail. Defaults are magic numbers for v1; some already
   accept URL params (?meters=, ?up=, ?smooth=).

   ?up=north   (default) — map North-up; arrow rotates with heading
   ?up=heading           — arrow fixed pointing up; map rotates with heading
   ?smooth=off           — draw raw fixes, no bridging (A/B against the default)

   Rates: the receiver speaks at ~10 Hz and the poll matches it, but the
   draw loop runs on requestAnimationFrame capped near 30 Hz. Repainting
   the same fix faster would look identical, so the frames between samples
   are earned: heading and position ease toward the newest fix, and a short
   dead-reckoning step carries the car forward along its last known course.
   Every new fix corrects it, so error cannot accumulate past one sample.

   Driving-scale note: consumer GPS still wanders meters even with a clear
   sky. The server EMA-smooths and freezes when nearly stopped; the trail
   still will not perfectly re-trace the same ground — that is the
   receiver, not the draw path. The faint circle is an HDOP-based honesty
   radius.
*/

(function () {
  "use strict";

  // --- v1 constants (street/track driving; URL params later) ---
  var DEFAULT_METERS_ACROSS = 200;   // shorter canvas edge
  var POLL_MS = 100;                 // match ~10 Hz GPS
  var DRAW_HZ = 30;                  // paint rate, decoupled from the GPS
  // Skip trail points closer than this so idle GPS wander does not scribble.
  var TRAIL_MIN_M = 3;
  var TRAIL_MAX_POINTS = 5000;
  var ARROW_PX = 28;
  var LIVE_URL = "/live";

  // Bridging between fixes. TAU is an exponential time constant: bigger is
  // smoother and laggier. DR_MAX_MS caps how far ahead of the last fix dead
  // reckoning will guess — a little over one sample interval, so a dropped
  // sample coasts instead of stalling, and a dead feed parks rather than
  // driving off into fiction.
  var POS_TAU_MS = 120;
  var HEADING_TAU_MS = 150;
  var DR_MAX_MS = 250;
  var DR_MIN_KMH = 1.0;              // below this, standing still: no DR
  var SNAP_M = 25;                   // a jump this big is a teleport, not motion

  var params = new URLSearchParams(window.location.search);
  var metersAcross = parseFloat(params.get("meters"));
  if (!isFinite(metersAcross) || metersAcross <= 0) {
    metersAcross = DEFAULT_METERS_ACROSS;
  }
  var upMode = (params.get("up") || "north").toLowerCase();
  if (upMode !== "north" && upMode !== "heading") {
    upMode = "north";
  }
  var smooth = (params.get("smooth") || "on").toLowerCase() !== "off";

  var canvas = document.getElementById("map");
  var ctx = canvas.getContext("2d");
  var statusEl = document.getElementById("status");

  var trail = [];      // [{lat, lon}, ...] absolute, from real fixes only
  var fix = null;      // newest server snapshot
  var fixAtMs = 0;     // performance.now() when a NEW fix landed
  var fixT = null;     // server timestamp of that fix, to spot repeats
  var render = null;   // {lat, lon, heading} the camera actually draws
  var lastFrameMs = 0;

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

  function maybeAppendTrail(lat, lon) {
    var p = { lat: lat, lon: lon };
    if (trail.length === 0) {
      trail.push(p);
      return;
    }
    if (distM(trail[trail.length - 1], p) < TRAIL_MIN_M) return;
    trail.push(p);
    if (trail.length > TRAIL_MAX_POINTS) {
      trail.splice(0, trail.length - TRAIL_MAX_POINTS);
    }
  }

  /* Where the camera should be right now, given the newest fix and how long
     ago it landed. Returns the fix itself when smoothing is off. */
  function targetState(nowMs) {
    var target = { lat: fix.lat, lon: fix.lon, heading: fix.heading_deg };
    if (!smooth) return target;
    var ageMs = Math.min(nowMs - fixAtMs, DR_MAX_MS);
    if (ageMs > 0 && fix.speed_kmh >= DR_MIN_KMH) {
      var metres = (fix.speed_kmh / 3.6) * (ageMs / 1000);
      var dr = offsetLatLon(fix.lat, fix.lon, fix.heading_deg, metres);
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

    if (!fix || !fix.ok || !render) {
      statusEl.textContent = "waiting for fix…";
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

    // Trail (world moves under a fixed center; may also rotate if up=heading)
    ctx.strokeStyle = "rgba(80, 200, 255, 0.85)";
    ctx.lineWidth = 2;
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
    if (started) ctx.stroke();

    // Arrow: rotates in north-up; fixed pointing up in heading-up
    var arrowHdg = (upMode === "heading") ? 0 : heading;
    drawArrow(cx, cy, arrowHdg, ARROW_PX);

    var acc = (fix.accuracy_m != null)
      ? ("  ±" + fix.accuracy_m.toFixed(1) + " m")
      : "";
    var sats = (fix.sats != null) ? ("  " + fix.sats + " sats") : "";
    statusEl.textContent =
      fix.lat.toFixed(6) + ", " + fix.lon.toFixed(6) +
      "  " + fix.speed_kmh.toFixed(1) + " km/h  hdg " +
      (Math.round(heading) % 360) + "°  up=" + upMode +
      (smooth ? "" : "  smooth=off") + "  " + metersAcross +
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
    draw();
  }

  function poll() {
    fetch(LIVE_URL, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        // Polls and fixes are not in step, so a poll often returns the fix
        // we already have. Restarting the dead-reckoning clock on those
        // would rewind the car to the raw fix and undo the bridging, so the
        // clock only moves when the server's timestamp does.
        if (data && data.ok && data.t !== fixT) {
          fixT = data.t;
          fixAtMs = window.performance.now();
          maybeAppendTrail(data.lat, data.lon);
        }
        fix = data;
      })
      .catch(function (err) {
        statusEl.textContent = "live feed: " + err.message;
      });
  }

  setInterval(poll, POLL_MS);
  poll();
  window.requestAnimationFrame(frame);
})();
