import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  encodeState, encodePing, encodePadGone, decodePong, decodeRumble, encodeHello,
  mapGamepad, scaleTrigger, scaleAxis, statesEqual, neutralState, XB, closeMessage,
} from '../protocol.js';
import { buildUrl } from '../transport.js';

const hex = (buf) => Buffer.from(buf).toString('hex');
const pad = (over = {}) => {
  const buttons = Array.from({ length: 17 }, () => ({ pressed: false, value: 0 }));
  for (const [i, v] of Object.entries(over.buttons || {})) buttons[i] = typeof v === 'object' ? v : { pressed: !!v, value: +v };
  return { buttons, axes: over.axes || [0, 0, 0, 0] };
};

test('STATE byte layout', () => {
  const buf = encodeState(2, 0x0102, { buttons: 0x1001, lt: 0x10, rt: 0xff, lx: 32767, ly: -32767, rx: 1, ry: -2 }, 0x01020304);
  assert.equal(buf.byteLength, 20);
  // type pad seq(LE) buttons(LE) lt rt lx ly rx ry t_client(LE)
  assert.equal(hex(buf), '0102' + '0201' + '0110' + '10ff' + 'ff7f' + '0180' + '0100' + 'feff' + '04030201');
});

test('STATE wraps seq to u16 and t_client to u32', () => {
  const buf = encodeState(0, 65536 + 5, neutralState(), 2 ** 32 + 7.9);
  const dv = new DataView(buf);
  assert.equal(dv.getUint16(2, true), 5);
  assert.equal(dv.getUint32(16, true), 7);
});

test('PING byte layout', () => {
  const buf = encodePing(0x0a0b0c0d, 1.5);
  assert.equal(buf.byteLength, 13);
  assert.equal(hex(buf), '02' + '0d0c0b0a' + '000000000000f83f');
});

test('PAD_GONE byte layout', () => {
  assert.equal(hex(encodePadGone(3)), '0403');
});

test('PONG decode', () => {
  const b = Buffer.alloc(17);
  b[0] = 3; b.writeUInt32LE(0xdeadbeef, 1); b.writeDoubleLE(123.25, 5); b.writeUInt32LE(777, 13);
  const ab = b.buffer.slice(b.byteOffset, b.byteOffset + 17);
  assert.deepEqual(decodePong(ab), { id: 0xdeadbeef, tClient: 123.25, procUs: 777 });
});

test('PONG decode rejects malformed', () => {
  assert.equal(decodePong(new ArrayBuffer(16)), null);
  const wrong = new ArrayBuffer(17); // type 0
  assert.equal(decodePong(wrong), null);
});

test('RUMBLE decode', () => {
  const ab = new Uint8Array([5, 1, 200, 9]).buffer;
  assert.deepEqual(decodeRumble(ab), { pad: 1, large: 200, small: 9 });
  assert.equal(decodeRumble(new Uint8Array([5, 1, 2]).buffer), null);
});

test('hello JSON', () => {
  assert.deepEqual(JSON.parse(encodeHello('x')), { type: 'hello', version: 1, client: 'x' });
});

test('all 17 buttons map per DESIGN.md', () => {
  const expected = {
    0: 0x1000, 1: 0x2000, 2: 0x4000, 3: 0x8000, 4: 0x0100, 5: 0x0200,
    8: 0x0020, 9: 0x0010, 10: 0x0040, 11: 0x0080,
    12: 0x0001, 13: 0x0002, 14: 0x0004, 15: 0x0008, 16: 0x0400,
  };
  for (const [i, mask] of Object.entries(expected)) {
    const s = mapGamepad(pad({ buttons: { [i]: true } }));
    assert.equal(s.buttons, mask, `button ${i}`);
    assert.equal(s.lt, 0); assert.equal(s.rt, 0);
  }
  // LT/RT (6, 7) are analog only: never set button bits.
  const s = mapGamepad(pad({ buttons: { 6: { pressed: true, value: 1 }, 7: { pressed: true, value: 1 } } }));
  assert.equal(s.buttons, 0);
  assert.equal(s.lt, 255); assert.equal(s.rt, 255);
  // All pressed together.
  const all = {}; for (let i = 0; i < 17; i++) all[i] = true;
  assert.equal(mapGamepad(pad({ buttons: all })).buttons, Object.values(XB).reduce((a, b) => a | b, 0));
});

test('missing buttons/axes tolerated', () => {
  assert.deepEqual(mapGamepad({ buttons: [], axes: [] }), neutralState());
});

test('trigger scaling', () => {
  assert.equal(scaleTrigger(0), 0);
  assert.equal(scaleTrigger(1), 255);
  assert.equal(scaleTrigger(0.5), 128); // round(127.5)
  assert.equal(scaleTrigger(-0.2), 0);
  assert.equal(scaleTrigger(1.7), 255);
  assert.equal(scaleTrigger(NaN), 0);
  assert.ok(Object.is(scaleTrigger(-0), 0));
});

test('axis scaling, Y inversion, clamping', () => {
  assert.equal(scaleAxis(1), 32767);
  assert.equal(scaleAxis(-1), -32767);
  assert.equal(scaleAxis(0.5), 16384); // round(16383.5)
  assert.equal(scaleAxis(3), 32767);
  assert.equal(scaleAxis(-3), -32767);
  assert.equal(scaleAxis(1, true), -32767);
  assert.equal(scaleAxis(-1, true), 32767);
  assert.ok(Object.is(scaleAxis(0, true), 0));
  const s = mapGamepad(pad({ axes: [1, 1, -1, -2] }));
  assert.deepEqual([s.lx, s.ly, s.rx, s.ry], [32767, -32767, -32767, 32767]);
});

test('statesEqual', () => {
  assert.ok(statesEqual(neutralState(), neutralState()));
  assert.ok(!statesEqual(neutralState(), { ...neutralState(), lx: 1 }));
  assert.ok(!statesEqual(null, neutralState()));
});

test('buildUrl', () => {
  const http = { protocol: 'http:', host: 'localhost:47800', search: '' };
  const https = { protocol: 'https:', host: 'pc.lan', search: '' };
  assert.equal(buildUrl('', '', http), 'ws://localhost:47800/ws');
  assert.equal(buildUrl('', '', https), 'wss://pc.lan/ws');
  assert.equal(buildUrl('192.168.1.5', '', http), 'ws://192.168.1.5:47800/ws');
  assert.equal(buildUrl('192.168.1.5:9000', 'a b', http), 'ws://192.168.1.5:9000/ws?token=a%20b');
  assert.equal(buildUrl('ws://h:1/ws', '', http), 'ws://h:1/ws');
  assert.equal(buildUrl('', '', { protocol: 'file:', host: '', search: '' }), 'ws://localhost:47800/ws');
});

test('closeMessage', () => {
  assert.match(closeMessage(4001), /newer client/);
});
