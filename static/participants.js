(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const base = '/api/admin/warehouse/participants-excel';
  const status = $('participants-status'), fields = $('participants-fields');
  const progress = $('participants-progress'), download = $('participants-download');
  const storageKey = 'participants-excel-job';
  let jobId = null, timer;
  GisTheme.bind($('theme-btn'));
  function message(text, error = false) {
    status.textContent = text;
    status.className = error ? 'wh-bad' : '';
  }
  async function json(url, options = {}) {
    const response = await fetch(url, {...options, headers: {'X-Admin-Request': '1'}, cache:'no-store'});
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 403) location.href = '/admin';
      throw new Error(data.message || (typeof data.detail === 'string' ? data.detail : 'Ошибка ' + response.status));
    }
    return data;
  }
  async function poll() {
    clearTimeout(timer);
    try {
      const job = await json(base + '/' + encodeURIComponent(jobId));
      if (['queued', 'running'].includes(job.status)) {
        fields.disabled = true;
        progress.hidden = false;
        progress.max = job.total || 1; progress.value = job.progress || 0;
        message('Проверено уникальных ИНН: ' + job.progress + ' из ' + (job.total || '…'));
        timer = setTimeout(poll, 2000);
        return;
      }
      fields.disabled = false; progress.hidden = true;
      if (job.status !== 'completed') {
        message(job.message || 'Проверка не завершена', true);
        return;
      }
      const r = job.report;
      download.href = base + '/' + encodeURIComponent(jobId) + '/download';
      download.hidden = false;
      message('Готово. Строк: ' + r.total + '; зарегистрированы: ' + r.registered +
        '; РЭП: ' + r.radio + '; заблокированы: ' + r.blocked + '; не проверено: ' + r.unknown +
        '; некорректный ИНН: ' + r.invalid + (r.mock ? '; тестовые строки: ' + r.mock : '') + '.');
    } catch (e) {
      fields.disabled = false; progress.hidden = true; message(e.message, true);
    }
  }
  $('participants-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (fields.disabled) return;
    const body = new FormData(e.currentTarget);
    fields.disabled = true; download.hidden = true; clearTimeout(timer);
    message('Загружаем Excel…');
    try {
      const job = await json(base, {method:'POST', body});
      jobId = job.id;
      try { sessionStorage.setItem(storageKey, jobId); } catch (_) {}
      await poll();
    } catch (error) { fields.disabled = false; message(error.message, true); }
  });
  try { jobId = sessionStorage.getItem(storageKey); } catch (_) {}
  if (jobId) { fields.disabled = true; poll(); }
})();
