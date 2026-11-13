#!/usr/bin/env node
// Regenerates client/tizen/spike/protocol.js from client/web/protocol.js.
// Tizen packaging needs every file inside the .wgt, so the spike cannot import ../../web/.
//   node client/tizen/sync-protocol.mjs          write the copy
//   node client/tizen/sync-protocol.mjs --check  exit 1 if the committed copy differs
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('../web/protocol.js', import.meta.url));
const DST = fileURLToPath(new URL('./spike/protocol.js', import.meta.url));

const HEADER = `// GENERATED from client/web/protocol.js (ghostpad protocol v1). DO NOT EDIT BY HAND.
// Regenerate with: node client/tizen/sync-protocol.mjs   (CI/test runs it with --check)
// Tizen packaging needs every file inside the .wgt, so it cannot import ../../web/.
// Differences from the original, and nothing else:
//   1. \`export\` keywords removed, so this loads as a classic <script> (no ES modules).
//   2. \`catch {\` rewritten to \`catch (e) {\` (ES2017-safe).
//   3. Everything exported is exposed on window.GhostpadProtocol at the bottom.
// Source of truth for the wire format is DESIGN.md.

(function () {
'use strict';
`;

export function generate(src) {
  src = src.replace(/\r\n/g, '\n');
  const names = [];
  let body = src.replace(/^export (const|function|class) ([A-Za-z_$][\w$]*)/gm, (_, kw, name) => {
    names.push(name);
    return `${kw} ${name}`;
  });
  if (/^\s*export\b/m.test(body)) throw new Error('unsupported export form in protocol.js (use `export const|function`)');
  body = body.replace(/\bcatch \{/g, 'catch (e) {');
  const lines = [];
  let cur = '  ';
  for (const n of names) {
    const item = `${n}: ${n}, `;
    if ((cur + item).length > 90) { lines.push(cur.trimEnd()); cur = '  '; }
    cur += item;
  }
  lines.push(cur.trimEnd().replace(/,$/, ''));
  return `${HEADER}${body.replace(/\n*$/, '\n')}\nwindow.GhostpadProtocol = {\n${lines.join('\n')}\n};\n})();\n`;
}

export function check() {
  const want = generate(readFileSync(SRC, 'utf8'));
  let have = null;
  try { have = readFileSync(DST, 'utf8'); } catch { /* missing */ }
  return { ok: have === want, want };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const { ok, want } = check();
  if (process.argv.includes('--check')) {
    if (!ok) {
      console.error('client/tizen/spike/protocol.js is out of date. Run: node client/tizen/sync-protocol.mjs');
      process.exit(1);
    }
    console.log('client/tizen/spike/protocol.js is up to date.');
  } else {
    writeFileSync(DST, want);
    console.log(ok ? 'unchanged' : 'wrote client/tizen/spike/protocol.js');
  }
}
