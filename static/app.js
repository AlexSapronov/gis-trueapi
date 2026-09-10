(function () {
  'use strict';

  // ===== состояние =====
  const S = {
    serverOnline: false,     // backend достижим
    trueapi: null,           // {state, last_success, last_error...}
    orgs: [],                // список орг с token_configured
    mode: 'mock',
    soundOn: localStorage.getItem('gis_sound') !== 'off', // default on
    theme: localStorage.getItem('gis_theme'),             // null = авто
    activeTab: 'scan',
    lastScanAt: null,        // timestamp последнего скана
  };

  const STATUS_RU = { EMITTED: 'Эмитирован', APPLIED: 'Нанесён', INTRODUCED: 'В обороте' };
  const statusRu = (s) => (s ? (STATUS_RU[s] || s) : '');

  // ===== DOM =====
  const $ = (id) => document.getElementById(id);
  const statusBar = $('status-bar'), statusDot = $('status-dot'),
        statusTitle = $('status-title'), statusSub = $('status-sub'),
        statusDetail = $('status-detail');
  const scanInput = $('scan-input'), scanResult = $('scan-result');
  const batchInput = $('batch-input'), batchCheck = $('batch-check'),
        batchClear = $('batch-clear'), batchSummary = $('batch-summary'),
        batchTable = $('batch-table');
  const balanceInput = $('balance-input'), balanceCheck = $('balance-check'),
        balanceResult = $('balance-result');
  const soundBtn = $('sound-btn'), themeBtn = $('theme-btn');

  // ===== тема =====
  function applyTheme() {
    const chosen = S.theme;
    let theme = chosen;
    if (!chosen) {
      theme = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', theme);
    themeBtn.textContent = theme === 'dark' ? '☀️' : '☾';
    themeBtn.title = theme === 'dark' ? 'Светлая тема' : 'Тёмная тема';
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const next = cur === 'dark' ? 'light' : 'dark';
    S.theme = next;
    localStorage.setItem('gis_theme', next);
    applyTheme();
  }
  themeBtn.addEventListener('click', toggleTheme);

  // ===== звук =====
  function updateSoundBtn() {
    soundBtn.textContent = S.soundOn ? '🔊' : '🔇';
    soundBtn.title = S.soundOn ? 'Звук включён' : 'Звук выключен';
  }
  soundBtn.addEventListener('click', () => {
    S.soundOn = !S.soundOn;
    localStorage.setItem('gis_sound', S.soundOn ? 'on' : 'off');
    updateSoundBtn();
  });

  let audioCtx = null;
  function beep(freq, durMs, when) {
    if (!S.soundOn) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const t = audioCtx.currentTime + (when || 0);
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.type = 'square';
      o.frequency.value = freq;
      g.gain.setValueAtTime(0.12, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + durMs / 1000);
      o.connect(g); g.connect(audioCtx.destination);
      o.start(t); o.stop(t + durMs / 1000);
    } catch (e) { /* нет поддержки — игнорируем */ }
  }
  function vibrate(pattern) {
    if (S.soundOn && navigator.vibrate) { try { navigator.vibrate(pattern); } catch (e) {} }
  }
  function sfxSuccess() { beep(880, 120); vibrate(50); }
  function sfxError() { beep(220, 160, 0); beep(220, 160, 0.18); vibrate([80, 40, 120]); }
  function sfxWarning() { beep(660, 90, 0); beep(660, 90, 0.12); vibrate([40, 60, 40]); }

  // ===== статистика сессии =====
  function loadStats() {
    try {
      const s = JSON.parse(sessionStorage.getItem('gis_stats') || '{"total":0,"ok":0,"warn":0,"err":0}');
      return { total: s.total|0, ok: s.ok|0, warn: s.warn|0, err: s.err|0 };
    } catch (e) { return { total: 0, ok: 0, warn: 0, err: 0 }; }
  }
  function saveStats(st) { sessionStorage.setItem('gis_stats', JSON.stringify(st)); }
  function bumpStats(verdict) {
    const st = loadStats();
    st.total++;
    if (verdict === 'ok') st.ok++;
    else if (verdict === 'warning') st.warn++;
    else st.err++;
    saveStats(st); renderStats(st);
  }
  function renderStats(st) {
    const el = $('session-stats');
    el.innerHTML = '';
    const parts = [
      ['За сессию: ', '<b>' + st.total + '</b>', ''],
      ['✓ ', '<b>' + st.ok + '</b>', 'ok'],
      ['⚠️ ', '<b>' + st.warn + '</b>', 'warn'],
      ['✕ ', '<b>' + st.err + '</b>', 'err'],
    ];
    parts.forEach(([pre, num, cls]) => {
      const s = document.createElement('span');
      s.className = 'stat ' + cls;
      s.innerHTML = pre + num;
      el.appendChild(s);
    });
    const btn = document.createElement('button');
    btn.className = 'reset-btn'; btn.id = 'reset-stats'; btn.textContent = 'сбросить';
    btn.addEventListener('click', () => { saveStats({total:0,ok:0,warn:0,err:0}); renderStats(loadStats()); });
    el.appendChild(btn);
  }

  // ===== status bar =====
  function hasToken() {
    const orgs = S.orgs || [];
    // "рабочая" организация = любая с токеном (для отображения статуса);
    // в live-режиме нужен хотя бы один токен.
    return !!(orgs.length && orgs.some((o) => o.token_configured));
  }

  function setStatus(level, title, sub, detailText) {
    statusBar.className = 'status-bar ' + level;
    statusTitle.textContent = title;
    statusSub.textContent = sub || '';
    statusDetail.textContent = detailText || '';
  }

  // Человеческая интерпретация возраста последнего успешного контакта.
  function agoText(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '';
    const diff = Date.now() - t;
    if (diff < 0) return iso.slice(11, 16);
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'только что';
    if (m < 60) return m + ' мин назад';
    const h = Math.floor(m / 60);
    if (h < 24) return h + ' ч назад';
    return Math.floor(h / 24) + ' дн назад';
  }

  function computeStatus() {
    if (!S.serverOnline) {
      setStatus('err', 'НЕТ СВЯЗИ С СЕРВЕРОМ', 'Подключение восстанавливается…',
        'Frontend не может доступиться до backend.');
      showServerOverlay(true);
      return;
    }
    showServerOverlay(false);

    // сервер онлайн
    if (S.mode !== 'live') {
      setStatus('warn', 'ДЕМО-РЕЖИМ', 'Тестовый режим без Честного знака', 'mode = ' + S.mode);
      return;
    }

    // нет токена
    if (!hasToken()) {
      setStatus('warn', 'НЕ НАСТРОЕН ДОСТУП К ЧЗ', 'Не настроен доступ к Честному знаку',
        'Для рабочих организаций отсутствует токен True API.');
      return;
    }

    // состояние True API
    const ta = S.trueapi;
    if (!ta || ta.state === 'unknown') {
      setStatus('ok', 'СИСТЕМА РАБОТАЕТ', 'ЧЗ: готов к проверке',
        'Обращений к True API после запуска ещё не было (state=unknown).');
      return;
    }

    if (ta.state === 'error') {
      const cat = ta.last_error || '';
      // различаем тип проблемы, а не валим всё в "ошибка ЧЗ"
      if (cat === 'token_invalid' || cat === 'unauthorized') {
        setStatus('warn', 'ТОКЕН ЧЗ ИСТЁК', 'Требуется обновить токен',
          'last_error=' + cat + ' time=' + (ta.last_error_time || '?'));
      } else if (cat === 'forbidden' || cat === 'no_permission') {
        setStatus('warn', 'НЕТ ДОСТУПА К ЧЗ', 'Недостаточно прав для операции',
          'last_error=' + cat + ' time=' + (ta.last_error_time || '?'));
      } else if (cat === 'rate_limit') {
        setStatus('warn', 'ЧЗ: СЛИШКОМ МНОГО ЗАПРОСОВ', 'Подождите и повторите',
          'last_error=' + cat + ' time=' + (ta.last_error_time || '?'));
      } else {
        setStatus('err', 'ЧЕСТНЫЙ ЗНАК НЕДОСТУПЕН', 'Сервис маркировки временно недоступен',
          'last_error=' + cat + ' time=' + (ta.last_error_time || '?'));
      }
      return;
    }

    // state = ok
    const ago = agoText(ta.last_success);
    const sub = ta.last_success
      ? (ago === 'только что' ? 'ЧЗ: последняя связь только что' : 'ЧЗ: готов к проверке · последняя связь ' + ago)
      : 'ЧЗ: готов к проверке';
    setStatus('ok', 'СИСТЕМА РАБОТАЕТ', sub,
      'last_success=' + (ta.last_success || '?'));
  }

  // полноэкранный оверлей "НЕТ СВЯЗИ"
  let overlay = null;
  function showServerOverlay(show) {
    if (show) {
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.className = 'server-overlay';
        overlay.innerHTML =
          '<div class="big-ico">⚠️</div>' +
          '<div class="big-title">НЕТ СВЯЗИ</div>' +
          '<div class="sub">Сервер временно недоступен.<br>Повторное подключение выполняется автоматически.</div>' +
          '<div class="dots">● ● ●</div>';
        document.body.appendChild(overlay);
      }
      overlay.style.display = 'flex';
    } else if (overlay) {
      overlay.style.display = 'none';
    }
  }

  // клик по статус-бару раскрывает тех. детали
  statusBar.addEventListener('click', (e) => {
    if (e.target.closest('.icon-btn')) return; // не перехватываем кнопки
    statusBar.classList.toggle('open');
  });

  // ===== polling backend =====
  async function checkBackend() {
    try {
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), 4000);
      const r = await fetch('/api/status', { signal: ctrl.signal });
      clearTimeout(to);
      if (!r.ok) { S.serverOnline = false; computeStatus(); return; }
      const d = await r.json();
      S.serverOnline = true;
      S.mode = d.mode;
      S.trueapi = d.trueapi;
      S.orgs = d.organizations || [];
      computeStatus();
    } catch (e) {
      S.serverOnline = false;
      computeStatus();
    }
  }

  window.addEventListener('online', checkBackend);
  window.addEventListener('offline', () => { S.serverOnline = false; computeStatus(); });

  // ===== вкладки =====
  const tabs = document.querySelectorAll('.tab');
  const panels = { scan: $('tab-scan'), batch: $('tab-batch'), balance: $('tab-balance') };
  function switchTab(name) {
    S.activeTab = name;
    tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    Object.keys(panels).forEach((k) => panels[k].classList.toggle('active', k === name));
    if (name === 'scan') focusScan();
    else if (name === 'batch') batchInput.focus();
    else if (name === 'balance') balanceInput.focus();
  }
  tabs.forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));

  // ===== focus manager =====
  function focusScan() {
    if (S.activeTab === 'scan') {
      // небольшой отложенный фокус, чтобы не конфликтовать с кликами по кнопкам
      setTimeout(() => { try { scanInput.focus(); } catch (e) {} }, 30);
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { checkBackend(); focusScan(); }
  });

  // ===== утилиты =====
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  }
  function nowTime() {
    const d = new Date();
    return d.toTimeString().slice(0, 8);
  }

  // ===== общий fetch-helper =====
  async function apiFetch(url, opts) {
    opts = opts || {};
    const ctrl = new AbortController();
    opts.signal = ctrl.signal;
    const to = setTimeout(() => ctrl.abort(), opts.timeoutMs || 15000);
    let r;
    try {
      r = await fetch(url, opts);
    } catch (e) {
      clearTimeout(to);
      return { ok: false, kind: 'network', status: 0, data: null };
    }
    clearTimeout(to);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (r.ok) return { ok: true, kind: 'ok', status: r.status, data };
    if (r.status >= 400 && r.status < 500) return { ok: false, kind: 'biz', status: r.status, data };
    return { ok: false, kind: 'server', status: r.status, data };
  }
  function showScanError(msg, retry) {
    scanResult.innerHTML = '<div class="result-card err"><div class="verdict">✕ ОШИБКА</div>' +
      '<div class="err-msg">' + esc(msg) + '</div>' +
      (retry ? '<div class="retry">' + esc(retry) + '</div>' : '') + '</div>';
  }

  // Извлекает человекочитаемое сообщение из тела ошибки (в т.ч. Pydantic 422).
  // Понимает: data.message; строковый data.detail; массив data.detail[] (локи + msg).
  // Убирает технический префикс "Value error, " если он есть.
  function apiErrorMessage(data, fallback) {
    if (!data) return fallback || 'Ошибка запроса';
    if (data.message) return String(data.message);
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail) && data.detail.length) {
      const items = data.detail.map((d) => {
        if (d && d.msg) return String(d.msg);
        if (typeof d === 'string') return d;
        return '';
      }).filter(Boolean);
      if (items.length) {
        let msg = items[0];
        // срезаем технические префиксы FastAPI/Pydantic
        msg = msg.replace(/^Value error,\s*/i, '');
        return msg;
      }
    }
    return fallback || 'Ошибка запроса';
  }

  // ===== одиночный скан =====
  let scanning = false; // lock от дубля Enter

  function renderPending() {
    scanResult.innerHTML = '<div class="result-card neutral"><span class="spinner"></span>Проверяем…</div>';
  }

  function renderScan(d) {
    let html;
    if (!d) { return; }

    const verdict = d.verdict || 'error';

    // Структурная/API-ошибка: красная карточка.
    if (verdict === 'error') {
      const isStructure = (d.error_category === 'dm_structure' || d.error_category === 'gtin_checksum');
      const title = isStructure ? 'ОШИБКА' : (d.error_category === 'km_not_found' ? 'КМ НЕ НАЙДЕН' : 'ОШИБКА');
      const msg = d.error_message || d.verdict_message || 'Ошибка проверки';
      html = '<div class="result-card err">' +
        '<div class="verdict">✕ ' + esc(title) + '</div>' +
        '<div class="err-msg">' + esc(msg) + '</div>' +
        '<div class="retry">Повторите сканирование</div>' +
        buildDetails(d) +
        '</div>';
      bumpStats('error'); sfxError();
    } else if (verdict === 'warning') {
      // EMITTED / неизвестный статус: жёлтая карточка. Не ошибка.
      const statusStr = statusRu(d.status);
      const ours = d.ours === true;
      const foreign = d.ours === false;
      const ownerBadge = ours ? '<span class="owner-badge ours">НАШ КМ</span>'
        : foreign ? '<span class="owner-badge foreign">ЧУЖОЙ КМ</span>' : '';
      html = '<div class="result-card warn">' +
        '<div class="verdict">⚠️ ВНИМАНИЕ</div>' +
        '<div class="product">' + esc(d.product_name || '—') + '</div>' +
        '<div class="qty">' + (d.quantity_in_pack != null ? esc(fmtNum(d.quantity_in_pack)) + ' шт.' : '') + '</div>' +
        '<div class="status-pill">' + esc(statusStr || d.status || '') + '</div>' +
        '<div class="owner">' + esc(d.our_org_name || d.owner_name || '') + '</div>' +
        ownerBadge +
        (d.verdict_message ? '<div class="warn-msg">' + esc(d.verdict_message) + '</div>' : '') +
        buildDetails(d) +
        '</div>';
      bumpStats('warning'); sfxWarning();
    } else {
      // ok
      const statusStr = statusRu(d.status);
      const ours = d.ours === true;
      const foreign = d.ours === false;
      const ownerBadge = ours ? '<span class="owner-badge ours">НАШ КМ</span>'
        : foreign ? '<span class="owner-badge foreign">ЧУЖОЙ КМ</span>' : '';
      html = '<div class="result-card ok">' +
        '<div class="verdict">✓ OK</div>' +
        '<div class="product">' + esc(d.product_name || '—') + '</div>' +
        '<div class="qty">' + (d.quantity_in_pack != null ? esc(fmtNum(d.quantity_in_pack)) + ' шт.' : '') + '</div>' +
        '<div class="status-pill">' + esc(statusStr || d.status || '') + '</div>' +
        '<div class="owner">' + esc(d.our_org_name || d.owner_name || '') + '</div>' +
        ownerBadge +
        buildDetails(d) +
        '</div>';
      bumpStats('ok'); sfxSuccess();
    }
    S.lastScanAt = new Date();
    scanResult.innerHTML = html +
      '<div class="last-check">Последняя проверка: ' + nowTime() + '</div>';
    // привязка "Подробнее"
    bindDetails();
  }

  function fmtNum(n) {
    try { return Number(n).toLocaleString('ru-RU'); } catch (e) { return n; }
  }

  function buildDetails(d) {
    const rows = [];
    if (d.gtin) rows.push(['GTIN', d.gtin]);
    if (d.serial) rows.push(['Serial', d.serial]);
    if (d.product_name) rows.push(['productName', d.product_name]);
    if (d.status) rows.push(['status', d.status]);
    if (d.status_ex) rows.push(['statusEx', d.status_ex]);
    if (d.owner_inn) rows.push(['ownerInn', d.owner_inn]);
    if (d.owner_name) rows.push(['ownerName', d.owner_name]);
    if (d.quantity_in_pack != null) rows.push(['quantityInPack', d.quantity_in_pack]);
    if (d.structure_error) rows.push(['локальная проверка', d.structure_error]);
    if (d.structure_valid) rows.push(['локальная проверка', 'OK']);
    rows.push(['API-проверка', d.api_checked ? 'выполнена' : 'не выполнена']);
    if (!rows.length) return '';
    const body = rows.map(([k, v]) => '<div class="kv"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v) + '</div></div>').join('');
    return '<button class="details-toggle">Подробнее ▾</button>' +
      '<div class="details-body">' + body + '</div>';
  }

  function bindDetails() {
    document.querySelectorAll('.details-toggle').forEach((btn) => {
      if (btn._bound) return;
      btn._bound = true;
      btn.addEventListener('click', () => {
        const body = btn.nextElementSibling;
        const open = body.classList.toggle('open');
        btn.textContent = open ? 'Подробнее ▴' : 'Подробнее ▾';
        if (!open) focusScan();
      });
    });
  }

  async function doScan(code) {
    if (scanning) return;         // дубль Enter
    if (!code || !code.trim()) return;
    scanning = true;
    renderPending();
    const res = await apiFetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code.trim() }),
    });
    if (res.kind === 'network') {
      S.serverOnline = false; computeStatus();
      showScanError('Сервер недоступен', 'Подключение восстановится автоматически');
      focusScan();
      scanning = false;
      return;
    }
    // backend достигнут (4xx/5xx/2xx) — связь есть
    S.serverOnline = true;
    if (res.kind === 'server') {
      showScanError('Внутренняя ошибка сервера', 'Повторите сканирование');
      focusScan();
      scanning = false;
      return;
    }
    if (res.kind === 'biz') {
      const msg = apiErrorMessage(res.data, 'Ошибка проверки');
      showScanError(msg, 'Повторите сканирование');
      focusScan();
      scanning = false;
      return;
    }
    // success
    renderScan(res.data);
    focusScan();
    scanning = false;
  }

  scanInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const code = scanInput.value;
      scanInput.value = '';
      doScan(code);
    }
  });
  scanResult.addEventListener('click', (e) => {
    if (!e.target.closest('.details-toggle')) focusScan();
  });

  // ===== пакетная =====
  batchCheck.addEventListener('click', async () => {
    const lines = batchInput.value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!lines.length) return;
    batchCheck.disabled = true; batchCheck.textContent = 'Проверка…';
    const res = await apiFetch('/api/scan_batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ codes: lines }),
    });
    batchCheck.disabled = false; batchCheck.textContent = 'Проверить';
    if (res.kind === 'network') {
      S.serverOnline = false; computeStatus();
      batchSummary.classList.remove('hidden');
      batchSummary.innerHTML = '<span class="err-n">Сервер недоступен</span>';
      return;
    }
    S.serverOnline = true;
    if (res.kind === 'server') {
      batchSummary.classList.remove('hidden');
      batchSummary.innerHTML = '<span class="err-n">Внутренняя ошибка сервера</span>';
      return;
    }
    if (res.kind === 'biz') {
      batchSummary.classList.remove('hidden');
      const msg = apiErrorMessage(res.data, 'Ошибка проверки');
      batchSummary.innerHTML = '<span class="err-n">' + esc(msg) + '</span>';
      return;
    }
    renderBatch(res.data.results);
  });

  function renderBatch(results) {
    const ok = results.filter((r) => (r.verdict || 'error') === 'ok').length;
    const warn = results.filter((r) => (r.verdict || 'error') === 'warning').length;
    const err = results.length - ok - warn;
    batchSummary.classList.remove('hidden');
    batchSummary.innerHTML =
      '<span>Всего: <b>' + results.length + '</b></span>' +
      '<span class="ok-n">OK: <b>' + ok + '</b></span>' +
      '<span class="warn-n">Внимание: <b>' + warn + '</b></span>' +
      '<span class="err-n">Ошибок: <b>' + err + '</b></span>';
    batchTable.innerHTML = '';
    results.forEach((r) => {
      const row = document.createElement('div');
      const v = r.verdict || 'error';
      const isWarn = v === 'warning';
      const isErr = v === 'error';
      row.className = 'batch-row' + (isErr ? ' bad' : (isWarn ? ' warn' : ''));

      let pn, qty, status, owner;
      if (isErr) {
        pn = r.error_message || 'Ошибка';
        qty = ''; status = ''; owner = '';
      } else {
        pn = r.product_name || '—';
        qty = r.quantity_in_pack != null ? fmtNum(r.quantity_in_pack) + ' шт.' : '';
        status = statusRu(r.status);
        owner = r.ours === true ? 'Наш' : (r.ours === false ? 'Чужой' : '');
      }

      let detail;
      if (isErr) {
        detail = '<div class="kv"><div class="k">Ошибка</div><div class="v">' + esc(r.error_message) + '</div></div>' +
          (r.structure_error ? '<div class="kv"><div class="k">Локальная проверка</div><div class="v">' + esc(r.structure_error) + '</div></div>' : '');
      } else if (isWarn) {
        detail =
          '<div class="kv"><div class="k">GTIN</div><div class="v">' + esc(r.gtin) + '</div></div>' +
          '<div class="kv"><div class="k">Serial</div><div class="v">' + esc(r.serial) + '</div></div>' +
          '<div class="kv"><div class="k">productName</div><div class="v">' + esc(r.product_name) + '</div></div>' +
          '<div class="kv"><div class="k">status</div><div class="v">' + esc(r.status) + '</div></div>' +
          '<div class="kv"><div class="k">statusEx</div><div class="v">' + esc(r.status_ex) + '</div></div>' +
          '<div class="kv"><div class="k">ownerInn</div><div class="v">' + esc(r.owner_inn) + '</div></div>' +
          '<div class="kv"><div class="k">ownerName</div><div class="v">' + esc(r.owner_name) + '</div></div>' +
          '<div class="kv"><div class="k">quantityInPack</div><div class="v">' + esc(r.quantity_in_pack) + '</div></div>' +
          (r.verdict_message ? '<div class="kv"><div class="k">Пояснение</div><div class="v">' + esc(r.verdict_message) + '</div></div>' : '');
      } else {
        detail =
          '<div class="kv"><div class="k">GTIN</div><div class="v">' + esc(r.gtin) + '</div></div>' +
          '<div class="kv"><div class="k">Serial</div><div class="v">' + esc(r.serial) + '</div></div>' +
          '<div class="kv"><div class="k">productName</div><div class="v">' + esc(r.product_name) + '</div></div>' +
          '<div class="kv"><div class="k">status</div><div class="v">' + esc(r.status) + '</div></div>' +
          '<div class="kv"><div class="k">statusEx</div><div class="v">' + esc(r.status_ex) + '</div></div>' +
          '<div class="kv"><div class="k">ownerInn</div><div class="v">' + esc(r.owner_inn) + '</div></div>' +
          '<div class="kv"><div class="k">ownerName</div><div class="v">' + esc(r.owner_name) + '</div></div>' +
          '<div class="kv"><div class="k">quantityInPack</div><div class="v">' + esc(r.quantity_in_pack) + '</div></div>';
      }
      row.innerHTML =
        '<div class="head"><div class="pn">' + esc(pn) + '</div>' +
        '<div>' + esc(qty) + '</div>' +
        '<div>' + esc(status) + '</div>' +
        '<div>' + esc(owner) + '</div></div>' +
        '<div class="detail">' + detail + '</div>';
      row.addEventListener('click', () => row.classList.toggle('open'));
      batchTable.appendChild(row);
    });

    // сигнал по итогам batch (единый, не на каждую строку)
    if (err > 0) sfxError();
    else if (warn > 0) sfxWarning();
    else if (ok > 0) sfxSuccess();
  }

  batchClear.addEventListener('click', () => {
    batchInput.value = ''; batchTable.innerHTML = '';
    batchSummary.classList.add('hidden');
    batchInput.focus();
  });

  // ===== баланс =====
  async function doBalance() {
    const gtin = balanceInput.value.trim();
    if (!gtin) return;
    balanceResult.innerHTML = '<div class="result-card neutral"><span class="spinner"></span>Запрос…</div>';
    const res = await apiFetch('/api/balance', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gtin }),
    });
    if (res.kind === 'network') {
      S.serverOnline = false; computeStatus();
      balanceResult.innerHTML = '<div class="result-card err"><div class="verdict">✕ ОШИБКА</div><div class="err-msg">Сервер недоступен</div></div>';
      return;
    }
    // backend достигнут — связь есть (в т.ч. 422 = невалидный GTIN)
    S.serverOnline = true;
    if (res.kind === 'server') {
      balanceResult.innerHTML = '<div class="result-card err"><div class="verdict">✕ ОШИБКА</div><div class="err-msg">Внутренняя ошибка сервера</div></div>';
      return;
    }
    if (res.kind === 'biz') {
      const msg = apiErrorMessage(res.data, 'Ошибка запроса');
      balanceResult.innerHTML = '<div class="result-card err"><div class="verdict">✕ ОШИБКА</div><div class="err-msg">' + esc(msg) + '</div></div>';
      return;
    }
    renderBalance(res.data);
  }

  function renderBalance(d) {
    let html = '<div class="org-block"><h3>Общий итог</h3><div class="balance-grid">';
    d.statuses.forEach((s) => { html += balCard(s, d.total[s], true); });
    html += '</div></div>';
    d.organizations.forEach((o) => {
      const orgEntries = o.statuses || {};
      const orgHasAny = d.statuses.some((s) => orgEntries[s] && (!orgEntries[s].error));
      // показываем org-level error только если нет ни одного успешного статуса
      if (o.error && !orgHasAny) {
        html += '<div class="org-block"><h3>' + esc(o.name) + '</h3><div class="err-note">⚠ ' + esc(o.error) + '</div></div>';
        return;
      }
      html += '<div class="org-block"><h3>' + esc(o.name) + '</h3><div class="balance-grid">';
      d.statuses.forEach((s) => {
        const e = orgEntries[s] || { complete: false, error: 'Нет данных' };
        html += balCard(s, e, false);
      });
      html += '</div></div>';
    });
    balanceResult.innerHTML = html;
  }
  function balCard(status, t, isTotal) {
    if (!t) return '';
    const incomplete = (t.complete === false) || (t.error);
    let body;
    if (incomplete) {
      // данные по статусу неполные/недоступны — не подставляем ноль как реальный
      const km = (t.km_count != null) ? (fmtNum(t.km_count) + ' КМ') : '—';
      let note = t.error ? esc(t.error) : 'Данные неполные';
      if (t.failed_organizations && t.failed_organizations.length) {
        note = 'Данные по ' + t.failed_organizations.length + ' орг. не получены';
      }
      body = '<div class="n">' + km + '</div>' +
        '<div class="incomplete">⚠️ ' + note + '</div>';
    } else {
      const km = (t.km_count != null) ? (fmtNum(t.km_count) + ' КМ') : '0 КМ';
      const qtyLine = (status === 'EMITTED') ? '' : '<div class="q">' + fmtNum(t.quantity_sum) + ' шт.</div>';
      body = '<div class="n">' + km + '</div>' + qtyLine;
    }
    return '<div class="bal-card' + (isTotal ? ' bal-total' : '') +
      (incomplete ? ' bal-incomplete' : '') + '">' +
      '<div class="st">' + esc(statusRu(status)) + '</div>' + body + '</div>';
  }
  balanceCheck.addEventListener('click', doBalance);
  balanceInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); doBalance(); } });

  // ===== init =====
  applyTheme();
  updateSoundBtn();
  renderStats(loadStats());
  focusScan();
  checkBackend();
  setInterval(checkBackend, 6000);   // polling каждые 6 сек

  // первичный статус "Подключение..."
  setStatus('pending', 'ПОДКЛЮЧЕНИЕ…', 'Проверка сервера');
})();