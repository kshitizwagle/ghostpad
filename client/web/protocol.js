// ghostpad protocol v1 — pure functions, no DOM access. Runs in browsers and Node.
// Source of truth: DESIGN.md. All multi-byte fields are little-endian.

export const PROTOCOL_VERSION = 1;

export const MSG = Object.freeze({
  STATE: 0x01,
  PING: 0x02,
  PONG: 0x03,
  PAD_GONE: 0x04,
  RUMBLE: 0x05,
});

export const SIZE = Object.freeze({ STATE: 20, PING: 13, PONG: 17, PAD_GONE: 2, RUMBLE: 4 });

export const CLOSE = Object.freeze({
  NORMAL: 1000,
  VERSION: 4000,
  TAKEN_OVER: 4001,
  BAD_TOKEN: 4002,
});

export const MAX_PADS = 4;

// XInput wButtons bits.
export const XB = Object.freeze({
  DPAD_UP: 0x0001,
  DPAD_DOWN: 0x0002,
  DPAD_LEFT: 0x0004,
  DPAD_RIGHT: 0x0008,
  START: 0x0010,
  BACK: 0x0020,
  LEFT_THUMB: 0x0040,
  RIGHT_THUMB: 0x0080,
  LEFT_SHOULDER: 0x0100,
  RIGHT_SHOULDER: 0x0200,
  GUIDE: 0x0400,
  A: 0x1000,
  B: 0x2000,
  X: 0x4000,
  Y: 0x8000,
});

// Gamepad "standard" button index -> XInput bit. Indices 6/7 (LT/RT) are analog only.
export const BUTTON_MAP = Object.freeze({
  0: XB.A,
  1: XB.B,
  2: XB.X,
  3: XB.Y,
  4: XB.LEFT_SHOULDER,
  5: XB.RIGHT_SHOULDER,
  8: XB.BACK,
  9: XB.START,
  10: XB.LEFT_THUMB,
  11: XB.RIGHT_THUMB,
  12: XB.DPAD_UP,
  13: XB.DPAD_DOWN,
  14: XB.DPAD_LEFT,
  15: XB.DPAD_RIGHT,
  16: XB.GUIDE,
});

export function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

/** Trigger value 0..1 -> 0..255 (clamped). */
export function scaleTrigger(v) {
  if (!Number.isFinite(v)) return 0;
  return Math.round(clamp(v, 0, 1) * 255) + 0; // +0 normalises -0
}

/** Axis value -1..1 -> i16 (clamped to +-32767). */
export function scaleAxis(v, invert = false) {
  if (!Number.isFinite(v)) return 0;
  const c = clamp(v, -1, 1);
  return Math.round((invert ? -c : c) * 32767) + 0;
}

/**
 * Map a Gamepad-like object ({buttons:[{pressed,value}], axes:[]}) in "standard"
 * mapping to a PadState { buttons, lt, rt, lx, ly, rx, ry }.
 */
export function mapGamepad(gp) {
  const b = gp.buttons || [];
  const a = gp.axes || [];
  let buttons = 0;
  for (const idx in BUTTON_MAP) {
    const btn = b[idx];
    if (btn && btn.pressed) buttons |= BUTTON_MAP[idx];
  }
  return {
    buttons,
    lt: b[6] ? scaleTrigger(b[6].value) : 0,
    rt: b[7] ? scaleTrigger(b[7].value) : 0,
    lx: scaleAxis(a[0]),
    ly: scaleAxis(a[1], true),
    rx: scaleAxis(a[2]),
    ry: scaleAxis(a[3], true),
  };
}

export function neutralState() {
  return { buttons: 0, lt: 0, rt: 0, lx: 0, ly: 0, rx: 0, ry: 0 };
}

export function statesEqual(a, b) {
  return (
    !!a && !!b &&
    a.buttons === b.buttons && a.lt === b.lt && a.rt === b.rt &&
    a.lx === b.lx && a.ly === b.ly && a.rx === b.rx && a.ry === b.ry
  );
}

/** STATE (20 bytes). seq wraps to u16, tClientMs to u32. */
export function encodeState(pad, seq, s, tClientMs) {
  const buf = new ArrayBuffer(SIZE.STATE);
  const dv = new DataView(buf);
  dv.setUint8(0, MSG.STATE);
  dv.setUint8(1, pad);
  dv.setUint16(2, seq & 0xffff, true);
  dv.setUint16(4, s.buttons & 0xffff, true);
  dv.setUint8(6, s.lt);
  dv.setUint8(7, s.rt);
  dv.setInt16(8, s.lx, true);
  dv.setInt16(10, s.ly, true);
  dv.setInt16(12, s.rx, true);
  dv.setInt16(14, s.ry, true);
  dv.setUint32(16, Math.floor(tClientMs) >>> 0, true);
  return buf;
}

/** PING (13 bytes). tClientMs is an f64 echoed back in PONG. */
export function encodePing(id, tClientMs) {
  const buf = new ArrayBuffer(SIZE.PING);
  const dv = new DataView(buf);
  dv.setUint8(0, MSG.PING);
  dv.setUint32(1, id >>> 0, true);
  dv.setFloat64(5, tClientMs, true);
  return buf;
}

/** PAD_GONE (2 bytes). */
export function encodePadGone(pad) {
  const buf = new ArrayBuffer(SIZE.PAD_GONE);
  const dv = new DataView(buf);
  dv.setUint8(0, MSG.PAD_GONE);
  dv.setUint8(1, pad);
  return buf;
}

/** PONG (17 bytes) -> {id, tClient, procUs}, or null if malformed. */
export function decodePong(buf) {
  if (!buf || buf.byteLength !== SIZE.PONG) return null;
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== MSG.PONG) return null;
  return {
    id: dv.getUint32(1, true),
    tClient: dv.getFloat64(5, true),
    procUs: dv.getUint32(13, true),
  };
}

/** RUMBLE (4 bytes) -> {pad, large, small}, or null if malformed. */
export function decodeRumble(buf) {
  if (!buf || buf.byteLength !== SIZE.RUMBLE) return null;
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== MSG.RUMBLE) return null;
  return { pad: dv.getUint8(1), large: dv.getUint8(2), small: dv.getUint8(3) };
}

/** Peek the message type of a binary frame, or -1. */
export function messageType(buf) {
  return buf && buf.byteLength > 0 ? new DataView(buf).getUint8(0) : -1;
}

export function encodeHello(clientName) {
  return JSON.stringify({ type: 'hello', version: PROTOCOL_VERSION, client: clientName });
}

/** Parse a text frame from the server. Returns the object or null. */
export function parseControl(text) {
  try {
    const o = JSON.parse(text);
    return o && typeof o === 'object' && typeof o.type === 'string' ? o : null;
  } catch {
    return null;
  }
}

export function closeMessage(code) {
  switch (code) {
    case CLOSE.NORMAL: return 'Closed normally.';
    case CLOSE.VERSION: return 'Protocol version mismatch (4000). Update the client or daemon.';
    case CLOSE.TAKEN_OVER: return 'Taken over by a newer client (4001). Not reconnecting.';
    case CLOSE.BAD_TOKEN: return 'Bad or missing token (4002).';
    default: return `Connection closed (${code}).`;
  }
}
