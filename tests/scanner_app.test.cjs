/* Load static/app.js in a minimal DOM sandbox and exercise scanner-invoke
 * behaviour: duplicate-Enter suppression, error-card rendering and the
 * backend-online toggle. No browser, no network. */
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const APP = fs.readFileSync(path.join(ROOT, 'static/app.js'), 'utf8');
const FOCUS = fs.readFileSync(path.join(ROOT, 'static/scanner-focus.js'), 'utf8');

function makeEl(id) {
  const _cls = new Set();
  const el = {
    id,
    _listeners: {},
    _cls,
    style: {}, dataset: {},
    textContent: '', innerHTML: '', value: '',
    disabled: false,
    classList: {
      add: (c) => _cls.add(c), remove: (c) => _cls.delete(c),
      toggle: (c, f) => { const n = f === undefined ? !_cls.has(c) : !!f; n ? _cls.add(c) : _cls.delete(c); return n; },
      contains: (c) => _cls.has(c),
    },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatchEvent(e) { e.target = e.target || el; (this._listeners[e.type] || []).slice().forEach((fn) => fn(e)); },
    focus() {},                       // no-op: avoids touching the node-realm `document`
    blur() {},
    appendChild() {}, removeChild() {}, querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    nextElementSibling: null,
  };
  return el;
}

const IDS = ['status-bar', 'status-dot', 'status-title', 'status-sub', 'status-detail',
  'scan-input', 'scan-result', 'batch-input', 'batch-check', 'batch-clear',
  'batch-summary', 'batch-table', 'balance-input', 'balance-check', 'balance-result',
  'sound-btn', 'theme-btn', 'tab-scan', 'tab-batch', 'tab-balance', 'session-stats'];

function buildSandbox({ scans }) {
  const elements = {}; IDS.forEach((id) => elements[id] = makeEl(id));
  let activeElement = null;
  const document = {
    _listeners: {},
    hidden: false, activeElement: null, body: { appendChild() {} },
    getElementById: (id) => elements[id],
    querySelectorAll: () => [],
    querySelector: () => null,   // scanner-focus checks document.querySelector('dialog[open]')
    createElement: () => makeEl('dyn'),
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatchEvent(e) { (this._listeners[e.type] || []).slice().forEach((fn) => fn(e)); },
    set activeElement(v) { activeElement = v; }, get activeElement() { return activeElement; },
  };
  const winListeners = {};
  const sandbox = {
    console, setTimeout, clearTimeout, Date, Promise,
    setInterval: () => 0, clearInterval: () => {},   // app.js polls every 6 s; not needed here
    addEventListener(t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); },
    AbortController: class { constructor() { this.signal = { addEventListener() {}, aborted: false }; } abort() {} },
    navigator: { vibrate() {} },
    localStorage: { _s: new Map(), getItem(k) { return this._s.get(k) ?? null; }, setItem(k, v) { this._s.set(k, String(v)); } },
    sessionStorage: { _s: new Map(), getItem(k) { return this._s.get(k) ?? null; }, setItem(k, v) { this._s.set(k, String(v)); } },
    document,
    fetch: async () => ({
      ok: true,
      json: async () => ({ build_id: 'test-build', mode: 'mock', trueapi: { state: 'ok' }, organizations: [] }),
    }),
    GisApi: {
      request(url, opts) { return scans.url(url, opts); },
    },
    GisTheme: { bind() {} },
    // scanner-focus.js assigns window.ScannerFocus; app.js then reads ScannerFocus
    location: { href: 'http://127.0.0.1/' },
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  // document/window event wiring fixture for scanner-focus + app.js
  vm.runInNewContext(FOCUS, sandbox);
  vm.runInNewContext(APP, sandbox);
  return { sandbox, elements, document, fireTime: (el, t, e) => el.dispatchEvent(Object.assign({ type: t, preventDefault() {} }, e)) };
}

function deferred() {
  let resolve; const promise = new Promise((res) => resolve = res);
  return { promise, resolve, called: false };
}

(async () => {
  // --- double-Enter is suppressed while a scan is in flight ----
  let scanCalls = 0;
  const p = deferred();
  const s = buildSandbox({ scans: { url(u) {
    if (u === '/api/scan') { scanCalls += 1; return p.promise; }
    throw new Error('unexpected GisApi url ' + u);
  } } });
  const input = s.elements['scan-input'];
  const keydown = () => s.fireTime(input, 'keydown', { key: 'Enter' });
  const micro = () => new Promise((r) => setTimeout(r, 0));

  await micro();                       // let load-time checkBackend settle
  input.value = '010464063834521821SERIAL';
  keydown();                           // first Enter -> starts a scan
  await micro();
  input.value = '010464063834521821SERIAL';
  keydown();                           // second Enter while in flight -> must be ignored
  await micro();
  assert(scanCalls === 1, 'duplicate Enter sent ' + scanCalls + ' /api/scan requests, expected 1');
  assert(input.value === '', 'scan input not cleared');
  p.resolve({ kind: 'timeout' });
  await micro(); await micro();
  assert(s.elements['scan-result'].innerHTML.includes('Истекло время ожидания ответа'), 'timeout card not rendered');

  // --- network failure marks server offline and renders an error card ----
  const s2 = buildSandbox({ scans: {
    url(u) { return u === '/api/scan'
      ? Promise.resolve({ kind: 'network' })
      : Promise.reject(new Error('unexpected ' + u)); },
  } });
  await micro(); await micro();
  s2.elements['scan-input'].value = '010464063834521821SERIAL';
  s2.fireTime(s2.elements['scan-input'], 'keydown', { key: 'Enter', preventDefault() {} });
  await micro(); await micro();
  assert(s2.elements['scan-result'].innerHTML.includes('Сервер недоступен'), 'network card not rendered');
  assert(s2.elements['status-title'].textContent.includes('НЕТ СВЯЗИ С СЕРВЕРОМ'), 'backend not marked offline');

  // --- business error (422) renders the human message, HTML-escaped ----
  const s3 = buildSandbox({ scans: {
    url(u) { return u === '/api/scan'
      ? Promise.resolve({ kind: 'biz', data: { message: '<b>Плохой</b> КМ' } })
      : Promise.reject(new Error('unexpected ' + u)); },
  } });
  await micro(); await micro();
  s3.elements['scan-input'].value = '010464063834521821SERIAL';
  s3.fireTime(s3.elements['scan-input'], 'keydown', { key: 'Enter', preventDefault() {} });
  await micro(); await micro();
  assert(s3.elements['scan-result'].innerHTML.includes('&lt;b&gt;Плохой&lt;/b&gt; КМ'), 'biz message not escaped/rendered');
  assert(!s3.elements['scan-result'].innerHTML.includes('<b>Плохой</b>'), 'raw markup leaked into scan card');

  console.log('scanner app.js duplicate-Enter/error-state tests OK');
})().catch((e) => { console.error(e); process.exitCode = 1; });

function assert(cond, msg) { if (!cond) throw new Error(msg); }