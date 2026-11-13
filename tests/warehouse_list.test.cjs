// Exercise list rendering without an installed browser; end-to-end coverage lives in browser_warehouse.py.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/warehouse.js', 'utf8');
const render = source.slice(source.indexOf('  function renderDocumentList()'), source.indexOf('  async function returnToDocuments('));
function setup() {
  let writes = 0, html = '', focused = false;
  const container = {dataset:{}, get innerHTML(){return html;}, set innerHTML(value){writes++; html=value;},
    querySelectorAll:()=>[{dataset:{doc:'a'},focus:()=>{focused=true;}}]};
  const sort = {value:'priority'};
  const context = vm.createContext({document:{querySelectorAll:()=>[],activeElement:null},
    $:id=>id==='document-list'?container:sort, admin:false, documentView:'list',
    listedDocuments:[
      {id:'a',number:'10',date:'2026-11-12',kind:'shipment',org_name:'<script>alert(1)</script>',recipient_name:'"Клиент"',status:'active',scan_count:3},
      {id:'b',number:'2',date:'2026-11-13',kind:'receipt',org_name:'Организация',status:'active',scan_count:0,pending_returns:1,open_issues:1}],
    esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    status:()=> 'В работе',stamp:s=>s||'—'});
  vm.runInContext(render,context);
  return {context,container,sort,writes:()=>writes,focused:()=>focused,draw:()=>context.renderDocumentList()};
}
test('priority preserves server order; number/date/unscanned sorts are selectable and do not mutate it',()=>{
  const x=setup();
  const ids=()=>[...x.container.innerHTML.matchAll(/data-doc="([^"]+)"/g)].map(m=>m[1]);
  x.draw();assert.deepEqual(ids(),['a','b']);
  for (const order of ['number','date','unscanned']) {x.sort.value=order;x.draw();assert.deepEqual(ids(),['b','a']);}
  x.sort.value='priority';x.draw();assert.deepEqual(ids(),['a','b']);
});
test('unchanged polling preserves DOM; changed data restores focused document without scrolling',()=>{
  const x=setup();x.draw();x.draw();assert.equal(x.writes(),1);
  x.context.document.activeElement={closest:()=>({dataset:{doc:'a'}})};
  x.context.listedDocuments[0].scan_count=4;x.draw();
  assert.equal(x.writes(),2);assert.equal(x.focused(),true);
});
test('both layouts escape source data, retain replies/issues and format invoice date without timezone conversion',()=>{
  const x=setup();x.draw();
  assert.ok(!x.container.innerHTML.includes('<script>'));
  assert.ok(x.container.innerHTML.includes('&lt;script&gt;'));
  assert.ok(x.container.innerHTML.includes('12.11.2026'));
  assert.ok(x.container.innerHTML.includes('Обращений: 1'));
  assert.ok(x.container.innerHTML.includes('Ответственный ответил'));
  const html=x.container.innerHTML;x.context.documentView='cards';x.draw();
  assert.equal(x.container.dataset.view,'cards');assert.equal(x.container.innerHTML,html);
});
test('empty results offer filter reset',()=>{
  const x=setup();x.context.listedDocuments=[];x.draw();
  assert.ok(x.container.innerHTML.includes('data-reset-filters'));
  assert.ok(!x.container.innerHTML.includes('wh-list-columns'));
});
