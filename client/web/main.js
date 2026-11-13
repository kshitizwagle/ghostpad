// ghostpad browser test client: Gamepad API -> STATE over WebSocket (DESIGN.md "Send policy").
import {
  MAX_PADS, XB, decodePong, decodeRumble, encodePadGone, encodePing, encodeState, mapGamepad, messageType, MSG, statesEqual,
} from './protocol.js';
import { Transport, buildUrl, initialConfig, lsGet, lsSet, persistConfig } from './transport.js';
import { LatencyTracker, PING_INTERVAL_MS, PollStats, exportStats } from './latency.js';
import { heartbeatDue } from './sendpolicy.js';
import { makeScheduler, normalizePollMode } from './schedulers.js';
import { copyText, renderHud } from './hud.js';
import { RumbleController } from './rumble.js';

// Pings are skipped when the poll loop has not ticked this recently. The daemon watchdog treats any
// frame as liveness, so a stalled poll loop must not be masked by PINGs (held buttons would stick).
const PING_REQUIRES_POLL_MS = 150;
const $ = (id) => document.getElementById(id);

// ---- environment checks --------------------------------------------------
const hasGamepadApi = typeof navigator.getGamepads === 'function';
const secure = window.isSecureContext;
if (!hasGamepadApi || !secure) {
  const b = $('banner-env');
  b.hidden = false;
  b.textContent = (!secure
    ? 'This page is not a secure context, so the browser hides the Gamepad API. '
    : 'navigator.getGamepads is unavailable in this browser. ') +
    'Open it via http://localhost on the PC running the daemon, or use the Tizen app.';
}

// ---- transport -----------------------------------------------------------
// Automatic connects (page load, after a takeover) need a controller to forward; `pads` is
// declared below and only read once the module has run.
const transport = new Transport({
  clientName: 'ghostpad-web',
  canResume: () => [...pads.values()].some((p) => !p.ignored),
});
const cfg = initialConfig();
$('host').value = cfg.host;
$('token').value = cfg.token;

// Auto-connect (default on, remembered): connect on load and come back after a takeover,
// both only when no other client owns the pads. Off: connect only with the Connect button.
const LS_AUTO = 'ghostpad.auto';
$('auto').checked = lsGet(LS_AUTO) !== '0';
transport.setAuto($('auto').checked);
$('auto').addEventListener('change', () => {
  lsSet(LS_AUTO, $('auto').checked ? '1' : '0');
  transport.setAuto($('auto').checked);
});

transport.addEventListener('status', (ev) => {
  const d = ev.detail;
  $('status').dataset.state = d.state;
  $('status-text').textContent = d.message +
    (d.state === 'waiting' && d.owner ? ' Press Connect to take them over.' : '');
  $('backend').textContent = d.state === 'ready' ? d.backend : '';
});
// After every (re)handshake, force a full STATE for each present pad.
transport.addEventListener('ready', () => { for (const p of pads.values()) p.lastSent = null; });

// ---- rumble (RUMBLE -> vibrationActuator) -------------------------------
const rumble = new RumbleController({
  getGamepad: (i) => (hasGamepadApi ? navigator.getGamepads()[i] : null),
  onChange: (pad, large, small) => {
    const p = pads.get(pad);
    if (p && p.ui.rumble) renderRumble(p.ui.rumble, large, small);
  },
});

// ---- latency (PING/PONG) -------------------------------------------------
const lat = new LatencyTracker();
const pollStats = new PollStats();
let lastPollAt = -Infinity;
transport.addEventListener('binary', (ev) => {
  const type = messageType(ev.detail);
  if (type === MSG.RUMBLE) { rumble.onRumble(decodeRumble(ev.detail)); return; }
  if (type !== MSG.PONG) return;
  const p = decodePong(ev.detail);
  if (p) lat.onPong(p, performance.now());
});
transport.addEventListener('status', (ev) => {
  if (ev.detail.state !== 'ready') { lat.dropPending(); rumble.stop(); }
});
setInterval(() => {
  const now = performance.now();
  lat.sweep(now);
  if (!transport.ready || paused || now - lastPollAt > PING_REQUIRES_POLL_MS) return;
  const id = lat.nextPing(now);
  if (!transport.send(encodePing(id, now))) lat.cancel(id);
}, PING_INTERVAL_MS);

function doConnect() {
  const host = $('host').value, token = $('token').value;
  persistConfig(host, token);
  transport.connect(buildUrl(host, token));
}
// The form's submit (button click or Enter in either field) connects; no page navigation.
$('connect-form').addEventListener('submit', (e) => { e.preventDefault(); doConnect(); });
$('disconnect-btn').addEventListener('click', () => transport.disconnect());

