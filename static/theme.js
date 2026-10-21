/* Shared theme preference for verification, warehouse and administration. */
window.GisTheme = (() => {
  let chosen;
  const buttons = new Set();
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  try { chosen = localStorage.getItem('gis_theme'); } catch (_) { /* browser storage can be disabled */ }
  function apply() {
    const theme = ['light','dark'].includes(chosen) ? chosen : media?.matches ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    buttons.forEach(button => {
      const label = theme === 'dark' ? 'Светлая тема' : 'Тёмная тема';
      button.textContent = theme === 'dark' ? '☀' : '☾';
      button.title = label; button.setAttribute('aria-label', label);
    });
  }
  function bind(button) {
    if (!button || buttons.has(button)) return;
    buttons.add(button);
    button.addEventListener('click', () => {
      chosen = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('gis_theme', chosen); } catch (_) { /* theme still changes in this page */ }
      apply();
    });
    apply();
  }
  window.addEventListener('storage', event => { if (event.key === 'gis_theme') { chosen=event.newValue; apply(); } });
  if (media?.addEventListener) media.addEventListener('change', apply);
  apply();
  return {bind};
})();
