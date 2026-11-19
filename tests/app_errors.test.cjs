/* Unit tests for the scan-page error helpers in static/app.js (no browser).
 * Extracted slice is self-contained: esc(), apiErrorMessage(), showScanError(). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('  function esc(s) {');
const end = source.indexOf('  // ===== одиночный скан =====');
assert.ok(start !== -1 && end !== -1 && end > start, 'test slice markers not found');
const slice = source.slice(start, end);

function setup() {
  let written = '';
  const S = { opsInFlight: 0 };
  const scanResult = { set innerHTML(v) { written = v; }, get innerHTML() { return written; } };
  const sandbox = { S, scanResult, GisApi: { request: () => {} } };
  vm.runInNewContext(slice, sandbox);
  return sandbox;
}

// ---- human-readable API error extraction + Pydantic prefix trimming ----
(async () => {
  const s = setup();
  const fmt = s.apiErrorMessage;

  // data.message wins
  assert.equal(fmt({ message: 'Плохой КМ' }, 'fallback'), 'Плохой КМ');
  // string detail
  assert.equal(fmt({ detail: 'Деталь' }, 'fallback'), 'Деталь');
  // Pydantic 422 array: first msg, technical "Value error, " trimmed
  assert.equal(
    fmt({ detail: [{ loc: ['body', 'code'], msg: 'Value error, неверный GTIN', type: 'value_error' }] }, 'fallback'),
    'неверный GTIN');
  // non-abbreviated string elements in detail[] are kept
  assert.equal(fmt({ detail: ['просто строка'] }, 'fallback'), 'просто строка');
  // empty / unknown / missing fall back safely
  assert.equal(fmt({}, 'Запас'), 'Запас');
  assert.equal(fmt(null, 'Запас'), 'Запас');
  assert.equal(fmt({ detail: [] }, undefined), 'Ошибка запроса');
  assert.equal(fmt(undefined, undefined), 'Ошибка запроса');
  console.log('apiErrorMessage extraction/trimming OK');
})().catch(e => { console.error(e); process.exitCode = 1; });

// ---- HTML escaping in error cards (XSS defense) ----
(async () => {
  const s = setup();
  // esc strips markup and quotes
  assert.equal(s.esc('<script>alert(1)</script> & "x" \'y\''),
    '&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot; &#39;y&#39;');
  assert.equal(s.esc(null), '');
  assert.equal(s.esc(undefined), '');
  assert.equal(s.esc(0), '0');

  // showScanError renders message AND retry detail HTML-escaped
  s.showScanError('<img src=x onerror=alert(1)>', '<b> повторите </b>');
  assert.ok(s.scanResult.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'), 'message not escaped');
  assert.ok(!s.scanResult.innerHTML.includes('<img src=x'), 'raw markup leaked');
  assert.ok(!s.scanResult.innerHTML.includes('<b>'), 'retry not escaped');
  assert.ok(s.scanResult.innerHTML.includes('&lt;b&gt; повторите &lt;/b&gt;'), 'retry escaped wrongly');
  console.log('esc/showScanError escaping OK');
})().catch(e => { console.error(e); process.exitCode = 1; });