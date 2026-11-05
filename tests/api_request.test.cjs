/* Deterministic fetch/clock tests; no npm dependencies or real waiting. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/api-request.js'), 'utf8');
function setup(fetch) {
  let now = 0, sequence = 0;
  const jobs = new Map();
  const sandbox = {AbortController, fetch,
    setTimeout(fn, delay) {const id = ++sequence; jobs.set(id, {at:now+delay, fn}); return id;},
    clearTimeout(id) {jobs.delete(id);}};
  vm.runInNewContext(source, sandbox);
  return {api:sandbox.GisApi, jobs, async tick(delay) {
    now += delay;
    for (const [id, job] of [...jobs]) if (job.at <= now) {jobs.delete(id); job.fn();}
    for (let n=0;n<8;n++) await Promise.resolve();
  }};
}
const response = (status, data) => ({status, ok:status>=200&&status<300, json:async()=>data});
(async () => {
  // A scan returning after the old 15 s browser limit must still complete.
  let resolveFetch, signal, options;
  const s=setup((url, opts)=>{signal=opts.signal;options=opts;return new Promise(resolve=>{resolveFetch=resolve;});});
  const result=s.api.request('/api/scan',{method:'POST',body:'{}'});
  await s.tick(20000);
  assert.equal(signal.aborted,false);
  resolveFetch(response(200,{api_checked:true,status:'INTRODUCED'}));
  assert.equal((await result).kind,'ok');
  assert.equal(s.jobs.size,0);
  assert.equal(options.method,'POST');

  // Browser deadline is a timeout, not evidence that the backend is offline.
  const t=setup((url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('abort')))));
  const timeout=t.api.request('/api/scan');
  await t.tick(45000);
  assert.equal((await timeout).kind,'timeout');
  assert.equal(t.jobs.size,0);

  // Keep the deadline until the JSON body has arrived, not just its headers.
  const b=setup(async(url,opts)=>({status:200,ok:true,json:()=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('abort body'))))}));
  const body=b.api.request('/api/scan');
  await b.tick(0);await b.tick(45000);
  assert.equal((await body).kind,'timeout');

  const n=setup(async()=>{throw new TypeError('Failed to fetch');});
  assert.equal((await n.api.request('/api/scan')).kind,'network');
  const malformed=setup(async()=>({status:200,ok:true,json:async()=>{throw new Error('HTML instead of JSON');}}));
  assert.equal((await malformed.api.request('/api/scan')).kind,'server');
  const empty=setup(async()=>response(200,null));
  assert.equal((await empty.api.request('/api/scan')).kind,'server');
  const business=setup(async()=>response(422,{message:'Invalid GTIN'}));
  assert.equal((await business.api.request('/api/balance')).kind,'biz');
  const upstream=setup(async()=>response(200,{error_category:'timeout',api_checked:true}));
  assert.equal((await upstream.api.request('/api/scan')).kind,'ok');
  const custom=setup(async(url,opts)=>{assert.equal('timeoutMs' in opts,false);return response(200,{results:[]});});
  await custom.api.request('/api/scan_batch',{timeoutMs:120000});
  assert.equal(custom.jobs.size,0);
  console.log('Core request regression tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
