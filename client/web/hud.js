// DOM rendering for the latency HUD. Logic lives in latency.js (DOM-free, tested).
import { buildHudModel } from './latency.js';

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

export function renderHud(lat, poll) {
  const m = buildHudModel(lat, poll);
  const v = document.getElementById('lat-verdict');
  v.replaceChildren(
    ...[m.verdict.median, m.verdict.p99].map((x) => el('span', x.grade, x.text)),
  );
  const t = document.getElementById('lat-table');
  const head = el('tr');
  head.append(el('th', '', 'ms'), ...m.columns.map((c) => el('th', '', c)));
  const rows = m.rows.map((r) => {
    const tr = el('tr');
    tr.append(el('td', '', r.label), ...r.cells.map((c) => el('td', c.grade, c.text)));
    return tr;
  });
  t.replaceChildren(head, ...rows);
  document.getElementById('lat-extras').replaceChildren(...m.extras.map((x) => {
    const d = el('div');
    d.append(el('span', 'k', x.k), el('span', 'v', x.v));
    return d;
  }));
}

/** Copy text to the clipboard; falls back to showing it selected in the textarea. */
export async function copyText(text, area, msg) {
  try {
    await navigator.clipboard.writeText(text);
    msg.textContent = 'Copied.';
    area.hidden = true;
    return true;
  } catch {
    area.hidden = false;
    area.value = text;
    area.focus();
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* ignore */ }
    msg.textContent = ok ? 'Copied.' : 'Clipboard blocked: select the text below and copy it.';
    return ok;
  }
}
