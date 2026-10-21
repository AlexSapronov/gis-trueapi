(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const admin = document.body.dataset.admin === 'true';
  let key = '', kind = new URLSearchParams(location.search).get('kind') === 'receipt' ? 'receipt' : 'shipment';
  let current = null, busy = 0, flushing = false, queue = [], dirty = false, refreshing = false, queueError = false;
  let editVersion = null, editRevision = null, editLines = [];
  let pendingReload = false, buildId = document.body.dataset.build, noticeTimer;
  const input = $('wh-scan');
  const apiBase = admin ? '/api/admin/warehouse' : '/api/warehouse';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stamp = s => s ? new Date(s).toLocaleString('ru-RU') : '—';
  const status = d => ({draft:d.kind === 'shipment' ? 'Ожидает сборки' : 'Ожидает приёмки',active:d.kind === 'shipment' ? 'В сборке' : 'В приёмке',completed:d.kind === 'shipment' ? 'Собран' : 'Принят'}[d.status]);
  function message(s, error = false) {
    clearTimeout(noticeTimer);
    const target = !admin && current ? $('scan-feedback') : $('notice');
    $('notice').textContent = '';
    target.textContent = s;
    target.className = error ? 'wh-bad' : 'wh-good';
  }
  function maybeReload() {
    if (pendingReload && !busy && !flushing && !queue.length && !input?.value && !dirty && !$('duplicate').open) location.reload();
  }
  async function request(path, options = {}) {
    const headers = {...options.headers};
    if (admin) headers['X-Admin-Key'] = key;
    if (options.body && !(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, {...options, headers, cache:'no-store'});
    if (!response.ok) {
      let d; try { d = await response.json(); } catch (_) { d = {}; }
      throw new Error(d.message || (typeof d.detail === 'string' ? d.detail : `Ошибка ${response.status}`));
    }
    return response;
  }
  async function json(path, options) { return (await request(path, options)).json(); }
  async function action(fn) {
    busy++;
    try { return await fn(); } catch (e) { message(e.message || 'Ошибка соединения', true); }
    finally { busy--; maybeReload(); }
  }
  async function download(path, fallback) {
    const r = await request(path), blob = await r.blob();
    let filename = fallback;
    const m = /filename\*=UTF-8''(.+)/.exec(r.headers.get('Content-Disposition') || '');
    if (m) filename = decodeURIComponent(m[1]);
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = filename; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function pendingKey() { return current ? 'warehouse-unsent-' + current.id : ''; }
  function savePending() {
    if (!current || admin) return;
    // Only unacknowledged input is cached. The server is authoritative for saved scans.
    try { sessionStorage.setItem(pendingKey(), JSON.stringify({queue, partial:input.value})); }
    catch (_) { message('Не удалось сохранить неподтверждённый ввод в браузере. Не закрывайте страницу до отправки.', true); }
  }
  function pendingState() {
    if (admin) return;
    if (queue.length && queueError && !flushing) {
      $('pending').innerHTML = `Ожидают сохранения: ${queue.length} <button class="btn" id="retry-pending">Повторить отправку</button> <button class="btn" id="discard-pending">Удалить первый неподтверждённый код</button>`;
      $('retry-pending').onclick = () => flush();
      $('discard-pending').onclick = () => { if (confirm('Удалить первый код из очереди? На сервер он не подтверждён.')) { queue.shift(); queueError = false; savePending(); pendingState(); flush(); } };
    } else $('pending').textContent = queue.length ? `Ожидают сохранения на сервере: ${queue.length}` : '';
    $('check').disabled = !current || current.status !== 'active' || current.checking || queue.length > 0 || flushing;
    $('complete').disabled = !current?.can_complete || queue.length > 0 || flushing || !!input.value;
  }
  const focus = input ? ScannerFocus.create({input, active:() => !!current && current.status === 'active' && !$('duplicate').open,
    submit:code => enqueue(code), resume:() => refresh()}) : null;

  async function list() {
    const data = await json(apiBase + '/documents?kind=' + kind);
    document.querySelectorAll('[data-kind]').forEach(b => b.classList.toggle('primary', b.dataset.kind === kind));
    $('document-list').innerHTML = data.documents.length ? data.documents.map(d => `<button class="wh-doc" data-doc="${esc(d.id)}"><b>№ ${esc(d.number)} · ${esc(d.date)}</b><span>${esc(d.org_name)}</span>${d.recipient_name ? `<span>Получатель: ${esc(d.recipient_name)}</span>` : ''}<span>${esc(status(d))} · ${d.scan_count} КМ</span>${d.kind === 'shipment' ? `<span>${d.strict_owner ? 'Строгое соответствие' : 'Наши организации'}</span>` : ''}<span>Изменён: ${esc(stamp(d.updated_at))}</span></button>`).join('') : '<p>Документов пока нет</p>';
  }
  async function open(id) {
    if (queue.length || flushing || input?.value) { message('Сначала сохраните ожидающие КМ и завершите ввод.', true); return; }
    const d = await json(apiBase + '/documents/' + id);
    current = d; dirty = false; queue = [];
    $('document').hidden = false; $('document-list').hidden = true;
    if ($('import-box')) $('import-box').hidden = true;
    history.replaceState(null, '', location.pathname + '?kind=' + kind + '&doc=' + encodeURIComponent(id));
    if (!admin) {
      try { const cached = JSON.parse(sessionStorage.getItem(pendingKey()) || '{}'); queue = cached.queue || []; input.value = cached.partial || ''; }
      catch (_) { queue = []; }
    }
    render(); focus?.focus();
    if (queue.length) flush();
  }
  function render() {
    if (!current) return;
    const d = current, result = d.result;
    $('doc-title').textContent = (d.kind === 'shipment' ? 'Отгрузка' : 'Приёмка') + ' № ' + d.number + ' от ' + d.date;
    $('doc-parties').textContent = `${d.org_name} · ИНН ${d.org_inn}` + (d.recipient_name ? ` → ${d.recipient_name} · ИНН ${d.recipient_inn}` : '');
    $('doc-status').textContent = `${status(d)} · Изменён ${stamp(d.updated_at)}` + (d.kind === 'shipment' ? ` · ${d.strict_owner ? 'Строгое соответствие юридическому лицу' : 'Допустимы КМ наших организаций'}` : '');
    $('doc-count').textContent = `Накоплено: ${d.scans.length} уникальных КМ` + (d.kind === 'receipt' ? ` · Ожидается: ${d.expected.length} · Проверено: ${result?.checked_count ?? 0} · Подтверждено: ${result?.accepted_count ?? 0}/${d.expected.length}` : '');
    const state = d.checking ? 'Проверка выполняется…' : !result ? 'КМ накапливаются. Проверка не выполнялась.' : !d.check_current ? 'Данные предыдущей проверки. Состав изменён, требуется повторная проверка.' : result.ok ? 'Проверено. Документ полностью совпадает.' : 'Проверено. Есть расхождения.';
    $('check-state').textContent = state;
    $('check-state').className = !d.check_current || d.checking ? 'wh-stale' : result?.ok ? 'wh-good' : 'wh-bad';
    $('actual-label').textContent = d.kind === 'shipment' ? 'Собрано' : 'Принято';
    const actual = new Map((result?.rows || []).map(r => [r.gtin,r]));
    $('lines').innerHTML = d.lines.map(l => { const r = actual.get(l.gtin); return `<tr><td>${esc(l.pn || 'Не определён')}</td><td>${esc(l.gtin)}</td><td>${l.quantity}</td><td>${r ? r.actual : 'Не проверено'}</td><td class="${r?.remaining ? 'wh-bad' : ''}">${r ? r.remaining : 'Не проверено'}</td></tr>`; }).join('');
    $('scan-total').textContent = '(' + d.scans.length + ')';
    const details = new Map((result?.codes || []).map(c => [c.scan_id,c]));
    $('scans').innerHTML = d.scans.map(s => { const r = details.get(s.id); return `<div class="wh-code"><b>${esc(s.gtin)}</b> · ${esc(stamp(s.scanned_at))}<br><code>${esc(s.cis)}</code>${r ? `<p>${esc(r.status || '')} ${esc(r.owner_name || r.owner_inn || '')} ${r.quantity == null ? '' : ' · ' + r.quantity + ' шт.'}</p><p class="wh-bad">${esc(r.errors.join('; '))}</p>` : '<p>КМ не проверен</p>'}${!admin && d.status === 'active' ? `<button class="btn" data-delete="${esc(s.id)}">Удалить КМ из документа</button>` : ''}</div>`; }).join('');
    $('check-errors').innerHTML = result ? (result.codes.some(c => c.errors.length) ? `<p class="wh-bad">Ошибочных КМ: ${result.codes.filter(c => c.errors.length).length}. Причины и владелец указаны в общем списке КМ ниже.</p>` : '') + (result.warnings || []).map(w => `<p class="wh-stale">${esc(w)}</p>`).join('') + (result.missing.length ? `<h3>Отсутствующие КМ: ${result.missing.length}</h3>` + result.missing.map(m => `<p class="wh-code"><code>${esc(m.cis)}</code></p>`).join('') : '') : '';
    if (admin) {
      $('start').hidden = d.status !== 'draft'; $('reopen').hidden = d.status !== 'completed';
      $('export').hidden = d.status !== 'completed' || d.kind !== 'shipment';
      $('strict-label').hidden = d.kind !== 'shipment'; $('strict').disabled = d.status === 'completed';
      if (!dirty) {
        editVersion = d.version; editRevision = d.revision; editLines = d.lines.map(l => ({...l}));
        $('strict').checked = !!d.strict_owner;
        $('pn-fields').innerHTML = d.lines.map(l => `<label>${esc(l.gtin)}<input data-pn="${esc(l.gtin)}" value="${esc(l.pn)}" maxlength="1000" placeholder="Модель / артикул"><input type="number" min="1" max="2147483647" step="1" data-quantity="${esc(l.gtin)}" value="${l.quantity}" ${d.kind !== 'shipment' || d.status === 'completed' ? 'disabled' : ''} aria-label="Плановое количество"><small>${esc(l.pn_source || 'PN не определён')}</small></label>`).join('');
      }
    } else {
      input.disabled = d.status !== 'active';
      $('scan-form').querySelector('button').disabled = d.status !== 'active';
      $('complete').textContent = d.kind === 'shipment' ? 'Завершить сборку' : 'Завершить приёмку';
      pendingState();
    }
  }
  async function refresh() {
    if (refreshing || (admin && !key)) return;
    refreshing = true;
    try {
      if (current) {
        const id = current.id, next = await json(apiBase + '/documents/' + id);
        if (current?.id === id) {
          const signature = d => JSON.stringify([d.version,d.revision,d.status,d.checking,d.result_at,d.scans.length]);
          const changed = signature(current) !== signature(next);
          current = next; if (changed) render();
        }
      } else await list();
      $('connection').textContent = 'Сервер доступен · синхронизация каждые 2 с';
    } catch (e) { $('connection').textContent = 'Нет синхронизации: ' + e.message; }
    finally { refreshing = false; }
  }
  function enqueue(code) {
    if (!code) return;
    if (!current || current.status !== 'active') {
      input.value = code; savePending(); message('Документ недоступен для сканирования. Код не сохранён.', true); return;
    }
    queue.push(code); queueError = false; savePending(); pendingState(); flush();
  }
  async function flush() {
    if (flushing || !queue.length || !current) return;
    flushing = true; queueError = false; pendingState();
    try {
      while (queue.length) {
        const data = await json('/api/warehouse/documents/' + current.id + '/scans', {method:'POST', body:{code:queue[0]}});
        queue.shift(); savePending(); pendingState();
        if (data.duplicate) {
          $('duplicate-info').textContent = `GTIN: ${data.scan.gtin}. Ранее отсканирован: ${stamp(data.scan.scanned_at)}`;
          if (!$('duplicate').open) $('duplicate').showModal();
          message('Повторный КМ не добавлен. Исходный КМ сохранён.');
        } else message('КМ сохранён на сервере');
      }
      await refresh();
    } catch (e) {
      message(e.message + '\nКод остался в очереди. Нажмите «Повторить отправку» или удалите неподтверждённый код.', true);
      queueError = true;
    } finally {
      flushing = false;
      pendingState();
      focus?.focus(); maybeReload();
    }
  }
  document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => { $('duplicate').close(); focus?.focus(); maybeReload(); });
  $('document-list').onclick = e => { const b = e.target.closest('[data-doc]'); if (b) action(() => open(b.dataset.doc)); };
  document.querySelectorAll('[data-kind]').forEach(b => b.onclick = () => action(async () => {
    if (queue.length || flushing || input?.value || dirty || busy > 1) { message('Сохраните текущий ввод перед переходом.', true); return; }
    kind = b.dataset.kind; current = null; $('document').hidden = true; $('document-list').hidden = false;
    if ($('import-box')) $('import-box').hidden = false;
    history.replaceState(null, '', location.pathname + '?kind=' + kind); await list();
  }));
  $('back').onclick = () => document.querySelector(`[data-kind="${kind}"]`).click();
  $('list-refresh').onclick = () => action(refresh);
  if (!admin) {
    input.addEventListener('input', () => { savePending(); pendingState(); maybeReload(); });
    $('scan-form').onsubmit = e => { e.preventDefault(); const code = input.value; input.value = ''; enqueue(code); };
    $('check').onclick = () => action(async () => {
      if (queue.length || input.value) throw new Error('Сначала сохраните все КМ и завершите ввод.');
      $('check').disabled = true; $('check-state').textContent = 'Проверка выполняется…';
      await json('/api/warehouse/documents/' + current.id + '/check', {method:'POST'}); await refresh();
    });
    $('complete').onclick = () => action(async () => {
      if (queue.length || input.value) throw new Error('Сначала сохраните все КМ.');
      await json('/api/warehouse/documents/' + current.id + '/complete', {method:'POST'}); await refresh();
    });
    $('scans').onclick = e => { const b = e.target.closest('[data-delete]'); if (b && confirm('Удалить ранее добавленный КМ из документа?')) action(async () => { await json('/api/warehouse/documents/' + current.id + '/scans/' + b.dataset.delete, {method:'DELETE'}); await refresh(); }); };
    window.addEventListener('beforeunload', e => { savePending(); if (queue.length || input.value) { e.preventDefault(); e.returnValue=''; } });
  } else {
    $('login').onsubmit = e => { e.preventDefault(); action(async () => {
      key = $('admin-key').value; const config = await json(apiBase + '/settings'); $('admin-key').value = '';
      $('login').hidden = true; $('workspace').hidden = false;
      $('import-org').innerHTML = '<option value="">Из файла</option>' + config.organizations.map(o => `<option value="${esc(o.inn)}">${esc(o.name)} · ${esc(o.inn)}</option>`).join('');
      if (kind === 'receipt') $('import-org').value = config.receipt_default_org;
      await list(); const id = new URLSearchParams(location.search).get('doc'); if (id) await open(id);
    }); };
    $('logout').onclick = () => { key=''; location.replace('/admin/warehouse'); };
    $('template').onclick = () => action(() => download(apiBase + '/templates/' + kind, 'template.xlsx'));
    $('import-form').onsubmit = e => { e.preventDefault(); action(async () => {
      if (!$('import-file').files[0]) return;
      const form = new FormData(); form.set('kind', kind); form.set('org_inn', $('import-org').value); form.set('file', $('import-file').files[0]);
      const d = await json(apiBase + '/documents', {method:'POST',body:form});
      await open(d.id); message((d.import_warnings || []).join('\n') || 'Документ загружен');
    }); };
    $('start').onclick = () => action(async () => { await json(apiBase + '/documents/' + current.id + '/start', {method:'POST'}); await refresh(); });
    $('reopen').onclick = () => { if (confirm('Возобновить документ? Итоговый состав станет доступен для изменений и потребуется повторная проверка.')) action(async () => { await json(apiBase + '/documents/' + current.id + '/reopen', {method:'POST'}); await refresh(); }); };
    $('export').onclick = () => action(() => download(apiBase + '/documents/' + current.id + '/export', 'shipment.xlsx'));
    $('cancel-edit').onclick = () => { dirty = false; render(); message('Несохранённые изменения отменены'); maybeReload(); };
    $('edit-form').addEventListener('input', () => { dirty = true; });
    $('edit-form').onsubmit = e => { e.preventDefault(); action(async () => {
      const pns = {}, quantities = {};
      document.querySelectorAll('[data-pn]').forEach(el => { if (editLines.find(l => l.gtin === el.dataset.pn)?.pn !== el.value) pns[el.dataset.pn] = el.value; });
      document.querySelectorAll('[data-quantity]').forEach(el => { if (!el.disabled && editLines.find(l => l.gtin === el.dataset.quantity)?.quantity !== Number(el.value)) quantities[el.dataset.quantity] = Number(el.value); });
      const body = {version:editVersion,revision:editRevision,pns,quantities};
      if (current.kind === 'shipment' && current.status !== 'completed' && $('strict').checked !== !!current.strict_owner) body.strict_owner = $('strict').checked;
      await json(apiBase + '/documents/' + current.id, {method:'PATCH',body}); dirty = false; await refresh(); render(); message('Изменения сохранены');
    }); };
    window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue=''; } });
  }
  setInterval(refresh, 2000);
  setInterval(async () => {
    try { const s = await (await fetch('/api/status', {cache:'no-store'})).json(); if (s.build_id !== buildId) { pendingReload = true; maybeReload(); } } catch (_) { /* refresh shows connection state */ }
  }, 10000);
  window.addEventListener('online', () => { refresh(); if (queue.length) flush(); });
  if (!admin) action(async () => { await list(); const id = new URLSearchParams(location.search).get('doc'); if (id) await open(id); });
})();