// ---- pad state -----------------------------------------------------------
/** pad index -> {seq, lastSent, lastSendAt, state, ignored, id, ui} */
const pads = new Map();
let sentTotal = 0;

function sendBinary(buf, now) {
  if (!transport.send(buf)) return false;
  sentTotal++;
  pollStats.recordSend(now);
  return true;
}

function addPad(gp) {
  if (gp.index >= MAX_PADS) return;
  const ignored = gp.mapping !== 'standard';
  const p = { seq: 0, lastSent: null, lastSendAt: -Infinity, state: null, ignored, id: gp.id, ui: null };
  p.ui = buildPadUi(gp.index, gp.id, ignored);
  pads.set(gp.index, p);
  updatePadCount();
}

function removePad(index, now) {
  const p = pads.get(index);
  if (!p) return;
  if (!p.ignored) sendBinary(encodePadGone(index), now ?? performance.now());
  p.ui.root.remove();
  pads.delete(index);
  rumble.onRumble({ pad: index, large: 0, small: 0 });
  updatePadCount();
}

window.addEventListener('gamepadconnected', (e) => { if (!pads.has(e.gamepad.index)) addPad(e.gamepad); });
window.addEventListener('gamepaddisconnected', (e) => removePad(e.gamepad.index));

// ---- poll tick -----------------------------------------------------------
let paused = document.visibilityState === 'hidden';

/**
 * One poll+send step. Scheduler-agnostic: call it from rAF, setInterval or a
 * MessageChannel loop (M3 experiment). `now` is performance.now().
 */
function tick(now) {
  if (paused) return;
  lastPollAt = now;
  pollStats.recordPoll(now);
  if (!hasGamepadApi) return;
  const list = navigator.getGamepads();
  for (const gp of list) {
    if (!gp) continue;
    if (!pads.has(gp.index)) addPad(gp); // pad present without an event (page loaded after connect)
  }
  for (const [index, p] of pads) {
    const gp = list[index];
    if (!gp) { removePad(index, now); continue; }
    if (p.ignored) continue;
    const s = mapGamepad(gp);
    p.state = s;
    const changed = !statesEqual(s, p.lastSent);
    if (!changed && !heartbeatDue(now, p.lastSendAt)) continue;
    const buf = encodeState(index, p.seq, s, now);
    if (sendBinary(buf, now)) {
      p.seq = (p.seq + 1) & 0xffff;
      p.lastSent = s;
      p.lastSendAt = now;
    }
  }
}

// Scheduler: raf (default) or fast (~4 ms timer); ?poll=raf|fast, switchable from the HUD.
let pollMode = normalizePollMode(new URLSearchParams(location.search).get('poll'));
let scheduler = makeScheduler(pollMode, tick);
$('poll-mode').value = pollMode;
$('poll-mode').addEventListener('change', () => {
  const next = normalizePollMode($('poll-mode').value);
  if (next === pollMode) return;
  scheduler.stop();
  pollMode = next;
  scheduler = makeScheduler(pollMode, tick);
  pollStats.reset();
  const u = new URL(location.href);
  u.searchParams.set('poll', pollMode);
  history.replaceState(null, '', u);
  if (!paused) scheduler.start();
});
// Test hook: read poll stats / mode without scraping the DOM.
window.__ghostpad = { pollStats, lat, get pollMode() { return pollMode; }, get scheduler() { return scheduler; } };

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    paused = true; // stop heartbeats; the server watchdog neutralises the pads
    rumble.stop();
    scheduler.stop();
  } else {
    paused = false;
    pollStats.breakChain();
    for (const p of pads.values()) p.lastSent = null; // resend full state
    scheduler.start();
    const b = $('banner-resume');
    b.hidden = false;
    b.textContent = 'Page was hidden: input was paused and the server released all buttons. Resumed.';
  }
});
if (!paused) scheduler.start();

