/* Bulk customs correction runs in the background and survives page reloads. */
class GtdExcel {
  constructor({json,download}) {
    this.json=json;this.download=download;this.$=id=>document.getElementById(id);
    this.esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    this.$('gtd-excel-form').onsubmit=e=>{e.preventDefault();this.start();};
    this.$('gtd-catalog-form').onsubmit=e=>{e.preventDefault();this.catalog();};
    this.$('gtd-catalog-template').onclick=()=>this.download('/api/admin/warehouse/gtd-catalog/template','GoodID_GTIN.xlsx').catch(e=>this.status(e.message,true));
    this.$('gtd-excel-download').onclick=()=>this.download('/api/admin/warehouse/gtd-excel/'+this.jobId+'/download','ГТД.xlsx').catch(e=>this.status(e.message,true));
    this.$('gtd-excel-only-changed').onchange=()=>this.renderRows();
  }
  status(text,error=false) {this.$('gtd-excel-status').textContent=text;this.$('gtd-excel-status').className=error?'wh-bad':'';}
  hasDraft() {return this.busy || !!this.$('gtd-excel-file').value || !!this.$('gtd-catalog-file').value;}
  lock(value) {this.busy=value;this.$('gtd-excel-fields').disabled=value;this.$('gtd-catalog-fields').disabled=value;}
  file(id) {
    const file=this.$(id).files[0];
    if(!file)throw new Error('Выберите файл .xlsx');
    if(file.size>10*1024*1024)throw new Error('Размер файла превышает 10 МБ');
    const body=new FormData();body.append('file',file);return body;
  }
  async catalog() {
    if(this.busy)return;this.lock(true);
    try {
      const result=await this.json('/api/admin/warehouse/gtd-catalog',{method:'POST',body:this.file('gtd-catalog-file')});
      this.$('gtd-catalog-result').textContent=`Добавлено: ${result.added}. Обновлено: ${result.updated}. Без изменений: ${result.unchanged}.`;
      this.$('gtd-catalog-form').reset();
    } catch(e) {this.$('gtd-catalog-result').textContent=e.message;} finally {this.lock(false);}
  }
  async start() {
    if(this.busy)return;this.lock(true);clearTimeout(this.timer);
    this.$('gtd-excel-download').hidden=true;this.report=null;this.renderRows();
    try {
      const job=await this.json('/api/admin/warehouse/gtd-excel',{method:'POST',body:this.file('gtd-excel-file')});
      this.jobId=job.id;try{sessionStorage.setItem('gtd-excel-job',job.id);}catch(_){}
      this.status('Проверка запущена…');await this.poll();
    } catch(e) {this.status(e.message,true);this.lock(false);}
  }
  resume() {
    if(!this.jobId){try{this.jobId=sessionStorage.getItem('gtd-excel-job');}catch(_){}}
    if(this.jobId&&!this.busy){this.lock(true);this.poll();}
  }
  async poll() {
    clearTimeout(this.timer);
    try {
      const job=await this.json('/api/admin/warehouse/gtd-excel/'+this.jobId);
      this.lock(job.status==='queued'||job.status==='running');
      if(this.busy) {
        this.status(`Проверяем ${job.filename}. Обработано отгрузок: ${job.progress} из ${job.total || '…'}. Можно оставаться в других разделах.`);
        this.timer=setTimeout(()=>this.poll(),2000);return;
      }
      if(job.status==='failed'){this.status(job.message,true);return;}
      this.report=job.report;this.renderRows();this.$('gtd-excel-download').hidden=false;
      this.status(`Готово. Исходных строк: ${job.report.total}. Изменено / пересобрано: ${job.report.changed}. Подтверждено без изменений: ${job.report.confirmed}. Не подтверждено: ${job.report.unconfirmed}. Неподтверждённые строки сохранены; пояснения есть в файле.`);
    } catch(e) {this.status(e.message,true);this.lock(false);}
  }
  renderRows() {
    const rows=(this.report?.rows || []).filter(r=>!this.$('gtd-excel-only-changed').checked||r.changed);
    const labels={changed:'Изменена / пересобрана',confirmed:'Подтверждена',unconfirmed:'Не подтверждена'};
    this.$('gtd-excel-rows').innerHTML=rows.map(r=>`<tr class="${r.changed?'wh-gtd-changed':''}"><td>${this.esc(r.sheet)} / ${r.row}</td><td>${this.esc(r.number)}<br>${this.esc(r.pn)}${r.warehouse_number?'<br>Склад: '+this.esc(r.warehouse_number):''}</td><td>${this.esc(r.before)}</td><td>${this.esc(r.after)}</td><td>${labels[r.status]}</td><td>${this.esc(r.reason)}<br>${this.esc(r.match_source || '')}</td></tr>`).join('');
  }
}
