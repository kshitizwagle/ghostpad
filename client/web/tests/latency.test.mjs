import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  Ring, quantile, summarize, jitter, LatencyTracker, PollStats, gradeMedian, gradeP99, buildHudModel, fmtMs,
} from '../latency.js';
import { heartbeatDue, HEARTBEAT_MS } from '../sendpolicy.js';
import { intervalScheduler, makeScheduler, normalizePollMode } from '../schedulers.js';

test('Ring keeps the newest N values in order', () => {
  const r = new Ring(3);
  for (let i = 1; i <= 5; i++) r.push(i);
  assert.deepEqual(r.values(), [3, 4, 5]);
  r.clear();
  assert.equal(r.length, 0);
});

test('summarize: nearest-rank percentiles, same rule as the daemon', () => {
  const v = Array.from({ length: 100 }, (_, i) => 100 - i); // unsorted 100..1
  assert.deepEqual(summarize(v), { n: 100, min: 1, median: 51, p95: 96, p99: 100, max: 100 });
  assert.deepEqual(summarize([]), { n: 0, min: null, median: null, p95: null, p99: null, max: null });
  assert.equal(quantile([7], 0.99), 7);
});

test('jitter is the mean absolute consecutive difference', () => {
  assert.equal(jitter([1, 3, 2, 6]), (2 + 1 + 4) / 3);
  assert.equal(jitter([5]), null);
  assert.equal(jitter([4, 4, 4]), 0);
});

test('tracker: RTT, network RTT and server proc from a PONG', () => {
  const t = new LatencyTracker();
  const id = t.nextPing(1000);
  const s = t.onPong({ id, tClient: 1000, procUs: 500 }, 1004);
  assert.equal(s.rtt, 4);
  assert.equal(s.net, 3.5);
  assert.equal(s.procMs, 0.5);
  const snap = t.snapshot();
  assert.equal(snap.rtt.median, 4);
  assert.equal(snap.net_rtt.median, 3.5);
  assert.equal(snap.one_way.median, 1.75);
  assert.equal(snap.proc.median, 0.5);
  assert.deepEqual(snap.pings, { sent: 1, received: 1, lost: 0, late: 0, pending: 0 });
});

test('tracker: loss after 1 s, late PONG ignored and counted', () => {
  const t = new LatencyTracker();
  const a = t.nextPing(0), b = t.nextPing(250);
  t.sweep(999);
  assert.equal(t.lost, 0);
  t.sweep(1000); // a is 1000 ms old
  assert.equal(t.lost, 1);
  assert.equal(t.onPong({ id: a, tClient: 0, procUs: 0 }, 1100), null);
  assert.equal(t.late, 1);
  assert.ok(t.onPong({ id: b, tClient: 250, procUs: 0 }, 1100));
  assert.equal(t.snapshot().pings.received, 1);
});

test('tracker: rolling window of 240, negative/garbage RTT ignored, network RTT clamped', () => {
  const t = new LatencyTracker();
  for (let i = 0; i < 300; i++) {
    const id = t.nextPing(i * 250);
    t.onPong({ id, tClient: i * 250, procUs: 0 }, i * 250 + (i < 60 ? 100 : 2));
  }
  const s = t.snapshot();
  assert.equal(s.rtt.n, 240);
  assert.equal(s.rtt.max, 2); // the first 60 slow samples rolled out
  const id = t.nextPing(0);
  assert.equal(t.onPong({ id, tClient: 50, procUs: 0 }, 10), null);
  const id2 = t.nextPing(0);
  assert.equal(t.onPong({ id: id2, tClient: 0, procUs: 9000 }, 4).net, 0);
});

test('tracker: cancel and dropPending keep sent consistent', () => {
  const t = new LatencyTracker();
  const a = t.nextPing(0); t.nextPing(1);
  t.cancel(a);
  assert.equal(t.sent, 1);
  t.dropPending();
  assert.deepEqual([t.sent, t.pending.size, t.lost], [0, 0, 0]);
});

