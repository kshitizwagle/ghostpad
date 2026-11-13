// Latency statistics for the HUD (DESIGN.md "Measurement design"). Pure logic, no DOM,
// no timers: callers pass `now` (performance.now() milliseconds). Runs in browsers and Node.

export const PING_INTERVAL_MS = 250;
export const LOSS_TIMEOUT_MS = 1000;
export const RTT_WINDOW = 240; // 240 samples at 250 ms ~= 60 s
export const POLL_WINDOW = 1000;

// Budget (DESIGN.md "Latency budget"), applied to one-way network time = net RTT / 2.
export const BUDGET_MEDIAN_MS = 5;
export const BUDGET_P99_MS = 16.7;

/** Fixed-capacity ring buffer of numbers. */
export class Ring {
  constructor(capacity) {
    this.cap = capacity;
    this.buf = [];
    this.head = 0;
  }
  push(v) {
    if (this.buf.length < this.cap) this.buf.push(v);
    else { this.buf[this.head] = v; this.head = (this.head + 1) % this.cap; }
  }
  get length() { return this.buf.length; }
  /** Values oldest -> newest. */
  values() {
    return this.buf.length < this.cap ? this.buf.slice() : this.buf.slice(this.head).concat(this.buf.slice(0, this.head));
  }
  clear() { this.buf = []; this.head = 0; }
}

/** Nearest-rank quantile of an ascending array (same rule as the daemon's stats.py). */
export function quantile(sorted, p) {
  if (sorted.length === 0) return null;
  return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
}

/** {n, min, median, p95, p99, max} of an unsorted array; fields null when empty. */
export function summarize(values) {
  const s = values.slice().sort((a, b) => a - b);
  return {
    n: s.length,
    min: s.length ? s[0] : null,
    median: quantile(s, 0.5),
    p95: quantile(s, 0.95),
    p99: quantile(s, 0.99),
    max: s.length ? s[s.length - 1] : null,
  };
}

/** Mean absolute difference of consecutive values (oldest -> newest); null if < 2 values. */
export function jitter(values) {
  if (values.length < 2) return null;
  let sum = 0;
  for (let i = 1; i < values.length; i++) sum += Math.abs(values[i] - values[i - 1]);
  return sum / (values.length - 1);
}

/** PING/PONG tracking: RTT, network RTT (RTT - server proc time), loss. */
export class LatencyTracker {
  constructor({ window = RTT_WINDOW, lossMs = LOSS_TIMEOUT_MS } = {}) {
    this.lossMs = lossMs;
    this.rtt = new Ring(window);
    this.net = new Ring(window);
    this.proc = new Ring(window); // server proc time, ms
    this.pending = new Map(); // id -> sentAt
    this.nextId = 1;
    this.sent = 0;
    this.received = 0;
    this.lost = 0;
    this.late = 0; // PONGs that arrived after being counted lost (or unknown id)
  }

  /** Register a PING being sent; returns its id. */
  nextPing(now) {
    const id = this.nextId;
    this.nextId = (this.nextId + 1) >>> 0;
    this.pending.set(id, now);
    this.sent++;
    return id;
  }

  /** Handle a decoded PONG {id, tClient, procUs}; returns the sample or null if ignored. */
  onPong(p, now) {
    if (!this.pending.has(p.id)) { this.late++; return null; }
    this.pending.delete(p.id);
    const rtt = now - p.tClient;
    if (!(rtt >= 0)) return null; // clock went backwards / garbage echo
    const procMs = p.procUs / 1000;
    const net = Math.max(0, rtt - procMs);
    this.rtt.push(rtt);
    this.net.push(net);
    this.proc.push(procMs);
    this.received++;
    return { rtt, net, procMs };
  }

  /** A PING that could not be sent: forget it without counting it. */
  cancel(id) { if (this.pending.delete(id)) this.sent--; }

  /** Link went down: outstanding pings can no longer be answered; forget them without counting loss. */
  dropPending() { this.sent -= this.pending.size; this.pending.clear(); }

  /** Count pings with no PONG within lossMs as lost. Call periodically. */
  sweep(now) {
    for (const [id, at] of this.pending) {
      if (now - at >= this.lossMs) { this.pending.delete(id); this.lost++; }
    }
  }

  reset() {
    this.rtt.clear(); this.net.clear(); this.proc.clear(); this.pending.clear();
    this.sent = this.received = this.lost = this.late = 0;
  }

