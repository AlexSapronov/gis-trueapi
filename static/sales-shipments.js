(() => {
  'use strict';
  const api = '/api/sales-shipments';
  const $ = id => document.getElementById(id);
  const login = $('sales-login');
  const workspace = $('sales-workspace');
  const upload = $('sales-upload');
  const notice = $('notice');
  const rows = $('sales-tasks');
  const controls = [upload.querySelector('button'), $('sales-refresh'), $('sales-logout')];
  const writeHeaders = {'X-Sales-Request': '1'};
  let linkKey = location.hash.slice(1);
  if (linkKey) history.replaceState(history.state, '', location.pathname + location.search);
  window.addEventListener('hashchange', () => {
    if (location.hash) location.reload();
  });
  GisTheme.bind($('theme-btn'));

  const stamp = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'short', timeStyle: 'short'});
  function taskStatus(task) {
    if (task.deleted_at) return 'Удалено';
    if (task.archived_at) return 'В архиве';
    if (task.status === 'completed') return 'Сборка завершена';
    if (task.has_open_issue) return 'Есть обращение склада';
    if (task.scan_count) return 'Сканирование начато';
    return 'Передано на склад';
  }

  function renderTasks(tasks) {
    rows.replaceChildren();
    if (!tasks.length) {
      const empty = rows.insertRow().insertCell();
      empty.colSpan = 6;
      empty.textContent = 'Заданий пока нет.';
      return;
    }
    for (const task of tasks) {
      const row = rows.insertRow();
      const values = ['№ ' + task.number + ' от ' + task.date, task.recipient_name,
        task.org_name, taskStatus(task), task.scan_count, stamp.format(new Date(task.updated_at))];
      for (const value of values) row.insertCell().textContent = value;
    }
  }

  function showLogin() {
    workspace.hidden = true;
    rows.replaceChildren();
    $('sales-identity').textContent = '';
    upload.reset();
    login.hidden = !/^[A-Za-z0-9_-]{32,128}$/.test(linkKey);
    if (login.hidden) notice.textContent = 'Для входа откройте персональную ссылку.';
  }

  function errorMessage(result) {
    const message = result.data?.message || result.data?.detail;
    if (typeof message === 'string') return message;
    if (result.kind === 'timeout') return 'Сервер долго отвечает. Обновите список заданий перед повтором.';
    if (result.kind === 'network') return 'Не удалось связаться с сервером.';
    return 'Не удалось выполнить запрос.';
  }

  async function loadTasks() {
    const result = await GisApi.request(api + '/tasks');
    if (result.ok) renderTasks(result.data.tasks);
    else if (result.status === 401) showLogin();
    else notice.textContent = errorMessage(result);
  }

  async function openWorkspace(username) {
    linkKey = '';
    login.hidden = true;
    workspace.hidden = false;
    $('sales-identity').textContent = username;
    await perform(loadTasks);
  }

  async function perform(action) {
    for (const control of controls) control.disabled = true;
    notice.textContent = '';
    try {
      await action();
    } finally {
      for (const control of controls) control.disabled = false;
    }
  }

  login.addEventListener('submit', async event => {
    event.preventDefault();
    const button = login.querySelector('button');
    button.disabled = true;
    const result = await GisApi.request(api + '/session', {
      method: 'POST', headers: {...writeHeaders, 'Content-Type': 'application/json'},
      body: JSON.stringify({username: $('sales-username').value.trim(), link_key: linkKey})
    });
    button.disabled = false;
    if (result.ok) await openWorkspace(result.data.username);
    else notice.textContent = errorMessage(result);
  });

  upload.addEventListener('submit', event => {
    event.preventDefault();
    perform(async () => {
      notice.textContent = 'Загружаем задание…';
      const result = await GisApi.request(api + '/tasks', {
        method: 'POST', headers: writeHeaders, body: new FormData(upload), timeoutMs: 120000
      });
      if (result.status === 401) {
        showLogin();
        return;
      }
      if (result.ok) {
        upload.reset();
        notice.textContent = ['Задание № ' + result.data.number + ' передано на склад.',
          ...result.data.import_warnings].join('\n');
      } else {
        notice.textContent = errorMessage(result);
      }
      if (result.ok || result.status === 409) await loadTasks();
    });
  });

  $('sales-refresh').addEventListener('click', () => perform(loadTasks));
  $('sales-logout').addEventListener('click', () => perform(async () => {
    const result = await GisApi.request(api + '/session', {method: 'DELETE', headers: writeHeaders});
    if (result.ok) showLogin();
    else notice.textContent = errorMessage(result);
  }));

  if (linkKey) showLogin();
  else GisApi.request(api + '/session').then(result => {
    if (result.ok) openWorkspace(result.data.username);
    else if (result.status === 401) showLogin();
    else notice.textContent = errorMessage(result);
  });
})();