test('PollStats: interval median/p99, break chain, send rate over 1 s', () => {
  const p = new PollStats();
  for (let i = 0; i < 100; i++) p.recordPoll(i * 4 + (i === 99 ? 20 : 0));
  p.breakChain();
  p.recordPoll(5000); // no interval across the break
  const s = p.snapshot(5000);
  assert.equal(s.interval_ms.n, 99);
  assert.equal(s.interval_ms.median, 4);
  assert.equal(s.interval_ms.max, 24);
  for (const t of [3900, 4500, 4950, 5000]) p.recordSend(t);
  assert.equal(p.sendRate(5000), 3); // 3900 is older than 1 s
  assert.equal(p.sends, 4);
});

test('grading follows the budget', () => {
  assert.equal(gradeMedian(5), 'good');
  assert.equal(gradeMedian(5.1), 'warn');
  assert.equal(gradeMedian(10.1), 'bad');
  assert.equal(gradeP99(16.7), 'good');
  assert.equal(gradeP99(20), 'warn');
  assert.equal(gradeP99(40), 'bad');
  assert.equal(gradeMedian(null), 'none');
});

test('HUD model: one-way row graded, empty state renders dashes', () => {
  const t = new LatencyTracker();
  const empty = buildHudModel(t.snapshot(), new PollStats().snapshot(0));
  assert.equal(empty.rows[2].cells[1].text, '-');
  assert.equal(empty.verdict.median.grade, 'none');
  const id = t.nextPing(0);
  t.onPong({ id, tClient: 0, procUs: 200 }, 2.2);
  const m = buildHudModel(t.snapshot(), new PollStats().snapshot(0));
  assert.equal(m.rows[2].label, 'One-way');
  assert.equal(m.rows[2].cells[1].text, '1.0');
  assert.equal(m.rows[2].cells[1].grade, 'good');
  assert.equal(m.rows[0].cells[1].grade, 'none');
  assert.match(m.verdict.median.text, /OK$/);
  assert.equal(fmtMs(1.234, 2), '1.23');
});

test('heartbeat fires at ~50 ms on a 60 Hz tick (issue #4)', () => {
  // Old rule: now - last < 50 on 16.67 ms ticks misses the tick at 50.0 - eps and sends at 66.7.
  const frame = 1000 / 60;
  for (const jitterMs of [0, -0.4, 0.4, -1.5]) {
    let last = 0, sends = [];
    for (let k = 1; k <= 60; k++) {
      const now = k * frame + (k % 2 ? jitterMs : -jitterMs);
      if (heartbeatDue(now, last)) { sends.push(now - last); last = now; }
    }
    assert.ok(sends.length >= 17, `got ${sends.length} heartbeats in 1 s`);
    for (const d of sends) assert.ok(d <= HEARTBEAT_MS + 4, `interval ${d}`);
  }
  // the pre-fix rule really did drift
  let last = 0; const old = [];
  for (let k = 1; k <= 60; k++) { const now = k * frame - 0.01; if (now - last >= HEARTBEAT_MS) { old.push(now - last); last = now; } }
  assert.ok(old[0] > 60, `old rule first interval ${old[0]}`);
});

test('fast scheduler ticks roughly every 4 ms and stops cleanly', async () => {
  let n = 0;
  const s = intervalScheduler(() => n++, 4);
  s.start(); s.start();
  await new Promise((r) => setTimeout(r, 100));
  s.stop();
  const at = n;
  assert.ok(at >= 10 && at <= 30, `ticks ${at}`);
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(n, at);
});

test('scheduler selection', () => {
  assert.equal(normalizePollMode('fast'), 'fast');
  assert.equal(normalizePollMode('bogus'), 'raf');
  assert.equal(normalizePollMode(null), 'raf');
  assert.equal(makeScheduler('fast', () => {}).name, 'fast');
  assert.equal(makeScheduler('msg', () => {}).name, 'msg');
});
