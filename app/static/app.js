// Confirm destructive forms. Source deletes also carry the page's "delete downloaded files" choice.
document.addEventListener('submit', e => {
  let msg = e.target.dataset.confirm;
  const files = e.target.querySelector('[data-files]');
  if (files) {
    const on = !!document.querySelector('[data-files-toggle]:checked');
    files.value = on ? '1' : '';
    if (msg) msg += on ? ' Their downloaded files on disk are deleted too.' : ' Downloaded files stay on disk.';
  }
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

// Select videos or sources for bulk delete
(function () {
  const bar = document.getElementById('bulk');
  if (!bar) return;
  const boxes = () => [...document.querySelectorAll('input[name=ids][form=bulk]')];
  const all = bar.querySelector('[data-select-all]');
  function sync() {
    const picked = boxes().filter(b => b.checked);
    const n = picked.length;
    bar.hidden = n === 0;
    bar.querySelector('[data-selected]').textContent = `${n} selected`;
    all.checked = n > 0 && n === boxes().length;
    all.indeterminate = n > 0 && n < boxes().length;
    bar.dataset.confirm = bar.dataset.noun === 'source'
      ? `Delete ${n} source${n === 1 ? '' : 's'}?`
      : `Delete ${n} video${n === 1 ? '' : 's'} and their files from disk? ` +
        "They won't be downloaded again unless you choose Download.";
    boxes().forEach(b => b.closest('.frame, tr').classList.toggle('picked', b.checked));
  }
  document.addEventListener('change', e => { if (e.target.matches('input[name=ids][form=bulk]')) sync(); });
  all.addEventListener('change', () => { boxes().forEach(b => { b.checked = all.checked; }); sync(); });
  bar.querySelector('[data-clear]').addEventListener('click', () => { boxes().forEach(b => { b.checked = false; }); sync(); });
  sync();
})();

// Download folder location: "Type a path…" reveals a text box
document.querySelectorAll('[data-path-select]').forEach(sel => {
  const custom = sel.parentElement.querySelector('[name=path_custom]');
  const sync = () => { custom.hidden = sel.value !== '__custom'; if (!custom.hidden) custom.focus(); };
  sel.addEventListener('change', sync);
});
