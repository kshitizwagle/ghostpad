// ghostpad M4 Tizen spike. Classic script, ES2017-level, no modules (see docs/tizen-spike.md
// for why). NOT RUN ON A REAL TV YET: written against the Samsung/Tizen API docs only.
//
// Flow: detect APIs -> list VIDEOSOURCE -> pick HDMI -> tvwindow.setSource -> tvwindow.show
// (z-order BEHIND, fallback FRONT) -> overlay with gamepad/rAF/visibility readouts ->
// optional WebSocket smoke test to the daemon.
(function () {
  'use strict';

  // ---- Config -------------------------------------------------------------------------
  // Set to the PC's LAN IP (e.g. '192.168.1.50') to enable the WebSocket smoke test.
  // Empty string = disabled. Can also be set at runtime from the web inspector console:
  //   setPcHost('192.168.1.50')   (persisted in localStorage)
  var PC_HOST = '';
  var PC_PORT = 47800;
  var PC_TOKEN = '';              // only if the daemon runs with --token
  var AUTO_PICK_MS = 5000;        // auto-pick first connected HDMI after this long without input
  var FULLSCREEN_RECT = ['0', '0', '1920px', '1080px']; // show(): x, y, w, h as strings
  var SEND_HEARTBEAT_MS = 50;     // DESIGN.md send policy
  var PING_MS = 250;

  var P = window.GhostpadProtocol;
  var hasTizen = typeof window.tizen !== 'undefined';

  // ---- DOM / logging ------------------------------------------------------------------
  function $(id) { return document.getElementById(id); }
  var els = { overlay: $('overlay'), mini: $('mini'), steps: $('steps'), sources: $('sources'),
    page: $('page'), pads: $('pads'), ws: $('ws'), log: $('log') };

  var T0 = Date.now();
  function ts() { return ((Date.now() - T0) / 1000).toFixed(1) + 's'; }

  var logLines = [];
  function log(msg) {
    var line = '[' + ts() + '] ' + msg;
    try { console.log('[spike] ' + line); } catch (e) { /* ignore */ }
    logLines.push(line);
    if (logLines.length > 14) logLines.shift();
  }

  // Step results: name -> {status: 'PENDING'|'OK'|'FAIL'|'SKIP', detail}
  var STEP_ORDER = ['tizen object', 'tizen.tvwindow', 'tizen.systeminfo', 'tizen.tvinputdevice',
    'VIDEOSOURCE read', 'HDMI source found', 'setSource', 'show BEHIND', 'show FRONT (fallback)'];
  var steps = {};
  STEP_ORDER.forEach(function (n) { steps[n] = { status: 'PENDING', detail: '' }; });
  function step(name, status, detail) {
    steps[name] = { status: status, detail: detail || '' };
    log(name + ': ' + status + (detail ? ' - ' + detail : ''));
  }
  function errStr(e) {
    if (!e) return String(e);
    return (e.name || 'Error') + (e.message ? ': ' + e.message : '') + (e.code ? ' (code ' + e.code + ')' : '');
  }

  // ---- State --------------------------------------------------------------------------
  var overlayMode = 2;            // 0 hidden, 1 mini, 2 full. Info/Green cycles.
  var sources = [];               // connected sources: {type, number, name}
  var allSourcesRaw = null;
  var pickIndex = 0;
  var pickerActive = false;
  var pickDeadline = 0;
  var autoPickTimer = null;
  var chosenSource = null;
  var zPos = 'BEHIND';            // z-order currently requested
  var videoShown = false;

  var visLog = [];                // visibilitychange history
  var rafTicks = 0, rafPerSec = 0, timerTicks = 0, timerPerSec = 0, lastRate = performance.now();
  var gpUpdates = 0, gpUpdatesPerSec = 0, lastGpTs = {};
  var lastInputNote = '(none yet)';
  var lastRafAt = performance.now(), maxRafGap = 0;

  // ---- Feature detection ---------------------------------------------------------------
  function detect() {
    step('tizen object', hasTizen ? 'OK' : 'FAIL', hasTizen ? '' : 'not on a Tizen runtime (desktop browser?)');
    if (!hasTizen) {
      ['tizen.tvwindow', 'tizen.systeminfo', 'tizen.tvinputdevice'].forEach(function (n) { step(n, 'FAIL', 'no tizen object'); });
      return false;
    }
    try {
      if (tizen.systeminfo && tizen.systeminfo.getCapability) log('capability tv.pip=' + tizen.systeminfo.getCapability('http://tizen.org/feature/tv.pip'));
    } catch (e) { log('getCapability(tv.pip) threw ' + errStr(e)); }
    var okWin = !!(tizen.tvwindow && typeof tizen.tvwindow.show === 'function');
    step('tizen.tvwindow', okWin ? 'OK' : 'FAIL', okWin ? '' : 'API missing (optional on some TVs, or privilege not granted)');
    var okSys = !!(tizen.systeminfo && typeof tizen.systeminfo.getPropertyValue === 'function');
    step('tizen.systeminfo', okSys ? 'OK' : 'FAIL', okSys ? '' : 'API missing');
    var okInp = !!(tizen.tvinputdevice && typeof tizen.tvinputdevice.registerKey === 'function');
    step('tizen.tvinputdevice', okInp ? 'OK' : 'FAIL', okInp ? '' : 'API missing: only default remote keys will work');
    return okWin && okSys;
  }

  // ---- Remote keys ---------------------------------------------------------------------
  // Key codes are looked up at runtime with tizen.tvinputdevice.getKey(name).code (and the
  // getSupportedKeys() list is logged) instead of trusting hard-coded numbers. The numbers below
  // are only the fallback when getKey is unavailable (desktop browser) or throws; they are from
  // memory and UNVERIFIED. Back (10009) is delivered without registration. Keyboard fallbacks
  // i/g/y/b/r help when testing in a desktop browser.
  var KEY = { INFO: 457, RED: 403, GREEN: 404, YELLOW: 405, BLUE: 406, BACK: 10009,
    ENTER: 13, LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40 };
  var KEY_NAMES = { INFO: 'Info', RED: 'ColorF0Red', GREEN: 'ColorF1Green', YELLOW: 'ColorF2Yellow', BLUE: 'ColorF3Blue' };

  function resolveKeyCodes() {
    if (!(hasTizen && tizen.tvinputdevice && typeof tizen.tvinputdevice.getKey === 'function')) return;
    Object.keys(KEY_NAMES).forEach(function (id) {
      try {
        var k = tizen.tvinputdevice.getKey(KEY_NAMES[id]);
        if (k && typeof k.code === 'number') {
          if (k.code !== KEY[id]) log('key ' + KEY_NAMES[id] + ': getKey code ' + k.code + ' differs from fallback ' + KEY[id]);
          KEY[id] = k.code;
        } else { log('getKey(' + KEY_NAMES[id] + ') returned no code; using fallback ' + KEY[id]); }
      } catch (e) { log('getKey(' + KEY_NAMES[id] + ') threw ' + errStr(e) + '; using fallback ' + KEY[id]); }
    });
  }

  function registerKeys() {
    if (!(hasTizen && tizen.tvinputdevice)) return;
    try {
      var supported = tizen.tvinputdevice.getSupportedKeys();
      log('supported keys: ' + supported.map(function (k) { return k.name + '=' + k.code; }).join(' '));
    } catch (e) { log('getSupportedKeys threw ' + errStr(e)); }
    resolveKeyCodes();
    Object.keys(KEY_NAMES).forEach(function (id) {
      var name = KEY_NAMES[id];
      try { tizen.tvinputdevice.registerKey(name); log('registerKey ' + name + ' ok (code ' + KEY[id] + ')'); }
      catch (e) { log('registerKey ' + name + ' failed: ' + errStr(e)); }
    });
  }

  // Gamepad B may also be delivered by the TV as a Back key event (UNVERIFIED whether it happens on
  // the U8500F; not documented). Ignore a Back keydown if any gamepad button is held or was pressed
  // within GP_BACK_GUARD_MS, so pressing B on the controller cannot close the app.
  var GP_BACK_GUARD_MS = 400;
  var lastPadButtonAt = 0;
  function padButtonRecent() {
    var pads = getPads(), now = Date.now();
    if (pads) {
      for (var i = 0; i < pads.length; i++) {
        var g = pads[i];
        if (!g) continue;
        for (var j = 0; j < g.buttons.length; j++) if (g.buttons[j].pressed) { lastPadButtonAt = now; break; }
      }
    }
    return now - lastPadButtonAt < GP_BACK_GUARD_MS;
  }

  window.addEventListener('keydown', function (ev) {
    var k = ev.keyCode;
    log('keydown ' + k + ' (' + (ev.key || '?') + ')');
    if (pickerActive) {
      if (k === KEY.UP || k === KEY.LEFT) { pickIndex = (pickIndex + sources.length - 1) % sources.length; cancelAutoPick(); ev.preventDefault(); return; }
      if (k === KEY.DOWN || k === KEY.RIGHT) { pickIndex = (pickIndex + 1) % sources.length; cancelAutoPick(); ev.preventDefault(); return; }
      if (k === KEY.ENTER) { cancelAutoPick(); pickSource(sources[pickIndex]); ev.preventDefault(); return; }
    }
    if (k === KEY.INFO || k === KEY.GREEN || ev.key === 'i' || ev.key === 'g') {
      overlayMode = (overlayMode + 1) % 3; applyOverlayMode(); ev.preventDefault();
    } else if (k === KEY.YELLOW || ev.key === 'y') {
      toggleZ(); ev.preventDefault();
    } else if (k === KEY.BLUE || ev.key === 'b') {
      toggleVideo(); ev.preventDefault();
    } else if (k === KEY.RED || ev.key === 'r') {
      start(); ev.preventDefault();
    } else if (k === KEY.BACK) {
      ev.preventDefault();
      if (padButtonRecent()) { log('Back ignored: gamepad button active (controller B as Back?)'); return; }
      if (overlayMode !== 0) { overlayMode = 0; applyOverlayMode(); }
      else { try { tizen.application.getCurrentApplication().exit(); } catch (e) { log('exit failed ' + errStr(e)); } }
    }
  });

  function applyOverlayMode() {
    els.overlay.className = overlayMode === 2 ? '' : 'hidden';
    els.mini.className = overlayMode === 1 ? '' : 'hidden';
  }

  // ---- Video sources -------------------------------------------------------------------
  function readSources() {
    try {
      tizen.systeminfo.getPropertyValue('VIDEOSOURCE', function (info) {
        allSourcesRaw = info;
        // Shape: info.connected is an array of source objects; the Samsung TVWindow example
        // iterates `connected` and compares type === "HDMI" (verified). `disconnected` and the
        // per-source fields beyond type/number (name) are not verified; raw JSON is logged.
        var conn = (info && info.connected) || [];
        try { log('VIDEOSOURCE raw: ' + JSON.stringify(info)); } catch (e) { /* ignore */ }
        sources = [];
        for (var i = 0; i < conn.length; i++) sources.push(conn[i]);
        log('VIDEOSOURCE connected=' + sources.length + ' disconnected=' + ((info && info.disconnected) || []).length);
        step('VIDEOSOURCE read', 'OK', sources.length + ' connected');
        var hdmi = sources.filter(function (s) { return String(s.type).toUpperCase() === 'HDMI'; });
        if (!hdmi.length) {
          step('HDMI source found', 'FAIL', 'no connected HDMI source (is the PC on, cable in, source not "disconnected"?)');
          return;
        }
        sources = hdmi;
        step('HDMI source found', 'OK', hdmi.length + ' connected HDMI');
        pickIndex = 0; pickerActive = true; pickDeadline = Date.now() + AUTO_PICK_MS;
        autoPickTimer = setTimeout(function () {
          log('auto-picking first HDMI');
          pickSource(sources[0]);
        }, AUTO_PICK_MS);
      }, function (e) {
        step('VIDEOSOURCE read', 'FAIL', 'error callback: ' + errStr(e));
      });
    } catch (e) {
      step('VIDEOSOURCE read', 'FAIL', 'threw ' + errStr(e));
    }
  }

  function cancelAutoPick() { if (autoPickTimer) { clearTimeout(autoPickTimer); autoPickTimer = null; } }

  function pickSource(src) {
    pickerActive = false; cancelAutoPick();
    chosenSource = src;
    log('picked ' + src.type + ' ' + src.number + ' (' + (src.name || '-') + ')');
    try {
      // setSource(videoSource, successCallback, errorCallback, type)
      tizen.tvwindow.setSource(src, function (s, windowType) {
        step('setSource', 'OK', 'callback returned');
        showVideo(zPos);
      }, function (e) {
        step('setSource', 'FAIL', 'error callback: ' + errStr(e));
      }, 'MAIN');
    } catch (e) {
      step('setSource', 'FAIL', 'threw ' + errStr(e));
    }
  }

  function showVideo(z) {
    var name = z === 'BEHIND' ? 'show BEHIND' : 'show FRONT (fallback)';
    try {
      // show(successCallback, errorCallback, rectangle[4 strings], type, zPosition)
      tizen.tvwindow.show(function (rect) {
        videoShown = true; zPos = z;
        step(name, 'OK', 'rect=' + (rect ? rect.join(',') : '?'));
        if (z === 'BEHIND') log('If the screen is black/no video here, z-order BEHIND is not honoured: press Yellow for FRONT.');
        else log('FRONT: video covers the web layer, overlay will be hidden behind it (expected).');
        checkResolution();
        if (!resListening) { resListening = true; listenResolution(); }
      }, function (e) {
        step(name, 'FAIL', 'error callback: ' + errStr(e));
        if (z === 'BEHIND') { log('falling back to FRONT'); showVideo('FRONT'); }
      }, FULLSCREEN_RECT, 'MAIN', z);
    } catch (e) {
      step(name, 'FAIL', 'threw ' + errStr(e));
      if (z === 'BEHIND') { log('falling back to FRONT'); showVideo('FRONT'); }
    }
  }

  function toggleZ() {
    if (!hasTizen || !tizen.tvwindow) return;
    var next = zPos === 'BEHIND' ? 'FRONT' : 'BEHIND';
    log('manual z-order switch to ' + next);
    showVideo(next);
  }

  function toggleVideo() {
    if (!hasTizen || !tizen.tvwindow) return;
    try {
      if (videoShown) {
        tizen.tvwindow.hide(function () { videoShown = false; log('hide OK'); },
          function (e) { log('hide failed ' + errStr(e)); }, 'MAIN');
      } else { showVideo(zPos); }
    } catch (e) { log('toggleVideo threw ' + errStr(e)); }
  }

  // Resolution/refresh reported by the TV for the shown source. getVideoResolution is synchronous
  // and returns {width, height, frequency, ...} (Samsung TVWindow API, since 2.4); there is no callback form.
  var resInfo = 'n/a';
  function checkResolution() {
    try {
      if (typeof tizen.tvwindow.getVideoResolution !== 'function') return;
      var r = tizen.tvwindow.getVideoResolution('MAIN');
      resInfo = r.width + 'x' + r.height + '@' + r.frequency + 'Hz' + (r.aspectRatio ? ' ar=' + r.aspectRatio : '');
      log('video resolution: ' + resInfo + ' raw=' + JSON.stringify(r));
    } catch (e) { log('getVideoResolution threw ' + errStr(e)); }
  }
  // Re-read when the TV reports a resolution change (API is addVideoResolutionChangeListener;
  // there is no addVideoSourceChangeListener).
  function listenResolution() {
    try {
      if (typeof tizen.tvwindow.addVideoResolutionChangeListener !== 'function') return;
      tizen.tvwindow.addVideoResolutionChangeListener(function (r) {
        resInfo = r.width + 'x' + r.height + '@' + r.frequency + 'Hz';
        log('video resolution changed: ' + resInfo);
      }, 'MAIN');
    } catch (e) { log('addVideoResolutionChangeListener threw ' + errStr(e)); }
  }
  var resListening = false;

  // ---- Page visibility ------------------------------------------------------------------
  document.addEventListener('visibilitychange', function () {
    var s = 'visibilitychange -> hidden=' + document.hidden + ' state=' + document.visibilityState;
    visLog.push('[' + ts() + '] hidden=' + document.hidden);
    if (visLog.length > 6) visLog.shift();
    log(s);
  });
  window.addEventListener('blur', function () { log('window blur'); });
  window.addEventListener('focus', function () { log('window focus'); });

  // ---- Gamepad ----------------------------------------------------------------------------
  window.addEventListener('gamepadconnected', function (e) {
    log('gamepadconnected idx=' + e.gamepad.index + ' id="' + e.gamepad.id + '" mapping="' + e.gamepad.mapping + '"');
  });
  window.addEventListener('gamepaddisconnected', function (e) {
    log('gamepaddisconnected idx=' + e.gamepad.index);
  });

  function getPads() {
    var gp = null;
    try { gp = navigator.getGamepads ? navigator.getGamepads() : null; } catch (e) { return null; }
    return gp;
  }

  function padText(pads) {
    if (!pads) return 'navigator.getGamepads unavailable\n';
    var out = '', n = 0, i, j;
    for (i = 0; i < pads.length; i++) {
      var g = pads[i];
      if (!g) continue;
      n++;
      var pressed = [];
      for (j = 0; j < g.buttons.length; j++) if (g.buttons[j].pressed) pressed.push(j);
      var axes = [];
      for (j = 0; j < g.axes.length; j++) axes.push(g.axes[j].toFixed(2));
      out += '#' + g.index + ' "' + g.id + '"\n  mapping="' + g.mapping + '" connected=' + g.connected +
        ' ts=' + Math.round(g.timestamp) + '\n  buttons(' + g.buttons.length + ') pressed=[' + pressed.join(',') + ']' +
        '\n  LT=' + (g.buttons[6] ? g.buttons[6].value.toFixed(2) : '-') + ' RT=' + (g.buttons[7] ? g.buttons[7].value.toFixed(2) : '-') +
        '\n  axes(' + g.axes.length + ') [' + axes.join(', ') + ']\n';
    }
    return (n ? '' : 'no gamepad reported (press a button; pair controller to the TV first)\n') + out;
  }

  // Detect "input is arriving": g.timestamp changes, or any button/axis differs from last poll.
  var lastSig = {};
  function trackInput(pads) {
    if (!pads) return;
    for (var i = 0; i < pads.length; i++) {
      var g = pads[i];
      if (!g) continue;
      var sig = '', j;
      var anyBtn = false;
      for (j = 0; j < g.buttons.length; j++) { sig += g.buttons[j].pressed ? '1' : '0'; if (g.buttons[j].pressed) anyBtn = true; }
      if (anyBtn) lastPadButtonAt = Date.now();
      for (j = 0; j < g.axes.length; j++) sig += ',' + g.axes[j].toFixed(2);
      if (lastSig[i] !== sig || lastGpTs[i] !== g.timestamp) {
        if (lastSig[i] !== sig) lastInputNote = 'pad' + i + ' changed at ' + ts() + ' (hidden=' + document.hidden + ')';
        gpUpdates++;
      }
      lastSig[i] = sig; lastGpTs[i] = g.timestamp;
    }
  }

  // ---- WebSocket smoke test ----------------------------------------------------------------
  var ws = null, wsState = 'disabled', wsInfo = '', wsReconnectTimer = null;
  var pingId = 0, pingTimer = null, hbTimer = null;
  var rtts = [], lastRtt = null, pongs = 0, pingsSent = 0, statesSent = 0;
  var padSeq = [0, 0, 0, 0], lastSent = {}, lastSentAt = {}, announced = {};

  function wsUrl() {
    var u = 'ws://' + PC_HOST + ':' + PC_PORT + '/ws';
    if (PC_TOKEN) u += '?token=' + encodeURIComponent(PC_TOKEN);
    return u;
  }

  window.setPcHost = function (host, token) {
    try { localStorage.setItem('ghostpad.spike.host', host || ''); if (token !== undefined) localStorage.setItem('ghostpad.spike.token', token); } catch (e) { /* ignore */ }
    PC_HOST = host || ''; if (token !== undefined) PC_TOKEN = token;
    wsStop(); wsStart();
  };

  function wsStart() {
    wsStop();
    if (!PC_HOST) { wsState = 'disabled'; wsInfo = 'PC_HOST empty (set in main.js or setPcHost("ip") in the inspector)'; return; }
    wsState = 'connecting'; wsInfo = wsUrl();
    log('ws connecting ' + wsUrl());
    try { ws = new WebSocket(wsUrl()); } catch (e) { wsState = 'error'; wsInfo = 'ctor threw ' + errStr(e); log(wsInfo); scheduleReconnect(); return; }
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () {
      wsState = 'open (hello sent)';
      ws.send(P.encodeHello('tizen-spike'));
      log('ws open, hello sent');
    };
    ws.onmessage = function (ev) {
      if (typeof ev.data === 'string') {
        var c = P.parseControl(ev.data);
        log('ws text: ' + ev.data);
        if (c && c.type === 'welcome') { wsState = 'welcome (backend=' + c.backend + ')'; wsReady(); }
        return;
      }
      var pong = P.decodePong(ev.data);
      if (pong) {
        // RTT = now - echoed t_client, minus daemon processing time (proc_us), per DESIGN.md.
        var rtt = performance.now() - pong.tClient - pong.procUs / 1000;
        lastRtt = rtt; pongs++; rtts.push(rtt); if (rtts.length > 200) rtts.shift();
      }
    };
    ws.onerror = function () { log('ws error event'); };
    ws.onclose = function (ev) {
      wsState = 'closed'; wsInfo = P.closeMessage(ev.code);
      log('ws close code=' + ev.code + ' reason="' + ev.reason + '"');
      stopTimers(); ws = null;
      if (ev.code !== P.CLOSE.TAKEN_OVER && ev.code !== P.CLOSE.VERSION && ev.code !== P.CLOSE.BAD_TOKEN) scheduleReconnect();
    };
  }

  function wsStop() {
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    stopTimers();
    if (ws) { try { ws.onclose = null; ws.close(); } catch (e) { /* ignore */ } ws = null; }
  }
  function scheduleReconnect() { if (PC_HOST && !wsReconnectTimer) wsReconnectTimer = setTimeout(function () { wsReconnectTimer = null; wsStart(); }, 2000); }
  function stopTimers() { if (pingTimer) clearInterval(pingTimer); if (hbTimer) clearInterval(hbTimer); pingTimer = hbTimer = null; }

  function wsReady() {
    lastSent = {}; lastSentAt = {}; announced = {};
    pingTimer = setInterval(function () {
      if (ws && ws.readyState === 1) { ws.send(P.encodePing(++pingId, performance.now())); pingsSent++; }
    }, PING_MS);
    // Heartbeat/forward loop on a timer so STATE keeps flowing at 50 ms even if rAF were throttled.
    hbTimer = setInterval(forwardStates, SEND_HEARTBEAT_MS);
  }

  function forwardStates() {
    if (!ws || ws.readyState !== 1) return;
    var pads = getPads();
    if (!pads) return;
    var now = performance.now();
    for (var i = 0; i < pads.length && i < 4; i++) {
      var g = pads[i];
      if (!g) continue;
      if (g.mapping !== 'standard') continue;       // DESIGN.md: non-standard pads are ignored
      var s = P.mapGamepad(g);
      var changed = !P.statesEqual(s, lastSent[i]);
      if (changed || !announced[i] || now - (lastSentAt[i] || 0) >= SEND_HEARTBEAT_MS) {
        ws.send(P.encodeState(i, padSeq[i]++, s, now));
        lastSent[i] = s; lastSentAt[i] = now; announced[i] = true; statesSent++;
      }
    }
  }

  function median(a) { if (!a.length) return null; var b = a.slice().sort(function (x, y) { return x - y; }); return b[Math.floor(b.length / 2)]; }

  // ---- Render ----------------------------------------------------------------------------------
  function render(pads) {
    var i, s = '';
    for (i = 0; i < STEP_ORDER.length; i++) {
      var r = steps[STEP_ORDER[i]];
      s += '[' + (r.status === 'OK' ? ' OK ' : r.status === 'FAIL' ? 'FAIL' : r.status === 'SKIP' ? 'SKIP' : ' .. ') + '] ' + STEP_ORDER[i] + (r.detail ? ' - ' + r.detail : '') + '\n';
    }
    s += 'z-order requested: ' + zPos + '  video shown: ' + videoShown + '\n';
    els.steps.textContent = s;

    var src = '';
    if (pickerActive) {
      src += 'Pick HDMI source (arrows + Enter), first HDMI auto-picked in ' + (autoPickTimer ? Math.max(0, Math.ceil((pickDeadline - Date.now()) / 1000)) + 's' : 'cancelled') + '\n';
      for (i = 0; i < sources.length; i++) src += (i === pickIndex ? ' > ' : '   ') + sources[i].type + ' #' + sources[i].number + '  ' + (sources[i].name || '') + '\n';
    } else if (chosenSource) {
      src += 'chosen: ' + chosenSource.type + ' #' + chosenSource.number + ' ' + (chosenSource.name || '') + '\n';
    }
    src += 'resolution: ' + resInfo + '\n';
    els.sources.textContent = src;

    els.page.textContent = 'document.hidden=' + document.hidden + ' state=' + document.visibilityState +
      '\nrAF/s=' + rafPerSec + ' (max gap ' + Math.round(maxRafGap) + 'ms)  setInterval ticks/s=' + timerPerSec +
      '\nvisibility log: ' + (visLog.join(' ') || '(no changes)') +
      '\nuptime ' + ts() + '  UA ' + navigator.userAgent.replace(/^Mozilla\/5.0 /, '').slice(0, 110) + '\n';

    els.pads.textContent = 'gamepad updates/s=' + gpUpdatesPerSec + '  last: ' + lastInputNote + '\n' + padText(pads);

    var r2 = median(rtts);
    els.ws.textContent = 'state: ' + wsState + '\n' + wsInfo + '\nSTATE sent=' + statesSent + '  PING sent=' + pingsSent + ' PONG=' + pongs +
      '\nRTT last=' + (lastRtt === null ? '-' : lastRtt.toFixed(1)) + 'ms median=' + (r2 === null ? '-' : r2.toFixed(1)) +
      'ms max=' + (rtts.length ? Math.max.apply(null, rtts).toFixed(1) : '-') + 'ms\n';

    els.log.textContent = logLines.join('\n');

    els.mini.textContent = 'rAF ' + rafPerSec + '/s | pad ' + gpUpdatesPerSec + '/s | hidden=' + document.hidden +
      ' | ws ' + wsState.split(' ')[0] + (lastRtt === null ? '' : ' ' + lastRtt.toFixed(1) + 'ms') + ' | video ' + (videoShown ? zPos : 'off');
  }

  // ---- Main loops --------------------------------------------------------------------------------
  var lastRender = 0;
  function frame(now) {
    rafTicks++;
    var gap = now - lastRafAt; lastRafAt = now; if (gap > maxRafGap) maxRafGap = gap;
    var pads = getPads();
    trackInput(pads);
    if (now - lastRate >= 1000) {
      rafPerSec = rafTicks; timerPerSec = timerTicks; gpUpdatesPerSec = gpUpdates;
      rafTicks = 0; timerTicks = 0; gpUpdates = 0; lastRate = now; maxRafGap = 0;
    }
    if (overlayMode !== 0 && now - lastRender >= 100) { lastRender = now; render(pads); }
    requestAnimationFrame(frame);
  }
  setInterval(function () { timerTicks++; }, 50);

  // Note: setInterval ticks/s is expected to read ~20 when timers run unthrottled. A value near 0
  // while the app is visible means JS timers are being paused; rAF near 0 means rendering paused.

  function start() {
    log('start: UA=' + navigator.userAgent);
    cancelAutoPick(); pickerActive = false; sources = []; chosenSource = null;
    var ok = detect();
    registerKeys();
    if (!ok) {
      ['VIDEOSOURCE read', 'HDMI source found', 'setSource', 'show BEHIND', 'show FRONT (fallback)'].forEach(function (n) { step(n, 'SKIP', 'prerequisite API missing'); });
      return;
    }
    readSources();
  }

  try {
    var h = localStorage.getItem('ghostpad.spike.host'); if (h) PC_HOST = h;
    var t = localStorage.getItem('ghostpad.spike.token'); if (t) PC_TOKEN = t;
  } catch (e) { /* ignore */ }

  applyOverlayMode();
  requestAnimationFrame(frame);
  start();
  wsStart();
})();
