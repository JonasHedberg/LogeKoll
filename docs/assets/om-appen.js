/* Fullupplöst skärmbildsvisare. Ursprungslänken fungerar även utan JavaScript. */
(() => {
  'use strict';
  const dialog = document.querySelector('#guide-viewer');
  const image = document.querySelector('#viewer-image');
  const title = document.querySelector('#viewer-title');
  const original = document.querySelector('#viewer-original');
  const zoomButton = document.querySelector('#viewer-zoom');
  const closeButton = document.querySelector('#viewer-close');
  if (!dialog || !image || !title || !original || !zoomButton || !closeButton || typeof dialog.showModal !== 'function') return;
  document.querySelectorAll('.guide-screen-link').forEach(link => {
    link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      const img = link.querySelector('img');
      if (!img) return;
      image.src = link.href;
      image.alt = img.alt || '';
      title.textContent = link.dataset.screenTitle || img.alt || 'Skärmbild';
      original.href = link.href;
      dialog.classList.remove('is-zoomed');
      zoomButton.textContent = 'Visa 100 %';
      zoomButton.setAttribute('aria-pressed', 'false');
      dialog.showModal();
    });
  });
  zoomButton.addEventListener('click', () => {
    const enlarged = dialog.classList.toggle('is-zoomed');
    zoomButton.textContent = enlarged ? 'Anpassa bilden' : 'Visa 100 %';
    zoomButton.setAttribute('aria-pressed', String(enlarged));
  });
  closeButton.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener('close', () => { image.removeAttribute('src'); });
})();
