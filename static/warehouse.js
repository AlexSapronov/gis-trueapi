(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const admin = document.body.dataset.admin === 'true';
  GisTheme.bind($('theme-btn'));
  let authenticated = false;
  let section = admin && (new URLSearchParams(location.search).get('section') === 'token' || !new URLSearchParams(location.search).has('kind')) ? 'token' : 'documents';
  if (admin && new URLSearchParams(location.search).get('section') === 'settings') section = 'settings';
  let returnRequestId = null;
  let listSerial = 0, loadingFilters = false, filterTimer, reportSnapshot = null, selectedIssue = null, eventsBefore = null;
  let tokenOrganizations = [], defaultReceiptOrg = '', deletion = null;
  let kind = new URLSearchParams(location.search).get('kind') === 'receipt' ? 'receipt' : 'shipment';
  let current = null, busy = 0, flushing = false, queue = [], dirty = false, refreshPromise = null, queueError = false;
  let duplicateTarget = null, duplicateDeleting = false;
  let completionTarget = null, completionBusy = false;
  let workerResultSeen = null;
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
    if (pendingReload && !busy && !flushing && !queue.length && !input?.value && !dirty && !caseDraft() && !tokenDraft() && !settingsEditor?.isDirty() && !document.querySelector('dialog[open]')) location.reload();
  }
  async function request(path, options = {}) {
    const headers = {...options.headers};
    if (admin) headers['X-Admin-Request'] = '1';
    if (options.body && !(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, {...options, headers, cache:'no-store'});
    if (!response.ok) {
      let d; try { d = await response.json(); } catch (_) { d = {}; }
      if (admin && response.status === 403 && path !== '/api/admin/session') requireLogin();
      const error = new Error(d.message || (typeof d.detail === 'string' ? d.detail : `Ошибка ${response.status}`));
      error.status = response.status; throw error;
    }
    return response;
  }
  async function json(path, options) { return (await request(path, options)).json(); }
  async function action(fn) {
    const button=document.activeElement?.closest('button');
    if(button?.dataset.busy==='true')return;
    if(button){button.dataset.busy='true';button.setAttribute('aria-busy','true');}
    busy++; $('busy-state').hidden=false;
    try { return await fn(); } catch (e) { message(e.message || 'Ошибка соединения', true); }
    finally {
      if(button){delete button.dataset.busy;button.removeAttribute('aria-busy');}
      busy--; $('busy-state').hidden=!busy; maybeReload();
    }
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
  const settingsEditor = admin ? AdminSettings.create({json, action, message, onSaved:config=>{defaultReceiptOrg=config.runtime.values.receipt_default_org;}}) : null;
  const focus = input ? ScannerFocus.create({input, active:() => !!current && current.status === 'active' && !$('duplicate').open,
    submit:code => enqueue(code), resume:() => refresh()}) : null;

  function caseDraft() { return admin && !!$('issue-resolution').value; }
  function filterValues() {
    const values = {kind, scan_state:$('filter-scans').value};
    if ($('filter-org').value) values.org_inn = $('filter-org').value;
    if (admin) {
      [['query','filter-query'],['status','filter-status'],['date_from','filter-from'],['date_to','filter-to']].forEach(([field,id]) => { if ($(id).value) values[field] = $(id).value; });
      values.issue_state = $('filter-issues').value; values.trash = $('filter-trash').value;
    }
    return values;
  }
  function filterKey() { return 'warehouse-filters-' + (admin ? 'admin-' : 'worker-') + kind; }
  function saveFilters() { try { sessionStorage.setItem(filterKey(), JSON.stringify(filterValues())); } catch (_) { /* optional preferences */ } }
  function loadFilters() {
    loadingFilters=true; $('document-filters').reset();
    let saved; try { saved = JSON.parse(sessionStorage.getItem(filterKey()) || '{}'); } catch (_) { saved = {}; }
    const ids = {org_inn:'filter-org',scan_state:'filter-scans',query:'filter-query',status:'filter-status',date_from:'filter-from',date_to:'filter-to',issue_state:'filter-issues',trash:'filter-trash'};
    Object.entries(ids).forEach(([field,id]) => { if ($(id) && saved[field] != null) $(id).value = saved[field]; });
    if (!$('filter-scans').value) $('filter-scans').value = 'all';
    loadingFilters=false;
  }
  function setupFilterOrgs(orgs) {
    $('filter-org').innerHTML = '<option value="">Все организации</option>' + orgs.map(o => `<option value="${esc(o.inn)}">${esc(o.name)} · ${esc(o.inn)}</option>`).join('');
  }
  function renderMissingCodes(missing = []) {
    if (!missing.length) return '';
    const threshold = current?.runtime_settings?.values.receipt_missing_codes_threshold ?? 20;
    return `<h3 class="wh-stale">Ещё не отсканированы КМ: ${missing.length}</h3>` + (missing.length < threshold
      ? missing.map(m => `<p class="wh-code wh-stale"><code>${esc(m.cis)}</code></p>`).join('')
      : '<p class="wh-muted">Список скрыт: количество отсутствующих КМ достигло порога показа, заданного в настройках.</p>');
  }
  function renderScans() {
    if (!current) return;
    const details = new Map((current.result?.codes || []).map(c => [c.scan_id,c]));
    const query = $('scan-query').value.trim().toLowerCase(), errorsOnly = $('scan-errors-only').checked;
    const scans = current.scans.filter(s => (!query || (s.cis + ' ' + s.raw_code + ' ' + s.gtin).toLowerCase().includes(query)) && (!errorsOnly || details.get(s.id)?.errors.length));
    $('scan-total').textContent = '(' + current.scans.length + ')';
    $('scan-found').textContent = `Показано ${scans.length} из ${current.scans.length}`;
    $('scans').innerHTML = scans.map(s => { const r = details.get(s.id); return `<div class="wh-code"><b>${esc(s.gtin)}</b> · ${esc(stamp(s.scanned_at))}<br><code>${esc(s.cis)}</code>${r ? `<p>${esc(r.status || '')} ${esc(r.owner_name || r.owner_inn || '')} ${r.quantity == null ? '' : ' · ' + r.quantity + ' шт.'}</p><p class="wh-bad">${esc(r.errors.join('; '))}</p>` : '<p>КМ не проверен</p>'}${!admin && current.status === 'active' && current.runtime_settings?.values.worker_delete_scans !== false ? `<button class="btn" data-delete="${esc(s.id)}">Удалить КМ из документа</button>` : ''}</div>`; }).join('') || '<p>Подходящих КМ нет</p>';
  }
  async function loadIssues() {
    if (!current) return;
    const id = current.id, data = await json(apiBase + '/documents/' + id + '/issues');
    if (current?.id !== id) return;
    $('issues-panel').hidden = false;
    $('issues-list').innerHTML = data.issues.map(i => `<button class="btn wh-issue-choice" data-issue="${esc(i.id)}">${i.resolved_at ? 'Разобрано' : 'Проблема'} · ${esc(stamp(i.reported_at))} · ${esc(i.message || 'Без комментария')}</button>`).join('') || '<p>Обращений нет</p>';
  }
  async function loadIssueEvents(append=false) {
    if (!current || !selectedIssue) return;
    const id=current.id, issue=selectedIssue.id;
    const data=await json(apiBase + '/documents/' + id + '/issues/' + issue + '/events' + (append && eventsBefore ? '?before=' + eventsBefore : ''));
    if (current?.id !== id || selectedIssue?.id !== issue) return;
    const labels={policy_changed:'Правила отгрузки изменены',created:'Документ создан',started:'Операция запущена',reopened:'Возобновлена',scan_added:'КМ добавлен',scan_removed:'КМ удалён',edited:'Реквизиты изменены',check_started:'Проверка запущена',checked:'Проверка завершена',completed:'Документ завершён',deleted:'Документ удалён',restored:'Документ восстановлен',issue_reported:'Обращение отправлено',issue_resolved:'Обращение разобрано',returned_to_warehouse:'Возвращён на склад',warehouse_return_acknowledged:'Склад прочитал ответ'};
    const html=data.events.map(e => `<details class="wh-code"><summary>${esc(stamp(e.created_at))} · ${e.actor === 'admin' ? 'Администратор' : 'Склад'} · ${esc(labels[e.action] || e.action)}</summary><pre>${esc(JSON.stringify(e.details,null,2))}</pre></details>`).join('');
    if (append) $('issue-events').insertAdjacentHTML('beforeend',html); else $('issue-events').innerHTML=html;
    eventsBefore=data.next_before; $('events-more').hidden=!eventsBefore; $('events-title').hidden=false;
  }
  async function openIssue(issueId, passive=false) {
    if(caseDraft() && passive)return;
    if (caseDraft()) throw new Error('Сначала завершите разбор текущего обращения или очистите комментарий.');
    const id=current.id, issue=await json(apiBase + '/documents/' + id + '/issues/' + issueId);
    if (current?.id !== id || (passive && caseDraft())) return;
    selectedIssue=issue; returnRequestId=null; eventsBefore=null;
    const snap=issue.snapshot, result=snap.result, rawCodes=new Map(snap.scans.map(s=>[s.id,s.raw_code]));
    const rows=result.rows.filter(r => r.remaining !== 0).map(r => `<p>${esc(r.gtin)}: требуется ${r.required}, проверено ${r.actual}, осталось ${r.remaining}</p>`).join('');
    const codes=result.codes.map(c => `<details class="wh-code"><summary>${c.errors.length ? 'Ошибка · ' : ''}${esc(c.cis)}</summary><p>${esc(c.owner_name || c.owner_inn || '')} ${esc(c.status || '')} · ${c.quantity ?? 'Количество неизвестно'}</p><p class="wh-bad">${esc(c.errors.join('; '))}</p><pre>${esc(rawCodes.get(c.scan_id) || '')}</pre></details>`).join('');
    const missing=renderMissingCodes(result.missing);
    $('issue-detail').innerHTML=`<h4>Проверка от ${esc(stamp(issue.result_at))} · версия ${issue.version}</h4><p>${esc(issue.message || 'Без комментария')}</p><p class="${issue.resolved_at ? 'wh-good' : 'wh-bad'}">${issue.resolved_at ? 'Разобрано: ' + esc(issue.resolution) : 'Требует внимания'}</p>${(issue.returns || []).map(r=>`<div class="wh-reply-history"><b>Передано на склад · ${esc(stamp(r.created_at))}</b><p>${esc(r.message)}</p><small>${r.acknowledged_at ? 'Склад ознакомился: '+esc(stamp(r.acknowledged_at)) : 'Ожидает ознакомления склада'}</small></div>`).join('')}${rows}${missing}<details open><summary>Коды в обращении: ${snap.scans.length}</summary>${codes}</details>`;
    $('resolve-form').hidden=false; $('resolve-issue').hidden=!!issue.resolved_at;
    $('return-warehouse').disabled=current.status!=='active'||!!current.deleted_at||current.runtime_settings?.values.return_to_warehouse===false;
    $('return-hint').textContent=$('return-warehouse').disabled ? 'Возврат недоступен. Проверьте, что документ в работе и функция включена в настройках.' : 'Комментарий будет виден складу. Сканирование доступно до и после возврата.';
    await loadIssueEvents();
  }
  function tokenDraft() { return admin && (!!$('token-uuid').value || !!$('token-signature').value); }
  function requireLogin() {
    authenticated = false;
    $('workspace').hidden = true; $('login').hidden = false;
    document.querySelectorAll('dialog[open]').forEach(d => d.close());
    $('connection').textContent = 'Войдите в админку'; $('connection').dataset.state='pending';
  }
  function showSection() {
    document.body.classList.toggle('wh-worker-document', !admin && !!current);
    $('document-filters').hidden = !!current || (admin && section !== 'documents');
    $('list-heading').hidden=$('document-filters').hidden;
    $('list-title').textContent=(admin && $('filter-trash').value==='true' ? 'Корзина · ' : '') + (kind==='shipment' ? 'Отгрузки' : 'Приёмки');
    document.querySelectorAll('[data-mode]').forEach(a=>{a.classList.toggle('current',a.dataset.mode===(admin?'admin':kind));if(a.classList.contains('current'))a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
    if (!admin) return;
    const token = section === 'token', settings = section === 'settings', other = token || settings;
    $('application-settings').hidden = !settings;
    $('settings-tab').classList.toggle('primary', settings);
    $('token-panel').hidden = !token;
    $('document-list').hidden = other || !!current;
    $('document').hidden = other || !current;
    $('import-box').hidden = other || !!current;
    $('list-refresh').hidden = other;
    $('token-tab').classList.toggle('primary', token);
    document.querySelectorAll('[data-kind]').forEach(b => b.classList.toggle('primary', !other && b.dataset.kind === kind));
  }
  async function tokenStatus() {
    const data = await json('/api/status');
    tokenOrganizations = data.organizations;
    const org = tokenOrganizations.find(o => o.inn === $('token-org').value);
    $('token-meta').textContent = org ? (org.token_configured ? 'Токен настроен · Обновлён: ' + stamp(org.token_updated_at) : 'Токен не настроен') : '';
  }
  async function authenticatedWorkspace() {
    const config = await json(apiBase + '/settings');
    authenticated = true; $('admin-key').value = '';
    $('login').hidden = true; $('workspace').hidden = false;
    defaultReceiptOrg = config.receipt_default_org;
    const options = config.organizations.map(o => `<option value="${esc(o.inn)}">${esc(o.name)} · ${esc(o.inn)}</option>`).join('');
    setupFilterOrgs(config.organizations); loadFilters();
    $('import-org').innerHTML = '<option value="">Из файла</option>' + options;
    $('token-org').innerHTML = options;
    if (kind === 'receipt') $('import-org').value = defaultReceiptOrg;
    if (section === 'documents') {
      const id = new URLSearchParams(location.search).get('doc');
      if (id && !current) await open(id); else if (current) render(); else await list();
    } else if (section === 'settings') await settingsEditor.load();
    else await tokenStatus();
    showSection();
  }
  async function list() {
    const serial=++listSerial;
    const data = await json(apiBase + '/documents?' + new URLSearchParams(filterValues()));
    if (serial !== listSerial || (admin && section !== 'documents')) return;
    $('filter-count').textContent = 'Документов: ' + data.documents.length;
    $('list-title').textContent=(admin && $('filter-trash').value==='true'?'Корзина · ':'')+(kind==='shipment'?'Отгрузки':'Приёмки');
    document.querySelectorAll('[data-kind]').forEach(b => b.classList.toggle('primary', b.dataset.kind === kind));
    $('document-list').innerHTML = data.documents.length ? data.documents.map(d => `<button class="wh-doc ${d.open_issues ? 'wh-problem' : ''} ${d.pending_returns ? 'wh-returned' : ''}" data-doc="${esc(d.id)}"><span class="wh-badge" data-status="${d.deleted_at ? 'deleted' : esc(d.status)}">${d.deleted_at ? 'В корзине' : esc(status(d))}</span><b>№ ${esc(d.number)}</b><span class="wh-muted">${esc(d.date)} · ${esc(d.org_name)}</span>${d.recipient_name ? `<span>→ ${esc(d.recipient_name)}</span>` : ''}${d.pending_returns ? `<span class="wh-good">${admin ? 'Ответ передан · ждёт ознакомления' : 'Ответственный ответил — можно продолжать'}</span>` : ''}${d.open_issues ? `<span class="wh-bad">Проблемный · обращений: ${d.open_issues}</span>` : ''}<span>${d.scan_count} КМ · ${d.scan_count ? 'Есть сканы' : 'Ещё не начато'}</span>${d.kind === 'shipment' ? `<span class="wh-muted">${d.strict_owner ? 'Строгое соответствие организации' : 'КМ наших организаций'}</span>` : ''}<span class="wh-muted">Изменён: ${esc(stamp(d.updated_at))}</span></button>`).join('') : '<div class="wh-card"><h3>Документы не найдены</h3><p class="wh-muted">Измените условия поиска или сбросьте фильтры.</p></div>';

  }
  async function returnToDocuments(text) {
    if (!admin && current && (queue.length || input.value)) {
      savePending(); queue = []; input.value = '';
      text += ' Неподтверждённые сканы сохранены в браузере для этого документа.';
    }
    current = null; dirty = false; section = 'documents';
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
    $('document').hidden = true; $('document-list').hidden = false;
    history.replaceState(null, '', location.pathname + '?kind=' + kind);
    showSection(); await list(); message(text);
  }
  async function open(id) {
    if (queue.length || flushing || input?.value) { message('Сначала сохраните ожидающие КМ и завершите ввод.', true); return; }
    const d = await json(apiBase + '/documents/' + id);
    if (!admin && d.status === 'completed') { await returnToDocuments('Документ уже завершён. Выберите следующий документ.'); return; }
    current = d; dirty = false; queue = []; section = 'documents'; selectedIssue=null; eventsBefore=null;
    if (!admin) { workerResultSeen = null; $('composition-details').open = false; $('document-meta').open = false; $('scan-details').open = false; }
    $('scan-query').value=''; $('scan-errors-only').checked=false;
    if (admin) { $('issues-panel').hidden=true; $('events-title').hidden=true; $('events-more').hidden=true; $('issue-detail').innerHTML=''; $('issue-events').innerHTML=''; $('issue-resolution').value=''; $('resolve-form').hidden=true; }
    $('document').hidden = false; $('document-list').hidden = true;
    if ($('import-box')) $('import-box').hidden = true;
    history.replaceState(null, '', location.pathname + '?kind=' + kind + '&doc=' + encodeURIComponent(id));
    if (!admin) {
      try { const cached = JSON.parse(sessionStorage.getItem(pendingKey()) || '{}'); queue = cached.queue || []; input.value = cached.partial || ''; }
      catch (_) { queue = []; }
    }
    render(); showSection(); focus?.focus();
    if (queue.length) flush();
  }
  function render() {
    if (!current) return;
    const d = current, result = d.result;
    const incompleteReceipt = d.kind === 'receipt' && result?.missing.length > 0 && !result.codes.some(c => c.errors.length);
    $('doc-badge').textContent=d.deleted_at?'В корзине':status(d); $('doc-badge').dataset.status=d.deleted_at?'deleted':d.status;
    $('doc-title').textContent = (d.kind === 'shipment' ? 'Отгрузка' : 'Приёмка') + ' № ' + d.number + (admin ? ' от ' + d.date : '');
    $('doc-parties').textContent = `${d.org_name} · ИНН ${d.org_inn}` + (d.recipient_name ? ` → ${d.recipient_name} · ИНН ${d.recipient_inn}` : '');
    $('doc-status').textContent = `${d.deleted_at ? 'В корзине · ' : ''}${status(d)} · Изменён ${stamp(d.updated_at)}` + (d.kind === 'shipment' ? ` · ${d.strict_owner ? 'Строгое соответствие юридическому лицу' : 'Допустимы КМ наших организаций'}` : '');
    $('doc-count').textContent = admin ? `Накоплено: ${d.scans.length} уникальных КМ` + (d.kind === 'receipt' ? ` · Ожидается: ${d.expected.length} · Проверено: ${result?.checked_count ?? 0} · Подтверждено: ${result?.accepted_count ?? 0}/${d.expected.length}` : '') : `Считано: ${d.scans.length} КМ`;
    let state = d.checking ? 'Проверка выполняется…' : !result ? 'КМ накапливаются. Проверка не выполнялась.' : !d.check_current ? 'Данные предыдущей проверки. Состав или правила изменены, требуется повторная проверка.' : result.ok ? 'Проверено. Документ полностью совпадает.' : incompleteReceipt ? 'Приёмка ещё не заполнена. Продолжайте сканирование.' : 'Проверено. Есть расхождения.';
    if (!admin) {
      state = d.checking ? 'Проверяем КМ…' : !result ? '' : !d.check_current ? 'Есть изменения. Проверьте ещё раз.' : result.ok ? 'Всё совпадает.' : incompleteReceipt ? 'Продолжайте сканирование.' : 'Есть ошибки в КМ. Проверьте результат ниже.';
      $('worker-org').textContent = d.org_name;
      $('worker-plan').textContent = d.kind === 'receipt' ? `По документу: ${d.expected.length} КМ` : `Товаров: ${d.lines.length}`;
      $('product-count').textContent = `(${d.lines.length})`;
      $('doc-status').textContent = `Дата: ${d.date} · ${$('doc-status').textContent}`;
      $('complete').hidden = !d.can_complete;
      $('check-state').hidden = !state;
    }
    $('check-state').textContent = state;
    $('check-state').className = 'wh-check-state ' + (!d.check_current || d.checking || incompleteReceipt ? 'wh-stale' : result?.ok ? 'wh-good' : 'wh-bad');
    $('warehouse-replies').innerHTML=(d.warehouse_replies || []).map(r=>`<section class="wh-reply"><span class="wh-eyebrow">Ответственный ответил · ${esc(stamp(r.created_at))}</span><h3>${admin?'Ответ передан на склад':'Можно продолжать работу'}</h3><p>${esc(r.message)}</p>${admin?'<small>Ожидает ознакомления склада</small>':`<button class="btn primary" data-ack-return="${esc(r.id)}">${d.status==='active'?'Продолжить работу':'Ответ прочитан'}</button>`}</section>`).join('');
    $('issue-state').textContent = d.open_issues ? '⚠ Сообщено ответственному · открытых обращений: ' + d.open_issues : (d.issue_count ? 'Обращения разобраны' : '');
    $('actual-label').textContent = d.kind === 'shipment' ? 'Собрано' : 'Принято';
    const actual = new Map((result?.rows || []).map(r => [r.gtin,r]));
    $('lines').innerHTML = d.lines.map(l => { const r = actual.get(l.gtin); return `<tr><td>${esc(l.pn || 'Не определён')}</td><td><button class="wh-link" data-product="${esc(l.gtin)}">${esc(l.gtin)}</button></td><td>${l.quantity}</td><td>${r ? r.actual : 'Не проверено'}</td><td class="${r?.remaining ? (d.kind === 'receipt' && r.remaining > 0 ? 'wh-stale' : 'wh-bad') : ''}">${r ? r.remaining : 'Не проверено'}</td></tr>`; }).join('');
    renderScans();
    $('check-errors').innerHTML = result ? (result.codes.some(c => c.errors.length) ? `<p class="wh-bad">Ошибочных КМ: ${result.codes.filter(c => c.errors.length).length}. Причины и владелец указаны в общем списке КМ ниже.</p>` : '') + (result.warnings || []).map(w => `<p class="wh-stale">${esc(w)}</p>`).join('') + renderMissingCodes(result.missing) : '';
    if (!admin) {
      const errors = result?.codes.filter(c => c.errors.length).length || 0;
      const missing = result?.missing.length || 0;
      $('worker-result').hidden = !result;
      $('worker-result-label').textContent = !d.check_current ? 'Предыдущая проверка' : errors ? `Ошибочные КМ: ${errors}` : missing ? `Ещё не считано: ${missing} КМ` : 'Результат проверки';
      const resultKey = d.id + ':' + d.result_at;
      if (workerResultSeen !== resultKey) { $('worker-result').open = !!errors; workerResultSeen = resultKey; }
      if (errors) $('check-errors').insertAdjacentHTML('afterbegin', `<button class="btn wh-danger" data-show-errors>Показать ошибочные КМ (${errors})</button>`);
    }
    if (admin) {
      $('start').hidden = d.status !== 'draft'; $('reopen').hidden = d.status !== 'completed';
      $('export').hidden = d.status !== 'completed' || d.kind !== 'shipment';
      $('restore-document').hidden=!d.deleted_at; $('delete-document').hidden=!!d.deleted_at;
      $('show-issues').hidden=!d.issue_count;
      if (d.deleted_at) { $('start').hidden=true; $('reopen').hidden=true; $('export').hidden=true; }
      $('save').disabled=!!d.deleted_at; $('strict-label').hidden = d.kind !== 'shipment'; $('strict').disabled = !!d.deleted_at || d.status === 'completed';
      if (!dirty) {
        editVersion = d.version; editRevision = d.revision; editLines = d.lines.map(l => ({...l}));
        $('strict').checked = !!d.strict_owner;
        const sourceName={excel:'Из Excel',manual:'Введён вручную',trueapi:'Карточка товара'};
        $('pn-fields').innerHTML = d.lines.map(l => `<section class="wh-edit-line"><strong>${esc(l.gtin)}</strong><label>PN / артикул<input data-pn="${esc(l.gtin)}" value="${esc(l.pn)}" maxlength="1000" ${d.deleted_at ? 'disabled' : ''} placeholder="Модель / артикул"></label><label>Плановое количество, шт.<input type="number" min="1" max="2147483647" step="1" data-quantity="${esc(l.gtin)}" value="${l.quantity}" ${d.deleted_at || d.kind !== 'shipment' || d.status === 'completed' ? 'disabled' : ''}></label><small>${esc(sourceName[l.pn_source] || 'PN не определён')}</small></section>`).join('');

      }
    } else {
      input.disabled = d.status !== 'active';
      $('scan-form').querySelector('button').disabled = d.status !== 'active';
      $('complete').textContent = d.kind === 'shipment' ? 'Завершить сборку' : 'Завершить приёмку';
      const features = d.runtime_settings?.values || {};
      $('manual-input').hidden=features.manual_input===false;
      if(features.manual_input===false){input.inputMode='none';$('manual-input').textContent='Ручной ввод';$('manual-input').setAttribute('aria-pressed','false');}
      $('report-message').required=!!features.report_comment_required;
      $('report-message').placeholder=features.report_comment_required ? 'Опишите расхождение' : 'Что нужно уточнить? Необязательно';
      $('report-issue').hidden=!(features.report_issues!==false && d.status==='active' && d.check_current && d.result && !d.result.ok && !d.checking);
      $('report-issue').disabled=!!d.reported_current_result; $('report-issue').textContent=d.reported_current_result ? 'Уже сообщено' : 'Сообщить ответственному';
      updateDuplicateActions();
      updateCompletionPrompt();
      pendingState();
    }
  }
  function refresh(force = false) {
    if (refreshPromise) return force ? refreshPromise.then(() => refresh()) : refreshPromise;
    if (admin && !authenticated) return Promise.resolve();
    refreshPromise = refreshDocument().finally(() => { refreshPromise = null; });
    return refreshPromise;
  }
  async function refreshDocument() {
    try {
      if (admin && section === 'settings') return;
      if (admin && section === 'token') await tokenStatus();
      else if (current) {
        const id = current.id, next = await json(apiBase + '/documents/' + id);
        if (current?.id === id) {
          const signature = d => JSON.stringify([d.version,d.revision,d.status,d.checking,d.result_at,d.scans.length,d.deleted_at,d.open_issues,d.issue_count,d.pending_returns,d.runtime_settings?.revision]);
          const changed = signature(current) !== signature(next);
          current = next;
          if (!admin && next.status === 'completed') {
            await returnToDocuments('Документ завершён. Выберите следующий документ.');
          } else if (changed) {render(); if(admin&&selectedIssue&&!$('issues-panel').hidden)await openIssue(selectedIssue.id,true);}
        }
      } else await list();
      $('connection').textContent = 'Подключено'; $('connection').dataset.state='online';
    } catch (e) {
      if (e.status === 404 && current) {
        if (!admin) { input.disabled = true; $('scan-form').querySelector('button').disabled = true; $('check').disabled = true; $('complete').disabled = true; current.status = 'deleted'; $('report-issue').hidden=true; }
        else { current = null; dirty = false; history.replaceState(null, '', '/admin?kind=' + kind); showSection(); await list(); }
        if (!admin) $('doc-status').textContent = 'Удалён администратором';
        message('Документ удалён администратором. Дальнейшая работа по нему недоступна.', true);
        $('connection').textContent = 'Документ удалён'; $('connection').dataset.state='pending'; return;
      }
      if(e.status===403 && admin){$('connection').textContent='Сессия завершена — войдите снова';$('connection').dataset.state='pending';}
      else {$('connection').textContent='Синхронизация недоступна: '+e.message;$('connection').dataset.state='offline';}
    }
  }
  function enqueue(code) {
    if (!code) return;
    if (!current || current.status !== 'active') {
      input.value = code; savePending(); message('Документ недоступен для сканирования. Код не сохранён.', true); return;
    }
    queue.push(code); queueError = false; savePending(); pendingState(); flush();
  }
  async function flush() {
    if (flushing || !queue.length || !current || $('duplicate').open) return;
    const documentId = current.id, pendingQueue = queue;
    flushing = true; queueError = false; pendingState();
    try {
      while (queue.length && current?.id === documentId && queue === pendingQueue) {
        const data = await json('/api/warehouse/documents/' + documentId + '/scans', {method:'POST', body:{code:queue[0]}});
        if (current?.id !== documentId || queue !== pendingQueue) break;
        queue.shift(); savePending(); pendingState();
        if (data.duplicate) {
          duplicateTarget = {documentId:current.id, scanId:data.scan.id};
          $('duplicate-info').textContent = `КМ: ${data.scan.cis}. Ранее отсканирован: ${stamp(data.scan.scanned_at)}`;
          $('duplicate-error').textContent = '';
          updateDuplicateActions();
          $('duplicate').showModal();
          message('Повторный КМ не добавлен. Исходный КМ сохранён.');
          break;
        } else message('КМ сохранён на сервере');
      }
      await refresh();
    } catch (e) {
      if (current?.id === documentId && queue === pendingQueue) {
        message(e.message + '\nКод остался в очереди. Нажмите «Повторить отправку» или удалите неподтверждённый код.', true);
        queueError = true;
      }
    } finally {
      flushing = false;
      pendingState();
      focus?.focus(); maybeReload();
      if (queue.length && !queueError && !$('duplicate').open) flush();
    }
  }
  function updateDuplicateActions() {
    $('duplicate-keep').disabled = duplicateDeleting;
    $('duplicate-remove').hidden = current?.runtime_settings?.values.worker_delete_scans === false;
    $('duplicate-remove').disabled = duplicateDeleting || !duplicateTarget || current?.id !== duplicateTarget.documentId || current?.status !== 'active' || !!current?.deleted_at;
  }
  $('duplicate-keep').onclick = () => {
    if (duplicateDeleting) return;
    message('КМ оставлен в документе. Продолжайте сканирование.');
    $('duplicate').close();
  };
  $('duplicate-remove').onclick = () => action(async () => {
    const target = duplicateTarget;
    if (!target || duplicateDeleting) return;
    duplicateDeleting = true; updateDuplicateActions();
    try {
      const updated = await json('/api/warehouse/documents/' + target.documentId + '/scans/' + target.scanId, {method:'DELETE'});
      if (current?.id === target.documentId) { current = updated; render(); }
      message('КМ удалён из документа. Продолжайте сканирование.');
      $('duplicate').close();
    } catch (e) { $('duplicate-error').textContent = e.message || 'Не удалось удалить КМ. Повторите попытку.'; }
    finally { duplicateDeleting = false; updateDuplicateActions(); focus?.focus(); }
  });
  $('duplicate').addEventListener('cancel', e => { if (duplicateDeleting) e.preventDefault(); });
  $('duplicate').addEventListener('close', () => {
    duplicateTarget = null; updateDuplicateActions(); pendingState(); flush(); focus?.focus(); maybeReload();
  });
  function completionCurrent() {
    return completionTarget && current?.id === completionTarget.id && current.can_complete &&
      current.version === completionTarget.version && current.result_at === completionTarget.resultAt &&
      !queue.length && !input.value && !flushing;
  }
  function updateCompletionPrompt() {
    if (admin) return;
    if (completionTarget && !completionBusy && !completionCurrent()) {
      $('complete-dialog').close(); completionTarget = null;
      message('Документ изменился. Перед завершением выполните актуальную проверку.', true);
    }
    $('complete-confirm').disabled = completionBusy || !completionCurrent();
    $('complete-later').disabled = completionBusy;
  }
  function promptCompletion() {
    if (!current?.can_complete || queue.length || input.value || flushing) return;
    completionTarget = {id:current.id, version:current.version, resultAt:current.result_at};
    const operation = current.kind === 'receipt' ? 'приёмку' : 'сборку';
    $('complete-description').textContent = `Все КМ проверены, ошибок нет, количество совпадает. Завершить ${operation}?`;
    $('complete-confirm').textContent = `Завершить ${operation}`;
    $('complete-error').textContent = '';
    updateCompletionPrompt(); $('complete-dialog').showModal();
  }
  $('document-list').onclick = e => { const b = e.target.closest('[data-doc]'); if (b) action(() => open(b.dataset.doc)); };
  document.querySelectorAll('[data-kind]').forEach(b => b.onclick = () => action(async () => {
    if (queue.length || flushing || input?.value || dirty || caseDraft() || tokenDraft() || settingsEditor?.isDirty() || busy > 1) { message('Сохраните текущий ввод перед переходом.', true); return; }
    kind = b.dataset.kind; loadFilters(); section = 'documents'; current = null; $('document').hidden = true; $('document-list').hidden = false;
    if ($('import-box')) { $('import-box').hidden = false; $('import-org').value = kind === 'receipt' ? defaultReceiptOrg : ''; }
    showSection();
    history.replaceState(null, '', location.pathname + '?kind=' + kind); await list();
  }));
  $('document-filters').onsubmit=e=>e.preventDefault();
  $('document-filters').addEventListener('change',()=>{ saveFilters(); action(list); });
  $('document-filters').addEventListener('input',e=>{ if(e.target.type!=='search')return; clearTimeout(filterTimer); filterTimer=setTimeout(()=>{saveFilters();action(list);},300); });
  $('document-filters').addEventListener('reset',()=>{ if(loadingFilters)return; setTimeout(()=>{saveFilters();action(list);},0); });
  $('scan-query').oninput=renderScans; $('scan-errors-only').onchange=renderScans;
  $('check-errors').onclick = e => { if (e.target.closest('[data-show-errors]')) { $('scan-query').value = ''; $('scan-errors-only').checked = true; $('scan-details').open = true; renderScans(); $('scan-details').scrollIntoView({block:'nearest'}); } };
  $('lines').onclick=e=>{const b=e.target.closest('[data-product]');if(b){$('scan-query').value=b.dataset.product;$('scan-details').open=true;renderScans();}};
  $('back').onclick = () => document.querySelector(`[data-kind="${kind}"]`).click();
  $('list-refresh').onclick = () => action(refresh);
  if (!admin) {
    $('warehouse-replies').onclick=e=>{const b=e.target.closest('[data-ack-return]');if(b)action(async()=>{
      await json('/api/warehouse/documents/'+current.id+'/returns/'+b.dataset.ackReturn+'/acknowledge',{method:'POST'});
      await refresh(); message('Ответ прочитан. Можно продолжать работу.');focus?.focus();
    });};
    $('manual-input').onclick=()=>{
      const manual=input.inputMode!=='text'; input.inputMode=manual?'text':'none';
      $('manual-input').textContent=manual?'Режим сканера':'Ручной ввод';$('manual-input').setAttribute('aria-pressed',String(manual));input.focus();
    };
    $('report-issue').onclick=()=>{reportSnapshot={id:current.id,version:current.version,result_at:current.result_at};$('report-message').value='';$('report-dialog').showModal();};
    $('report-cancel').onclick=()=>{$('report-dialog').close();reportSnapshot=null;focus?.focus();};
    $('report-form').onsubmit=e=>{e.preventDefault();action(async()=>{
      const report=reportSnapshot;if(!report)return;
      const result=await json('/api/warehouse/documents/'+report.id+'/issues',{method:'POST',body:{version:report.version,result_at:report.result_at,message:$('report-message').value}});
      $('report-dialog').close();reportSnapshot=null;await refresh();message(result.duplicate?'Обращение уже отправлено':'Сообщено ответственному');focus?.focus();
    });};
    input.addEventListener('input', () => { savePending(); pendingState(); maybeReload(); });
    $('scan-form').onsubmit = e => { e.preventDefault(); const code = input.value; input.value = ''; enqueue(code); };
    $('check').onclick = () => action(async () => {
      if (queue.length || input.value) throw new Error('Сначала сохраните все КМ и завершите ввод.');
      $('check').disabled = true; $('check-state').textContent = 'Проверка выполняется…';
      await json('/api/warehouse/documents/' + current.id + '/check', {method:'POST'}); await refresh(true);
      promptCompletion();
    });
    $('complete').onclick = promptCompletion;
    $('complete-later').onclick = () => { if (!completionBusy) $('complete-dialog').close(); };
    $('complete-dialog').addEventListener('cancel', e => { if (completionBusy) e.preventDefault(); });
    $('complete-dialog').addEventListener('close', () => { completionTarget = null; focus?.focus(); maybeReload(); });
    $('complete-confirm').onclick = () => action(async () => {
      await refresh(true);
      if (!completionCurrent()) return;
      const id = completionTarget.id;
      completionBusy = true; updateCompletionPrompt();
      try {
        await json('/api/warehouse/documents/' + id + '/complete', {method:'POST'});
        await returnToDocuments('Документ завершён. Выберите следующий документ.');
      } catch (e) { $('complete-error').textContent = e.message; await refresh(); }
      finally { completionBusy = false; updateCompletionPrompt(); }
    });
    $('scans').onclick = e => { const b = e.target.closest('[data-delete]'); if (b && confirm('Удалить ранее добавленный КМ из документа?')) action(async () => { await json('/api/warehouse/documents/' + current.id + '/scans/' + b.dataset.delete, {method:'DELETE'}); await refresh(); }); };
    window.addEventListener('beforeunload', e => { savePending(); if (queue.length || input.value) { e.preventDefault(); e.returnValue=''; } });
  } else {
    $('login').onsubmit = e => { e.preventDefault(); action(async () => {
      await json('/api/admin/session', {method:'POST', body:{key:$('admin-key').value}});
      await authenticatedWorkspace();
    }); };
    $('logout').onclick = () => action(async () => {
      if ((dirty || caseDraft() || tokenDraft() || settingsEditor?.isDirty()) && !confirm('Выйти и отменить несохранённый ввод?')) return;
      await json('/api/admin/session', {method:'DELETE'}); dirty = false; $('token-form').reset(); location.replace('/admin');
    });
    $('token-tab').onclick = () => action(async () => {
      if (dirty || caseDraft() || tokenDraft() || settingsEditor?.isDirty() || busy > 1) throw new Error('Сохраните или отмените изменения документа перед переходом.');
      section = 'token'; current = null; history.replaceState(null, '', '/admin?section=token');
      showSection(); await tokenStatus();
    });
    $('settings-tab').onclick = () => action(async () => {
      if (dirty || caseDraft() || tokenDraft() || settingsEditor.isDirty() || busy > 1) throw new Error('Сохраните или отмените текущие изменения перед переходом.');
      section = 'settings'; current = null; history.replaceState(null, '', '/admin?section=settings');
      showSection(); await settingsEditor.load();
    });
    $('token-org').onchange = () => action(tokenStatus);
    $('token-form').onsubmit = e => { e.preventDefault(); action(async () => {
      $('token-submit').disabled = true; $('token-result').textContent = 'Обновление…';
      try {
        const form = new FormData(); form.set('inn', $('token-org').value); form.set('uuid', $('token-uuid').value.trim()); form.set('signature', $('token-signature').value.trim());
        const result = await json('/admin/token', {method:'POST',body:form});
        if (!result.ok) throw new Error(result.message);
        $('token-result').textContent = result.message; $('token-result').className = 'wh-good';
        $('token-uuid').value = ''; $('token-signature').value = ''; await tokenStatus();
      } catch (e) { $('token-result').textContent = e.message; $('token-result').className = 'wh-bad'; }
      finally { $('token-submit').disabled = false; }
    }); };
    $('restore-document').onclick=()=>action(async()=>{
      const d=current;await json(apiBase+'/documents/'+d.id+'/restore',{method:'POST',body:{version:d.version,revision:d.revision}});
      $('filter-trash').value='false';saveFilters();await refresh();message('Документ восстановлен из корзины');
    });
    $('show-issues').onclick=()=>action(loadIssues); $('issues-refresh').onclick=()=>action(async()=>{await loadIssues();if(selectedIssue&&!caseDraft())await openIssue(selectedIssue.id);});
    $('issues-list').onclick=e=>{const b=e.target.closest('[data-issue]');if(b)action(()=>openIssue(b.dataset.issue));};
    $('events-more').onclick=()=>action(()=>loadIssueEvents(true));
    $('resolve-form').onsubmit=e=>{e.preventDefault();action(async()=>{
      if(!selectedIssue)return;
      await json(apiBase+'/documents/'+current.id+'/issues/'+selectedIssue.id+'/resolve',{method:'POST',body:{resolution:$('issue-resolution').value}});
      $('issue-resolution').value='';await refresh();await loadIssues();await openIssue(selectedIssue.id);message('Обращение закрыто');
    });};
    $('issue-resolution').addEventListener('input',()=>{returnRequestId=null;});
    $('resolve-form').addEventListener('reset',()=>{returnRequestId=null;});
    $('return-warehouse').onclick=()=>action(async()=>{
      if(!selectedIssue||!$('resolve-form').reportValidity())return;
      returnRequestId ||= window.crypto?.randomUUID?.() || 'reply_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2);
      const issueId=selectedIssue.id;
      await json(apiBase+'/documents/'+current.id+'/issues/'+issueId+'/return',{method:'POST',body:{resolution:$('issue-resolution').value,request_id:returnRequestId}});
      $('issue-resolution').value='';returnRequestId=null;await refresh();await loadIssues();await openIssue(issueId);message('Документ возвращён на склад с комментарием');
    });
    $('delete-document').onclick = () => {
      if (dirty) { message('Сохраните или отмените изменения перед удалением.', true); return; }
      deletion = {id:current.id,number:current.number,version:current.version,revision:current.revision};
      $('delete-description').textContent = (current.kind === 'shipment' ? 'Отгрузка' : 'Приёмка') + ' № ' + current.number + ' · ' + status(current);
      $('delete-number').value = ''; $('delete-confirm').disabled = true; $('delete-dialog').showModal();
    };
    $('delete-number').oninput = () => { $('delete-confirm').disabled = $('delete-number').value !== deletion?.number; };
    $('delete-cancel').onclick = () => { $('delete-dialog').close(); deletion = null; };
    $('delete-form').onsubmit = e => { e.preventDefault(); action(async () => {
      if (!deletion || $('delete-number').value !== deletion.number) return;
      $('delete-confirm').disabled = true;
      try {
        await json(apiBase + '/documents/' + deletion.id, {method:'DELETE',body:{version:deletion.version,revision:deletion.revision}});
        $('delete-dialog').close(); deletion = null; current = null; dirty = false;
        history.replaceState(null, '', '/admin?kind=' + kind); showSection(); await list(); message('Документ удалён');
      } catch (e) { $('delete-dialog').close(); deletion = null; await refresh(); throw e; }
    }); };
    $('template').onclick = () => action(() => download(apiBase + '/templates/' + kind, 'template.xlsx'));
    $('import-form').onsubmit = e => { e.preventDefault(); action(async () => {
      if (!$('import-file').files[0]) return;
      const form = new FormData(); form.set('kind', kind); form.set('org_inn', $('import-org').value); form.set('file', $('import-file').files[0]);
      const d = await json(apiBase + '/documents', {method:'POST',body:form});
      $('import-box').open=false; $('import-file').value='';
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
    window.addEventListener('beforeunload', e => { if (dirty || caseDraft() || tokenDraft() || settingsEditor?.isDirty()) { e.preventDefault(); e.returnValue=''; } });
  }
  setInterval(refresh, 2000);
  setInterval(async () => {
    try { const s = await (await fetch('/api/status', {cache:'no-store'})).json(); if (s.build_id !== buildId) { pendingReload = true; maybeReload(); } } catch (_) { /* refresh shows connection state */ }
  }, 10000);
  window.addEventListener('online', () => { refresh(); if (queue.length) flush(); });
  if (admin) action(async () => {
    try { await json('/api/admin/session'); await authenticatedWorkspace(); }
    catch (e) { if (e.status !== 403) throw e; requireLogin(); }
  });
  if (!admin) action(async () => { setupFilterOrgs((await json('/api/status')).organizations); loadFilters(); await list(); const id = new URLSearchParams(location.search).get('doc'); if (id) await open(id); });
})();
