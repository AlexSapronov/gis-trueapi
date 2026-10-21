/* Shared event-driven scanner focus lock for general scan and warehouse pages. */
window.ScannerFocus = {
  create({input, active, submit, resume = () => {}}) {
    let timer;
    const editable = el => !!el && (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable);
    function focus() {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!active() || document.hidden || document.querySelector('dialog[open]')) return;
        if (editable(document.activeElement) && document.activeElement !== input) return;
        try { input.focus({preventScroll: true}); } catch (_) { input.focus(); }
      }, 60);
    }
    ['pointerup', 'mouseup'].forEach(name => document.addEventListener(name, focus));
    input.addEventListener('blur', focus);
    ['focus', 'pageshow'].forEach(name => window.addEventListener(name, focus));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { resume(); focus(); } });
    document.addEventListener('keydown', e => {
      if (!active() || document.querySelector('dialog[open]') || editable(document.activeElement)) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        const code = input.value;
        input.value = '';
        submit(code);
      } else if (e.key && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        input.value += e.key;
        input.dispatchEvent(new Event('input'));
      }
      focus();
    });
    return {focus};
  }
};