// ---- UI ------------------------------------------------------------------
const BTN_LABELS = [
  ['A', XB.A], ['B', XB.B], ['X', XB.X], ['Y', XB.Y], ['LB', XB.LEFT_SHOULDER], ['RB', XB.RIGHT_SHOULDER],
  ['Back', XB.BACK], ['Start', XB.START], ['Guide', XB.GUIDE], ['LS', XB.LEFT_THUMB], ['RS', XB.RIGHT_THUMB],
  ['Up', XB.DPAD_UP], ['Down', XB.DPAD_DOWN], ['Left', XB.DPAD_LEFT], ['Right', XB.DPAD_RIGHT],
];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function buildPadUi(index, id, ignored) {
  $('nopads').hidden = true;
  const root = el('div', 'pad' + (ignored ? ' ignored' : ''));
  const h = el('h2', '', `Pad ${index} `);
  h.append(el('small', '', ignored ? `${id} (non-standard mapping, ignored)` : id));
  root.append(h);
  const ui = { root, btns: [], trig: {}, stick: {} };
  if (!ignored) {
    const body = el('div', 'pad-body');
    const grid = el('div', 'btns');
    for (const [label, mask] of BTN_LABELS) {
      const b = el('div', 'btn', label);
      grid.append(b);
      ui.btns.push([b, mask]);
    }
    const analog = el('div', 'analog');
    for (const k of ['lt', 'rt']) {
      const t = el('div', 'trig'), bar = el('div', 'bar'), fill = el('i');
      bar.append(fill);
      const num = el('span', 'num', '0');
      t.append(bar, el('span', '', k.toUpperCase()), num);
      analog.append(t);
      ui.trig[k] = [fill, num];
    }
    for (const [k, label] of [['l', 'L stick'], ['r', 'R stick']]) {
      const s = el('div', 'stick'), box = el('div', 'box'), dot = el('i');
      box.append(dot);
      const num = el('span', 'num', '0, 0');
      s.append(box, el('span', '', label), num);
      analog.append(s);
      ui.stick[k] = [dot, num];
    }
    body.append(grid, analog);
    root.append(body);
    const rum = el('div', 'rumble', '');
    rum.append(el('span', 'k', 'Rumble'), el('span', 'rum-l', 'L 0'), el('span', 'rum-s', 'S 0'));
    rum.title = 'Last RUMBLE magnitudes received from the daemon (large / small motor, 0-255)';
    root.append(rum);
    ui.rumble = rum;
  }
  $('pads').append(root);
  return ui;
}

function renderRumble(rum, large, small) {
  rum.classList.toggle('on', large > 0 || small > 0);
  rum.querySelector('.rum-l').textContent = `L ${large}`;
  rum.querySelector('.rum-s').textContent = `S ${small}`;
}

function updatePadCount() {
  $('padcount').textContent = String(pads.size);
  $('nopads').hidden = pads.size > 0;
}

function renderPad(p) {
  const s = p.state;
  if (!s || p.ignored) return;
  for (const [b, mask] of p.ui.btns) b.classList.toggle('on', (s.buttons & mask) !== 0);
  for (const k of ['lt', 'rt']) {
    p.ui.trig[k][0].style.height = `${(s[k] / 255) * 100}%`;
    p.ui.trig[k][1].textContent = String(s[k]);
  }
  for (const k of ['l', 'r']) {
    const x = s[k + 'x'], y = s[k + 'y'];
    const [dot, num] = p.ui.stick[k];
    dot.style.left = `${50 + (x / 32767) * 50}%`;
    dot.style.top = `${50 - (y / 32767) * 50}%`; // +y is up
    dot.classList.toggle('on', x !== 0 || y !== 0);
    num.textContent = `${x}, ${y}`;
  }
}

// Rendering has its own rAF loop so it stays independent of the (swappable) poll scheduler.
function renderLoop() {
  for (const p of pads.values()) renderPad(p);
  requestAnimationFrame(renderLoop);
}
requestAnimationFrame(renderLoop);

setInterval(() => {
  const now = performance.now();
  $('sent').textContent = String(sentTotal);
  $('rate').textContent = String(pollStats.sendRate(now));
  renderHud(lat.snapshot(), pollStats.snapshot(now));
}, 250);

function statsJson() {
  const now = performance.now();
  return JSON.stringify(exportStats(lat.snapshot(), pollStats.snapshot(now), {
    captured_at: new Date().toISOString(),
    poll_mode: pollMode,
    backend: transport.backend,
    user_agent: navigator.userAgent,
  }), null, 2);
}
$('lat-copy').addEventListener('click', () => copyText(statsJson(), $('lat-json'), $('lat-copy-msg')));
$('lat-reset').addEventListener('click', () => { lat.reset(); pollStats.reset(); $('lat-copy-msg').textContent = ''; });

// Auto-connect on load (served over http(s)) to the remembered PC, without taking the pads
// from another client: it waits for a controller and for the pads to be free.
if (location.protocol.startsWith('http') && $('auto').checked) {
  transport.autoConnect(buildUrl(cfg.host, cfg.token));
}
