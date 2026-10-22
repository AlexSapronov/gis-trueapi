/* Runtime settings editor. Server schema supplies labels and validation bounds. */
window.AdminSettings = {
  create({json, action, message, onSaved}) {
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const labels = {ok:'Допустимо', warning:'Внимание', error:'Ошибка', EMITTED:'Эмитирован', APPLIED:'Нанесён', INTRODUCED:'В обороте'};
    let config, dirty = false;
    const form = $('application-settings-form');
    const groups = [
      ['Функции склада', ['report_issues','return_to_warehouse','worker_delete_scans','manual_input','auto_pn','report_comment_required']],
      ['Правила документов', ['strict_owner_default','receipt_default_org','receipt_missing_codes_threshold','shipment_statuses','check_timeout_seconds']],
      ['Вердикты сверки КМ', ['verdict_emitted','verdict_applied','verdict_introduced','verdict_unknown']]
    ];
    const describe = value => Array.isArray(value) ? value.map(v=>labels[v] || v).join(', ') : typeof value === 'boolean' ? (value ? 'Включено' : 'Отключено') : labels[value] || config.organizations.find(o=>o.inn===value)?.name || value || 'Не задано';
    function fields(values) {
      const props = config.schema.properties;
      const option = (v, selected, title) => `<option value="${esc(v)}" ${selected ? 'selected' : ''}>${esc(title ?? labels[v] ?? v)}</option>`;
      $('application-settings-fields').innerHTML = groups.map(([title, keys]) => `<fieldset class="wh-policy-group"><legend>${title}</legend>${keys.map(key => {
        const field = props[key], type = field.$ref ? config.schema.$defs[field.$ref.split('/').pop()] : field;
        const value = values[key], id = 'setting-' + key;
        let control;
        if (type.type === 'boolean') control = `<input id="${id}" data-setting="${key}" type="checkbox" ${value ? 'checked' : ''}>`;
        else if (key === 'receipt_default_org') control = `<select id="${id}" data-setting="${key}">${option('', !value, 'Не задана')}${config.organizations.map(o=>option(o.inn, o.inn===value, o.name+' · '+o.inn)).join('')}</select>`;
        else if (type.enum) control = `<select id="${id}" data-setting="${key}">${type.enum.map(v=>option(v, v===value)).join('')}</select>`;
        else if (type.type === 'array') control = `<div class="wh-policy-options">${type.items.enum.map(v=>`<label><input type="checkbox" data-status="${v}" ${value.includes(v) ? 'checked' : ''}> ${esc(labels[v])} (${v})</label>`).join('')}</div>`;
        else control = `<input id="${id}" data-setting="${key}" type="number" min="${type.minimum}" max="${type.maximum}" step="1" value="${value}" required>`;
        return `<div class="wh-policy-field">${type.type === 'array' ? `<span>${esc(field.title)}</span>${control}` : `<label for="${id}">${type.type === 'boolean' ? control+' '+esc(field.title) : esc(field.title)+control}</label>`}<small class="wh-muted">${esc(field.description)}</small></div>`;
      }).join('')}</fieldset>`).join('');
      state();
    }
    function state() {
      $('settings-unsaved').textContent = dirty ? 'Есть несохранённые изменения' : 'Изменения сохранены';
      $('settings-save').disabled = !dirty;
    }
    function render() {
      fields(config.runtime.values);
      $('settings-meta').textContent = `Версия ${config.runtime.revision}` + (config.runtime.updated_at ? ' · Обновлено: '+new Date(config.runtime.updated_at).toLocaleString('ru-RU') : ' · Исходные настройки');
      $('settings-history').innerHTML = config.history.map(h=>`<details class="wh-code"><summary>Версия ${h.revision} · ${esc(new Date(h.created_at).toLocaleString('ru-RU'))}</summary>${Object.entries(h.changes).map(([key,c])=>`<p><b>${esc(config.schema.properties[key]?.title || key)}</b>: ${esc(describe(c.before))} → ${esc(describe(c.after))}</p>`).join('')}</details>`).join('') || '<p class="wh-muted">Настройки ещё не менялись.</p>';
    }
    async function load() {
      config = await json('/api/admin/warehouse/settings'); dirty = false; render(); onSaved(config);
    }
    form.addEventListener('input', () => {dirty = true; state();});
    form.addEventListener('change', () => {dirty = true; state();});
    form.onsubmit = e => {e.preventDefault(); action(async()=>{
      const values = {...config.runtime.values};
      form.querySelectorAll('[data-setting]').forEach(el=>{values[el.dataset.setting] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value;});
      values.shipment_statuses = [...form.querySelectorAll('[data-status]:checked')].map(el=>el.dataset.status);
      if (!values.shipment_statuses.length) throw new Error('Выберите хотя бы один допустимый статус отгрузки.');
      const policyChanged = [...values.shipment_statuses].sort().join() !== [...config.runtime.values.shipment_statuses].sort().join();
      await json('/api/admin/warehouse/settings', {method:'PUT', body:{revision:config.runtime.revision,values}});
      await load(); message('Настройки сохранены и применены.' + (policyChanged ? ' Незавершённые отгрузки нужно проверить повторно.' : ''));
    });};
    $('settings-reload').onclick = ()=>{if (!dirty || confirm('Отменить несохранённые изменения и загрузить текущие настройки?')) action(load);};
    $('settings-defaults').onclick = ()=>{if (confirm('Подставить исходные настройки? Для применения нажмите «Сохранить настройки».')) {dirty=true;fields(config.defaults);}};
    return {load, isDirty:()=>dirty};
  }
};
