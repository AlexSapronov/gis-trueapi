/* Administrator-only XML enrichment, scoped to the selected warehouse snapshot. */
class UpdEditor {
  constructor({request, json, download}) {
    this.request=request; this.json=json; this.download=download;
    this.$=id=>document.getElementById(id);
    this.esc=s=>String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    this.$('upd-upload-form').onsubmit=e=>{e.preventDefault();this.upload();};
    this.$('upd-mapping-form').onsubmit=e=>{e.preventDefault();this.prepare();};
    this.$('upd-mapping-form').onchange=()=>this.clearOutput();
    this.$('upd-file').onchange=()=>{this.info=null;this.clearOutput();this.$('upd-mapping-form').hidden=true;};
    this.$('upd-download').onclick=()=>this.run(()=>this.download(this.url+'/download','УПД.xml'));
  }
  hasDraft() {return !!(this.working || this.info || this.$('upd-file').value);}
  setDocument(doc) {
    const key=doc ? [doc.id,doc.version,doc.revision,doc.status,doc.check_current,doc.deleted_at].join(':') : '';
    this.$('upd-panel').hidden=!(doc && doc.kind==='shipment' && doc.status==='completed' && doc.check_current && doc.result?.ok && !doc.deleted_at);
    if(key===this.key)return;
    this.key=key;this.doc=doc;this.info=null;this.url=null;
    this.$('upd-upload-form').reset();this.$('upd-mapping-form').reset();this.$('upd-mapping-form').hidden=true;
    this.$('upd-status').textContent='';this.clearOutput();
  }
  clearOutput() {this.$('upd-download').hidden=true;this.$('upd-preview').hidden=true;}
  async run(fn) {
    if(this.working)return;
    this.working=true;
    for(const id of ['upd-upload-fields','upd-mapping-fields','upd-download'])this.$(id).disabled=true;
    const key=this.key;
    try {await fn();} catch(e) {if(key===this.key){this.$('upd-status').textContent=e.message;this.$('upd-status').className='wh-bad';}}
    finally {this.working=false;for(const id of ['upd-upload-fields','upd-mapping-fields','upd-download'])this.$(id).disabled=false;}
  }
  upload() {return this.run(async()=>{
    const doc=this.doc,key=this.key,file=this.$('upd-file').files[0];
    if(!file)return;
    if(file.size>10*1024*1024)throw new Error('XML должен быть не больше 10 МБ');
    this.info=null;this.clearOutput();this.$('upd-mapping-form').hidden=true;
    this.$('upd-status').textContent='Читаем XML…';this.$('upd-status').className='';
    const form=new FormData();form.append('file',file);
    const info=await this.json('/api/admin/warehouse/documents/'+doc.id+'/upd',{method:'POST',body:form});
    if(key!==this.key)return;
    this.info=info;this.url='/api/admin/warehouse/documents/'+doc.id+'/upd/'+info.upload_id;
    this.$('upd-source').textContent='УПД № '+info.number+' · '+info.rows.length+' строк';
    this.$('upd-mapping-rows').innerHTML=info.rows.map(row=>`<tr><td>${this.esc(row.number)}</td><td><b>${this.esc(row.pn)}</b><br>${this.esc(row.name)}<br><small>${row.source==='saved'?'Сохранённая связь':row.source==='exact'?'PN совпадает':'Выберите соответствие'}</small></td><td>${this.esc(row.quantity)}<br><small>ГТД: ${this.esc((row.declarations || []).join('; ') || 'не указана')}</small></td><td><select required data-upd-row="${this.esc(row.id)}" aria-label="PN в сканах для строки ${this.esc(row.number)}"><option value="">Выберите PN</option><option value="unmarked">Без маркировки — оставить строку</option>${info.choices.map((c,i)=>`<option value="pn:${i}" ${c.pn===row.target_pn?'selected':''}>${this.esc(c.pn)} · ${c.quantity} шт. · ${this.esc(c.gtins.join(', '))}</option>`).join('')}</select></td></tr>`).join('');
    this.$('upd-mapping-form').hidden=false;
    this.$('upd-status').textContent=info.warnings.join(' ');
  });}
  prepare() {return this.run(async()=>{
    const info=this.info,key=this.key,url=this.url;
    if(!info)return;
    this.clearOutput();
    const mapping={};
    for(const select of this.$('upd-mapping-rows').querySelectorAll('select')) {
      if(!select.value)throw new Error('Сопоставьте все строки XML');
      mapping[select.dataset.updRow]=select.value==='unmarked'?null:info.choices[Number(select.value.slice(3))].pn;
    }
    const fillGtd=this.$('upd-fill-gtd').checked;
    this.$('upd-status').textContent=fillGtd?'Получаем ГТД из ЧЗ и проверяем XML. Дождитесь результата…':'Заполняем КМ и проверяем XML…';this.$('upd-status').className='';
    const result=await this.json(url+'/prepare',{method:'POST',body:{mapping,fill_gtd:fillGtd,save_links:this.$('upd-save-links').checked}});
    if(key!==this.key)return;
    if(result.ready===false) {
      const packs=items=>(items || []).map(p=>`${p.codes} × ${p.quantity} шт.`).join('; ');
      const source=rows=>(rows || []).map(r=>`Строка ${r.row}: ${r.quantity} шт.; ГТД: ${(r.declarations || []).join('; ') || 'не указана'}`).join(' | ');
      this.$('upd-preview').innerHTML='<h4>Как должны распределяться КМ</h4><p>Исходные ГТД показаны для сверки. Отсутствие номера ГТД в ЧЗ не означает отсутствия декларации.</p><div class="wh-table"><table><thead><tr><th>PN / GTIN</th><th>Исходные строки и ГТД</th><th>ГТД из ЧЗ</th><th>Количество, шт.</th><th>Целые КМ</th></tr></thead><tbody>'+result.rows.map(r=>`<tr><td>${this.esc(r.pn)}<br>${this.esc(r.gtin)}</td><td>${this.esc(source(r.original_rows))}</td><td>${this.esc(r.declaration_number || (result.gtd_checked?'Не указана в ЧЗ':'Не запрашивалась'))}</td><td>${r.quantity}</td><td>${this.esc(packs(r.packages))}</td></tr>`).join('')+'</tbody></table></div><h4>Вариант границ исходных строк по целым КМ</h4><p>Это рекомендация по количеству. Принадлежность КМ исходным ГТД необходимо подтвердить. Если КМ меньше, чем исходных строк, строки потребуется объединить.</p><div class="wh-table"><table><thead><tr><th>PN</th><th>Исходная группа строк</th><th>Строка варианта</th><th>Новое количество, шт.</th><th>Целые КМ</th></tr></thead><tbody>'+(result.boundaries || []).map(r=>`<tr><td>${this.esc(r.pn)}</td><td>${this.esc(r.source_rows.join(', '))}</td><td>${r.original_row}</td><td>${r.quantity}</td><td>${this.esc(packs(r.packages))}</td></tr>`).join('')+'</tbody></table></div>';
      this.$('upd-preview').hidden=false;
      this.$('upd-status').textContent=result.message;
      this.$('upd-status').className='wh-stale';
      return;
    }
    this.$('upd-preview').innerHTML='<h4>Результат заполнения</h4><div class="wh-table"><table><thead><tr><th>Исходная строка</th><th>PN / GTIN</th><th>ГТД</th><th>Количество, шт.</th><th>КМ</th></tr></thead><tbody>'+result.rows.map(r=>`<tr><td>${this.esc((r.source_rows || [r.source_row]).join(', '))}</td><td>${this.esc(r.pn)}<br>${this.esc(r.gtin)}</td><td>${this.esc(r.declaration_number)}</td><td>${r.quantity}</td><td>${r.codes}</td></tr>`).join('')+'</tbody></table></div>';
    this.$('upd-preview').hidden=false;this.$('upd-download').hidden=false;
    this.$('upd-status').textContent='XML подготовлен и проверен по XSD 5.03. Итоги документа сохранены. '+(result.gtd_filled?'ГТД получены из ЧЗ. ':'ГТД сохранены из исходного XML. ')+'Сохранено связей: '+result.saved_links+'. Страна происхождения взята из исходного XML.';
    this.$('upd-status').className='wh-good';
  });}
}
