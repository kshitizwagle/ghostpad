// WebSocket connection manager: URL building, hello/welcome handshake,
// auto-reconnect with capped backoff (asking first: probe/status), status events.
// DOM-free except for WebSocket/localStorage/location access, all guarded.
import { CLOSE, closeMessage, encodeHello, encodeProbe, parseControl } from './protocol.js';

export const DEFAULT_PORT = 47800;
const LS_HOST = 'ghostpad.host';
const LS_TOKEN = 'ghostpad.token';

export function lsGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
export function lsSet(key, val) {
  try { localStorage.setItem(key, val); } catch { /* storage may be blocked */ }
}

/**
 * Build the ws(s):// URL. `host` may be empty (use page host), "host:port",
 * "host" (adds default port) or a full ws:// / wss:// / http(s):// URL.
 */
export function buildUrl(host, token, loc = globalThis.location) {
  const secure = !!loc && loc.protocol === 'https:';
  const pageHost = loc && loc.host ? loc.host : `localhost:${DEFAULT_PORT}`;
  let h = (host || '').trim();
  let url;
  if (!h) {
    url = `${secure ? 'wss' : 'ws'}://${pageHost}/ws`;
  } else if (/^wss?:\/\//i.test(h)) {
    url = h;
  } else if (/^https?:\/\//i.test(h)) {
    url = h.replace(/^http/i, 'ws');
  } else {
    if (!/:\d+$/.test(h) && !/^\[.*\]$/.test(h)) h += `:${DEFAULT_PORT}`;
    url = `${secure ? 'wss' : 'ws'}://${h}`;
  }
  if (!/^wss?:\/\/[^/]+\/.+/i.test(url)) url = url.replace(/\/+$/, '') + '/ws';
  if (token) url += (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
  return url;
}

export function initialConfig(loc = globalThis.location) {
  const q = new URLSearchParams(loc ? loc.search : '');
  const host = q.get('host') ?? lsGet(LS_HOST) ?? '';
  const token = q.get('token') ?? lsGet(LS_TOKEN) ?? '';
  return { host, token };
}

export function persistConfig(host, token) {
  lsSet(LS_HOST, host);
  lsSet(LS_TOKEN, token);
}

/**
 * Events (EventTarget):
 *  'status'  detail {state, message, url, backend, maxPads, attempt, owner}
 *            state: idle | connecting | handshaking | ready | reconnecting | waiting | closed
 *            waiting: another client owns the pads, or (resume) no controller yet
 *  'ready'   detail welcome object (fired after every successful handshake)
 *  'binary'  detail ArrayBuffer (server -> client binary frame)
 *
 * Connect modes (DESIGN.md "Reconnecting"):
 *  takeover   connect(): a user action. hello at once; takes the pads from any owner.
 *  reconnect  after a drop: probe first, hello only if no client owns the pads.
 *  resume     autoConnect(), and after a takeover (4001) in auto mode: like reconnect,
 *             and only while canResume() holds (a controller is present).
 * Automatic connects therefore never take the pads from another client.
 */
export class Transport extends EventTarget {
  constructor({
    clientName = 'ghostpad-web', minDelay = 500, maxDelay = 5000, busyDelay = 3000,
    probeTimeout = 1500, canResume = () => true, WebSocketImpl = globalThis.WebSocket,
  } = {}) {
    super();
    this.clientName = clientName;
    this.minDelay = minDelay;
    this.maxDelay = maxDelay;
    this.busyDelay = busyDelay;
    this.probeTimeout = probeTimeout;
    this.canResume = canResume;
    this.WebSocketImpl = WebSocketImpl;
    this.auto = false;      // auto mode: after a takeover, resume when the pads are free
    this.ws = null;
    this.state = 'idle';
    this.url = '';
    this.backend = '';
    this.maxPads = 0;
    this.owner = null;      // owner named by the last status reply
    this.attempt = 0;
    this.mode = 'takeover';
    this._timer = null;
    this._probeTimer = null;
    this._wanted = false;
  }

  get ready() { return this.state === 'ready'; }

  /** User action: connect and take the pads, even from another client. */
  connect(url) { this._start(url, 'takeover'); }

  /** Automatic: connect only when the pads are free and canResume() holds. */
  autoConnect(url) { this._start(url, 'resume'); }

  /** Auto mode: after being taken over, keep asking and resume when the pads are free. */
  setAuto(on) { this.auto = !!on; }

  disconnect(silent = false) {
    this._wanted = false;
    this._clearTimers();
    this._drop();
    if (!silent) this._emit('idle', 'Disconnected.');
    else this.state = 'idle';
  }

  /** Send a binary frame; returns true if actually sent. */
  send(buf) {
    if (this.state !== 'ready' || !this.ws || this.ws.readyState !== 1) return false;
    try { this.ws.send(buf); return true; } catch { return false; }
  }

  _start(url, mode) {
    this.disconnect(true);
    this.url = url;
    this._wanted = true;
    this.attempt = 0;
    this._attempt(mode);
  }

  _clearTimers() {
    clearTimeout(this._timer);
    clearTimeout(this._probeTimer);
    this._timer = this._probeTimer = null;
  }

  _drop() {
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try { ws.close(1000); } catch { /* ignore */ }
    }
  }

  _emit(state, message) {
    this.state = state;
    this.dispatchEvent(new CustomEvent('status', { detail: {
      state, message, url: this.url, backend: this.backend,
      maxPads: this.maxPads, attempt: this.attempt, owner: this.owner,
    } }));
  }

  _retry(mode, delay, state, message) {
    this._emit(state, message);
    this._timer = setTimeout(() => { this._timer = null; if (this._wanted) this._attempt(mode); }, delay);
  }

  _attempt(mode) {
    this.mode = mode;
    if (mode === 'resume' && !this.canResume()) {
      this._retry(mode, 1000, 'waiting', 'Waiting for a controller before connecting...');
      return;
    }
    this._emit(this.attempt === 0 && mode === 'takeover' ? 'connecting' : 'reconnecting',
      this.attempt === 0 ? 'Connecting...' : `Reconnecting (attempt ${this.attempt})...`);
    let ws;
    try {
      ws = new this.WebSocketImpl(this.url);
    } catch (e) {
      this._emit('closed', `Invalid URL: ${e.message}`);
      this._wanted = false;
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    const hello = () => {
      clearTimeout(this._probeTimer);
      this._probeTimer = null;
      this._emit('handshaking', 'Connected, waiting for welcome...');
      ws.send(encodeHello(this.clientName));
    };
    ws.onopen = () => {
      if (this.ws !== ws) return;
      if (mode === 'takeover') { hello(); return; }
      ws.send(encodeProbe());
      // A daemon without probe support never answers: fall back to hello.
      this._probeTimer = setTimeout(() => { if (this.ws === ws) hello(); }, this.probeTimeout);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      if (typeof ev.data !== 'string') {
        this.dispatchEvent(new CustomEvent('binary', { detail: ev.data }));
        return;
      }
      const m = parseControl(ev.data);
      if (m && m.type === 'status' && this._probeTimer !== null) {
        this.owner = m.owner == null ? null : String(m.owner);
        if (this.owner === null) { hello(); return; }
        clearTimeout(this._probeTimer);
        this._probeTimer = null;
        this._drop();
        this._retry(mode, this.busyDelay, 'waiting',
          `Pads in use by "${this.owner}". Connecting when they are free...`);
      } else if (m && m.type === 'welcome') {
        this.backend = String(m.backend ?? '');
        this.maxPads = Number(m.max_pads ?? 0);
        this.owner = null;
        this.attempt = 0;
        this.mode = 'reconnect';  // from now on, automatic reconnects ask first
        this._emit('ready', `Connected (backend: ${this.backend}).`);
        this.dispatchEvent(new CustomEvent('ready', { detail: m }));
      }
    };
    ws.onerror = () => { /* onclose follows */ };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      clearTimeout(this._probeTimer);
      this._probeTimer = null;
      if (!this._wanted) return;
      const code = ev.code;
      if (code === CLOSE.TAKEN_OVER && this.auto) {
        this._retry('resume', this.busyDelay, 'waiting',
          `${closeMessage(code)} Connecting again when the pads are free...`);
        return;
      }
      // 4000/4002 are not fixed by retrying; 4001 without auto mode is final.
      if (code === CLOSE.TAKEN_OVER || code === CLOSE.VERSION || code === CLOSE.BAD_TOKEN) {
        this._wanted = false;
        this._emit('closed', closeMessage(code));
        return;
      }
      this.attempt += 1;
      const delay = Math.min(this.maxDelay, this.minDelay * 2 ** (this.attempt - 1));
      const next = this.mode === 'takeover' ? 'reconnect' : this.mode;
      this._retry(next, delay, 'reconnecting',
        `${code === 1006 ? 'Connection failed' : closeMessage(code)} Retrying in ${(delay / 1000).toFixed(1)} s (attempt ${this.attempt}).`);
    };
  }
}