  snapshot() {
    const rtt = this.rtt.values();
    const net = this.net.values();
    const halve = (s) => ({ ...s, min: h(s.min), median: h(s.median), p95: h(s.p95), p99: h(s.p99), max: h(s.max) });
    const netS = summarize(net);
    return {
      rtt: summarize(rtt),
      net_rtt: netS,
      one_way: halve(netS), // net RTT / 2: assumes a symmetric path
      jitter: jitter(rtt),
      proc: summarize(this.proc.values()),
      pings: { sent: this.sent, received: this.received, lost: this.lost, late: this.late, pending: this.pending.size },
      window: this.rtt.cap,
    };
  }
}
const h = (v) => (v === null ? null : v / 2);

/** Client-side polling cadence and STATE send rate. */
export class PollStats {
  constructor({ window = POLL_WINDOW } = {}) {
    this.intervals = new Ring(window);
    this.last = null;
    this.polls = 0;
    this.sendTimes = []; // ms timestamps within the last second
    this.sends = 0;
  }
  recordPoll(now) {
    if (this.last !== null) this.intervals.push(now - this.last);
    this.last = now;
    this.polls++;
  }
  /** Forget the previous poll timestamp (after a pause or scheduler switch). */
  breakChain() { this.last = null; }
  recordSend(now) { this.sends++; this.sendTimes.push(now); this.trim(now); }
  trim(now) {
    let i = 0;
    while (i < this.sendTimes.length && now - this.sendTimes[i] > 1000) i++;
    if (i) this.sendTimes.splice(0, i);
  }
  /** STATE packets sent during the last second. */
  sendRate(now) { this.trim(now); return this.sendTimes.length; }
  reset() { this.intervals.clear(); this.last = null; this.polls = 0; this.sendTimes = []; this.sends = 0; }
  snapshot(now) {
    const s = summarize(this.intervals.values());
    return {
      interval_ms: { n: s.n, median: s.median, p99: s.p99, max: s.max },
      polls: this.polls,
      sends: this.sends,
      send_rate_hz: this.sendRate(now),
    };
  }
}

/** 'good' | 'warn' | 'bad' | 'none' for the median one-way time (ms). */
export function gradeMedian(ms) {
  if (ms === null || ms === undefined) return 'none';
  return ms <= BUDGET_MEDIAN_MS ? 'good' : ms <= 2 * BUDGET_MEDIAN_MS ? 'warn' : 'bad';
}
/** 'good' | 'warn' | 'bad' | 'none' for the p99 one-way time (ms). */
export function gradeP99(ms) {
  if (ms === null || ms === undefined) return 'none';
  return ms <= BUDGET_P99_MS ? 'good' : ms <= 2 * BUDGET_P99_MS ? 'warn' : 'bad';
}

export function fmtMs(v, digits = 1) {
  return v === null || v === undefined || Number.isNaN(v) ? '-' : v.toFixed(digits);
}

/**
 * HUD view model from the two snapshots: rows of cells {text, grade}, plus a verdict
 * and extras. Grades apply to the one-way rows only; text always carries the verdict too.
 */
export function buildHudModel(lat, poll) {
  const cols = ['min', 'median', 'p95', 'p99', 'max'];
  const row = (label, s, graded) => ({
    label,
    cells: cols.map((c) => ({
      text: fmtMs(s[c]),
      grade: graded && c === 'median' ? gradeMedian(s[c]) : graded && c === 'p99' ? gradeP99(s[c]) : 'none',
    })),
  });
  const gm = gradeMedian(lat.one_way.median);
  const gp = gradeP99(lat.one_way.p99);
  const word = { good: 'OK', warn: 'SLOW', bad: 'OVER', none: '-' };
  const total = lat.pings.sent - lat.pings.pending;
  return {
    columns: cols,
    rows: [
      row('RTT', lat.rtt, false),
      row('Net RTT', lat.net_rtt, false),
      row('One-way', lat.one_way, true),
    ],
    verdict: {
      median: { text: `median ${fmtMs(lat.one_way.median)} ms ${word[gm]}`, grade: gm },
      p99: { text: `p99 ${fmtMs(lat.one_way.p99)} ms ${word[gp]}`, grade: gp },
    },
    extras: [
      { k: 'Jitter', v: `${fmtMs(lat.jitter, 2)} ms` },
      { k: 'Lost', v: `${lat.pings.lost} / ${total > 0 ? total : 0}` },
      { k: 'Server proc', v: `${fmtMs(lat.proc.median === null ? null : lat.proc.median * 1000, 0)} us` },
      { k: 'Poll median', v: `${fmtMs(poll.interval_ms.median, 2)} ms` },
      { k: 'Poll p99', v: `${fmtMs(poll.interval_ms.p99, 2)} ms` },
      { k: 'STATE rate', v: `${poll.send_rate_hz} /s` },
    ],
  };
}

/** Full JSON export for the "copy stats" button. */
export function exportStats(lat, poll, meta = {}) {
  return { ...meta, rtt_ms: lat, polling: poll };
}
