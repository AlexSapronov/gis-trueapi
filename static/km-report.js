/* Report jobs continue on the server and can be reopened after a page reload. */
class KmReport {
  constructor({json, download}) {
    this.json=json; this.download=download; this.$=id=>document.getElementById(id);
    this.$('km-report-form').onsubmit=e=>{e.preventDefault();this.start();};
    this.$('km-report-template').onclick=()=>this.download('/api/admin/warehouse/km-report/template','КМ.xlsx').catch(e=>this.status(e.message,true));
    this.$('km-report-download').onclick=()=>this.download('/api/admin/warehouse/km-report/'+this.jobId+'/download','КМ_количество_ГТД.xlsx').catch(e=>this.status(e.message,true));
  }
  status(message,error=false) {this.$('km-report-status').textContent=message;this.$('km-report-status').className=error?'wh-bad':'';}
  hasDraft() {return this.busy || !!this.$('km-report-file').value;}
  lock(value) {this.busy=value;this.$('km-report-fields').disabled=value;}
  async start() {
    if(this.busy)return;
    this.lock(true);clearTimeout(this.timer);this.$('km-report-download').hidden=true;
    try {
      const file=this.$('km-report-file').files[0];
      if(!file)throw new Error('Выберите файл .xlsx');
      if(file.size>10*1024*1024)throw new Error('Размер файла превышает 10 МБ');
      const body=new FormData();body.append('file',file);body.append('org_inn',this.$('gtd-org').value);
      this.status('Загружаем таблицу КМ…');
      const job=await this.json('/api/admin/warehouse/km-report',{method:'POST',body});
      this.jobId=job.id;try{sessionStorage.setItem('km-report-job',job.id);}catch(_){}
      await this.poll();
    } catch(e) {this.status(e.message,true);this.lock(false);}
  }
  resume() {
    if(!this.jobId){try{this.jobId=sessionStorage.getItem('km-report-job');}catch(_){}}
    if(this.jobId&&!this.busy){this.lock(true);this.poll();}
  }
  async poll() {
    clearTimeout(this.timer);
    try {
      const job=await this.json('/api/admin/warehouse/km-report/'+this.jobId);
      this.lock(['queued','running'].includes(job.status));
      if(this.busy){
        this.status(`Получаем сведения из ЧЗ: ${job.progress} из ${job.total || '…'} уникальных КМ. Можно переходить в другие разделы.`);
        this.timer=setTimeout(()=>this.poll(),2000);return;
      }
      if(job.status==='failed'){this.status(job.message,true);return;}
      const report=job.report;
      this.$('km-report-download').hidden=false;
      this.status(`Готово. Строк: ${report.total}; уникальных КМ: ${report.unique}; с количеством из ЧЗ: ${report.with_quantity}; с ГТД: ${report.with_gtd}. Пояснения и статус ЧЗ — в файле.`);
    } catch(e) {this.status(e.message,true);this.lock(false);}
  }
}
