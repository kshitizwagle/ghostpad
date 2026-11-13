import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RumbleController, REISSUE_MS } from '../rumble.js';
import { decodeRumble } from '../protocol.js';

function fakeClock() {
  let nextId = 1;
  const timers = new Map();
  return {
    setInterval: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearInterval: (id) => { timers.delete(id); },
    tick() { for (const t of [...timers.values()]) t.fn(); },
    get count() { return timers.size; },
    timers,
  };
}
function fakeActuator({ reset = true, rejects = false } = {}) {
  const a = { plays: [], resets: 0 };
  a.playEffect = (type, p) => { a.plays.push({ type, ...p }); return rejects ? Promise.reject(new Error('x')) : Promise.resolve('complete'); };
  if (reset) a.reset = () => { a.resets++; return Promise.resolve('complete'); };
  return a;
}
function setup(opts) {
  const clock = fakeClock();
  const act = fakeActuator(opts);
  const changes = [];
  const pads = { 1: { vibrationActuator: act } };
  const rc = new RumbleController({
    getGamepad: (i) => pads[i] || null, onChange: (...a) => changes.push(a),
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
  });
  return { clock, act, changes, rc, pads };
}

test('plays dual-rumble with normalised magnitudes immediately', () => {
  const { rc, act } = setup();
  rc.onRumble({ pad: 1, large: 255, small: 51 });
  assert.equal(act.plays.length, 1);
  assert.deepEqual(act.plays[0], { type: 'dual-rumble', startDelay: 0, duration: 200, strongMagnitude: 1, weakMagnitude: 0.2 });
});

test('re-issues every interval while non-zero, uses latest magnitudes', () => {
  const { rc, act, clock } = setup();
  assert.equal(REISSUE_MS, 150);
  rc.onRumble({ pad: 1, large: 100, small: 0 });
  clock.tick(); clock.tick();
  assert.equal(act.plays.length, 3);
  rc.onRumble({ pad: 1, large: 0, small: 255 });
  clock.tick();
  const last = act.plays.at(-1);
  assert.equal(last.strongMagnitude, 0);
  assert.equal(last.weakMagnitude, 1);
  assert.equal(clock.count, 1, 'single timer shared');
});

test('zero magnitudes reset and stop the timer', () => {
  const { rc, act, clock, changes } = setup();
  rc.onRumble({ pad: 1, large: 10, small: 10 });
  rc.onRumble({ pad: 1, large: 0, small: 0 });
  assert.equal(act.resets, 1);
  assert.equal(clock.count, 0);
  const n = act.plays.length;
  clock.tick();
  assert.equal(act.plays.length, n);
  assert.deepEqual(changes.at(-1), [1, 0, 0]);
});

test('zero without reset() falls back to a zero-magnitude effect', () => {
  const { rc, act } = setup({ reset: false });
  rc.onRumble({ pad: 1, large: 10, small: 10 });
  rc.onRumble({ pad: 1, large: 0, small: 0 });
  assert.deepEqual([act.plays.at(-1).strongMagnitude, act.plays.at(-1).weakMagnitude], [0, 0]);
});

test('stop() halts all pads and is idempotent', () => {
  const { rc, act, clock, changes } = setup();
  rc.onRumble({ pad: 1, large: 200, small: 200 });
  rc.stop(); rc.stop();
  assert.equal(act.resets, 1);
  assert.equal(clock.count, 0);
  assert.deepEqual(changes.at(-1), [1, 0, 0]);
});

test('unsupported / missing actuator and throwing or rejecting playEffect are swallowed', async () => {
  const clock = fakeClock();
  const gps = {
    0: {}, // no vibrationActuator
    1: { vibrationActuator: { playEffect() { throw new Error('boom'); } } },
    2: { vibrationActuator: fakeActuator({ rejects: true }) },
  };
  const rc = new RumbleController({ getGamepad: (i) => { if (i === 3) throw new Error('gone'); return gps[i] ?? null; },
    setInterval: clock.setInterval, clearInterval: clock.clearInterval });
  for (const pad of [0, 1, 2, 3, 4]) rc.onRumble({ pad, large: 5, small: 5 });
  clock.tick();
  rc.stop();
  await new Promise((r) => setImmediate(r)); // would surface unhandled rejections
});

test('end to end with decodeRumble', () => {
  const { rc, act } = setup();
  const buf = new Uint8Array([0x05, 1, 128, 0]).buffer;
  rc.onRumble(decodeRumble(buf));
  assert.equal(act.plays.length, 1);
  assert.ok(Math.abs(act.plays[0].strongMagnitude - 128 / 255) < 1e-9);
  rc.onRumble(decodeRumble(new ArrayBuffer(3))); // malformed -> null -> ignored
  assert.equal(act.plays.length, 1);
});
