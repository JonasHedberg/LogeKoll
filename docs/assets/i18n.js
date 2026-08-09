(() => {
  'use strict';
  const manifestURL = new URL('locales/manifest.json', document.baseURI);
  const languageControl = document.querySelector('[data-language-control]');
  const languageSelect = document.querySelector('[data-language-select]');

  const getByPath = (obj, path) => path.split('.').reduce((value, key) => value?.[key], obj);
  const requestedLanguage = () => {
    const query = new URLSearchParams(location.search).get('lang');
    if (query) return query.toLowerCase();
    const saved = localStorage.getItem('logekoll-language');
    if (saved) return saved.toLowerCase();
    return (navigator.language || 'sv').toLowerCase().split('-')[0];
  };

  async function start() {
    try {
      const manifest = await fetch(manifestURL, {cache: 'no-cache'}).then(r => {
        if (!r.ok) throw new Error(`Språkmanifest: ${r.status}`);
        return r.json();
      });
      const supported = manifest.locales.map(x => x.code);
      const desired = requestedLanguage();
      const locale = supported.includes(desired) ? desired : manifest.default;
      const entry = manifest.locales.find(x => x.code === locale);
      const dictURL = new URL(entry.file, manifestURL);
      const dict = await fetch(dictURL, {cache: 'no-cache'}).then(r => {
        if (!r.ok) throw new Error(`Språkfil: ${r.status}`);
        return r.json();
      });

      document.documentElement.lang = locale;
      document.querySelectorAll('[data-i18n]').forEach(el => {
        const value = getByPath(dict, el.dataset.i18n);
        if (typeof value === 'string') el.textContent = value;
      });
      document.querySelectorAll('[data-i18n-html]').forEach(el => {
        const value = getByPath(dict, el.dataset.i18nHtml);
        if (typeof value === 'string') el.innerHTML = value;
      });
      document.querySelectorAll('[data-i18n-aria]').forEach(el => {
        const value = getByPath(dict, el.dataset.i18nAria);
        if (typeof value === 'string') el.setAttribute('aria-label', value);
      });

      if (manifest.locales.length > 1 && languageControl && languageSelect) {
        languageSelect.replaceChildren(...manifest.locales.map(item => {
          const option = document.createElement('option');
          option.value = item.code; option.textContent = item.label; option.selected = item.code === locale;
          return option;
        }));
        languageControl.hidden = false;
        languageSelect.addEventListener('change', () => {
          localStorage.setItem('logekoll-language', languageSelect.value);
          const url = new URL(location.href); url.searchParams.set('lang', languageSelect.value); location.href = url;
        });
      }
    } catch (error) {
      console.warn('LogeKoll: svensk reservtext används.', error);
    }
  }
  start();
})();
