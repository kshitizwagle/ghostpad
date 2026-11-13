import { test, beforeEach, afterEach, mock } from 'node:test';
import assert from 'node:assert/strict';
import { Transport } from '../transport.js';

class FakeWS {
  static all = [];
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.readyState = 0;
    this.closedWith = null;
    FakeWS.all.push(this);
  }
  send(d) { this.sent.push(d); }
  close(code) { this.closedWith = code; this.readyState = 3; }
  // test drivers
  open() { this.readyState = 1; this.onopen?.(); }
  text(obj) { this.onmessage?.({ data: JSON.stringify(obj) }); }
  serverClose(code) { this.readyState = 3; this.onclose?.({ code, reason: '' }); }
  types() { return this.sent.map((s) => JSON.parse(s).type); }
}

const last = () => FakeWS.all[FakeWS.all.length - 1];
const WELCOME = { type: 'welcome', version: 1, backend: 'mock', max_pads: 4 };

function make(opts = {}) {
  const t = new Transport({ WebSocketImpl: FakeWS, ...opts });
  const states = [];
  t.addEventListener('status', (e) => states.push(e.detail));
  return { t, states };
}

/** Open the newest socket, answer the probe with `owner`, and complete the handshake if free. */
function probeAnswer(owner) {
  const ws = last();
  ws.open();
  assert.deepEqual(ws.types(), ['probe']);
  ws.text({ type: 'status', version: 1, owner });
  return ws;
}

beforeEach(() => { FakeWS.all = []; mock.timers.enable({ apis: ['setTimeout'] }); });
afterEach(() => { mock.timers.reset(); });

test('connect() takes over: hello at once, no probe', () => {
  const { t } = make();
  t.connect('ws://pc/ws');
  last().open();
  assert.deepEqual(last().types(), ['hello']);
  last().text(WELCOME);
  assert.equal(t.state, 'ready');
});

test('reconnect after a drop asks first, then says hello when the pads are free', () => {
  const { t } = make();
  t.connect('ws://pc/ws');
  last().open(); last().text(WELCOME);
  last().serverClose(1006);
  assert.equal(t.state, 'reconnecting');
  mock.timers.tick(500);
  const ws = probeAnswer(null);
  assert.deepEqual(ws.types(), ['probe', 'hello']);
  ws.text(WELCOME);
  assert.equal(t.state, 'ready');
});

test('busy pads: closes, waits, asks again, never sends hello while owned', () => {
  const { t, states } = make({ busyDelay: 3000 });
  t.connect('ws://pc/ws');
  last().open(); last().text(WELCOME);
  last().serverClose(1001);
  mock.timers.tick(500);
  const busy = probeAnswer('tizen-spike');
  assert.deepEqual(busy.types(), ['probe']);
  assert.equal(busy.closedWith, 1000);
  assert.equal(t.state, 'waiting');
  assert.equal(states.at(-1).owner, 'tizen-spike');
  mock.timers.tick(2999);
  assert.equal(FakeWS.all.length, 2);
  mock.timers.tick(1);
  assert.deepEqual(probeAnswer(null).types(), ['probe', 'hello']);
});

test('no status reply (older daemon): hello after the probe timeout', () => {
  const { t } = make({ probeTimeout: 1500 });
  t.autoConnect('ws://pc/ws');
  last().open();
  assert.deepEqual(last().types(), ['probe']);
  mock.timers.tick(1499);
  assert.deepEqual(last().types(), ['probe']);
  mock.timers.tick(1);
  assert.deepEqual(last().types(), ['probe', 'hello']);
  assert.equal(t.state, 'handshaking');
});

test('taken over (4001) without auto mode: final', () => {
  const { t } = make();
  t.connect('ws://pc/ws');
  last().open(); last().text(WELCOME);
  last().serverClose(4001);
  assert.equal(t.state, 'closed');
  mock.timers.tick(60000);
  assert.equal(FakeWS.all.length, 1);
});

test('taken over in auto mode: resumes only with a controller and free pads', () => {
  let hasPad = false;
  const { t } = make({ busyDelay: 3000, canResume: () => hasPad });
  t.setAuto(true);
  t.connect('ws://pc/ws');
  last().open(); last().text(WELCOME);
  last().serverClose(4001);
  assert.equal(t.state, 'waiting');
  mock.timers.tick(3000);
  assert.equal(FakeWS.all.length, 1, 'no controller: no connection attempt');
  assert.equal(t.state, 'waiting');
  hasPad = true;
  mock.timers.tick(1000);
  assert.equal(FakeWS.all.length, 2);
  probeAnswer('ghostpad-web');            // newer client still there
  mock.timers.tick(3000);
  assert.deepEqual(probeAnswer(null).types(), ['probe', 'hello']);
});

test('autoConnect() never takes over and waits for a controller', () => {
  let hasPad = false;
  const { t } = make({ canResume: () => hasPad });
  t.autoConnect('ws://pc/ws');
  assert.equal(FakeWS.all.length, 0);
  assert.equal(t.state, 'waiting');
  hasPad = true;
  mock.timers.tick(1000);
  assert.deepEqual(probeAnswer('tv').types(), ['probe']);
});

test('4000 and 4002 stay final even in auto mode', () => {
  for (const code of [4000, 4002]) {
    FakeWS.all = [];
    const { t } = make();
    t.setAuto(true);
    t.connect('ws://pc/ws');
    last().open(); last().text(WELCOME);
    last().serverClose(code);
    assert.equal(t.state, 'closed');
    mock.timers.tick(60000);
    assert.equal(FakeWS.all.length, 1);
  }
});

test('disconnect() cancels a pending retry and a pending probe', () => {
  const { t } = make();
  t.connect('ws://pc/ws');
  last().open(); last().text(WELCOME);
  last().serverClose(1006);
  t.disconnect();
  mock.timers.tick(60000);
  assert.equal(FakeWS.all.length, 1);
  t.autoConnect('ws://pc/ws');
  last().open();
  t.disconnect();
  mock.timers.tick(60000);
  assert.deepEqual(last().types(), ['probe']);
  assert.equal(t.state, 'idle');
});

test('a status message after hello is ignored', () => {
  const { t } = make();
  t.connect('ws://pc/ws');
  last().open();
  last().text({ type: 'status', version: 1, owner: 'x' });
  assert.equal(last().closedWith, null);
  last().text(WELCOME);
  assert.equal(t.state, 'ready');
});
