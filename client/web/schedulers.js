// Poll schedulers. Each factory returns {start(), stop(), name}; `fn(nowMs)` is the tick.
// 'raf' follows display refresh (16.7 ms at 60 Hz); 'fast' targets ~4 ms.
// Chrome clamps nested setInterval to >= 4 ms and throttles hidden pages, which is what we want
// here; a MessageChannel loop can go faster but spins a core, so it is kept as an experiment only.

export function rafScheduler(fn) {
  let id = 0, on = false;
  const loop = () => { fn(performance.now()); if (on) id = requestAnimationFrame(loop); };
  return {
    name: 'raf',
    start() { if (!on) { on = true; id = requestAnimationFrame(loop); } },
    stop() { on = false; cancelAnimationFrame(id); },
  };
}

export function intervalScheduler(fn, ms = 4) {
  let id = null;
  return {
    name: 'fast',
    start() { if (id === null) id = setInterval(() => fn(performance.now()), ms); },
    stop() { if (id !== null) { clearInterval(id); id = null; } },
  };
}

/** Experimental: self-posting MessageChannel loop, gated to `ms`. Busy-spins; not offered in the UI. */
export function messageScheduler(fn, ms = 4) {
  let on = false, ch = null, last = -Infinity;
  return {
    name: 'msg',
    start() {
      if (on) return;
      on = true;
      ch = new MessageChannel();
      ch.port1.onmessage = () => {
        if (!on) return;
        const now = performance.now();
        if (now - last >= ms) { last = now; fn(now); }
        ch.port2.postMessage(0);
      };
      ch.port2.postMessage(0);
    },
    stop() { on = false; if (ch) { ch.port1.close(); ch.port2.close(); ch = null; } },
  };
}

export const POLL_MODES = ['raf', 'fast'];

export function normalizePollMode(v) {
  return v === 'fast' ? 'fast' : 'raf';
}

export function makeScheduler(mode, fn) {
  if (mode === 'fast') return intervalScheduler(fn, 4);
  if (mode === 'msg') return messageScheduler(fn, 4);
  return rafScheduler(fn);
}
