// Confirm destructive forms
document.addEventListener('submit', e => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});
// Live download progress + refresh while indexing
(function () {
  const rows = document.querySelectorAll('[data-media]');
  const waiting = document.querySelector('[data-reload-when-idle]');
  if (!rows.length && !waiting) return;
  let wasActive = false;
  async function tick() {
    try {
      const r = await fetch('/api/status');
      if (!r.ok) return;
      const s = await r.json();
      let active = false;
      rows.forEach(row => {
        const p = s.progress[row.dataset.media];
        const bar = row.querySelector('.prog');
        if (!bar) return;
        if (p) {
          active = true;
          bar.hidden = false;
          bar.querySelector('span').style.width = p.percent + '%';
          bar.querySelector('em').textContent = `${p.percent}%  ${p.speed || ''}  ${p.eta ? 'ETA ' + p.eta : ''}`;
        } else if (!bar.hidden) {
          location.reload();
        }
      });
      if (waiting && s.indexing === null) location.reload();
      wasActive = active;
    } catch (_) {}
  }
  tick();
  setInterval(tick, 2000);
})();

// Profile form: "Audio only" quality <-> audio format
document.querySelectorAll('form').forEach(f => {
  const q = f.querySelector('[data-quality]'), c = f.querySelector('[data-container]');
  if (!q || !c) return;
  const groups = c.querySelectorAll('optgroup');
  function sync(from) {
    const audio = from === 'container' ? c.selectedOptions[0].parentElement === groups[1] : q.value === 'audio';
    if (from === 'container') {
      if (audio) q.value = 'audio';
      else if (q.value === 'audio') q.value = '1080';
    } else if ((c.selectedOptions[0].parentElement === groups[1]) !== audio) {
      c.value = audio ? 'mp3' : 'mp4';
    }
  }
  q.addEventListener('change', () => sync('quality'));
  c.addEventListener('change', () => sync('container'));
});
