// WebSocket connection manager: URL building, hello/welcome handshake,
// auto-reconnect with capped backoff, status events. DOM-free except for
// WebSocket/localStorage/location access, all guarded.
import { CLOSE, closeMessage, encodeHello, parseControl } from './protocol.js';

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
 *  'status'  detail {state, message, url, backend, maxPads, attempt}
 *            state: idle | connecting | handshaking | ready | reconnecting | closed
 *  'ready'   detail welcome object (fired after every successful handshake)
 *  'binary'  detail ArrayBuffer (server -> client binary frame)
 */
export class Transport extends EventTarget {
  constructor({ clientName = 'ghostpad-web', minDelay = 500, maxDelay = 5000 } = {}) {
    super();
    this.clientName = clientName;
    this.minDelay = minDelay;
    this.maxDelay = maxDelay;
    this.ws = null;
    this.state = 'idle';
    this.url = '';
    this.backend = '';
    this.maxPads = 0;
    this.attempt = 0;
    this._timer = null;
    this._wanted = false;
  }

  get ready() { return this.state === 'ready'; }

  connect(url) {
    this.disconnect(true);
    this.url = url;
    this._wanted = true;
    this.attempt = 0;
    this._open();
  }

  disconnect(silent = false) {
    this._wanted = false;
    clearTimeout(this._timer);
    this._timer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try { ws.close(1000); } catch { /* ignore */ }
    }
    if (!silent) this._emit('idle', 'Disconnected.');
    else this.state = 'idle';
  }

  /** Send a binary frame; returns true if actually sent. */
  send(buf) {
    if (this.state !== 'ready' || !this.ws || this.ws.readyState !== 1) return false;
    try { this.ws.send(buf); return true; } catch { return false; }
  }

  _emit(state, message) {
    this.state = state;
    this.dispatchEvent(new CustomEvent('status', { detail: {
      state, message, url: this.url, backend: this.backend,
      maxPads: this.maxPads, attempt: this.attempt,
    } }));
  }

  _open() {
    this._emit(this.attempt === 0 ? 'connecting' : 'reconnecting',
      this.attempt === 0 ? 'Connecting...' : `Reconnecting (attempt ${this.attempt})...`);
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      this._emit('closed', `Invalid URL: ${e.message}`);
      this._wanted = false;
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this._emit('handshaking', 'Connected, waiting for welcome...');
      ws.send(encodeHello(this.clientName));
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      if (typeof ev.data === 'string') {
        const m = parseControl(ev.data);
        if (m && m.type === 'welcome') {
          this.backend = String(m.backend ?? '');
          this.maxPads = Number(m.max_pads ?? 0);
          this.attempt = 0;
          this._emit('ready', `Connected (backend: ${this.backend}).`);
          this.dispatchEvent(new CustomEvent('ready', { detail: m }));
        }
      } else {
        this.dispatchEvent(new CustomEvent('binary', { detail: ev.data }));
      }
    };
    ws.onerror = () => { /* onclose follows */ };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (!this._wanted) return;
      const code = ev.code;
      // 4000/4001/4002 are not fixed by retrying.
      if (code === CLOSE.TAKEN_OVER || code === CLOSE.VERSION || code === CLOSE.BAD_TOKEN) {
        this._wanted = false;
        this._emit('closed', closeMessage(code));
        return;
      }
      this.attempt += 1;
      const delay = Math.min(this.maxDelay, this.minDelay * 2 ** (this.attempt - 1));
      this._emit('reconnecting',
        `${code === 1006 ? 'Connection failed' : closeMessage(code)} Retrying in ${(delay / 1000).toFixed(1)} s (attempt ${this.attempt}).`);
      this._timer = setTimeout(() => { if (this._wanted) this._open(); }, delay);
    };
  }
}
