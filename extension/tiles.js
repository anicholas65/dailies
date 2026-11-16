// A small "Dailies" chip on video tiles in YouTube's home feed, search, subscriptions and sidebars.
// From a tile you can download that video, add its whole channel as a series, or add its playlist,
// without opening the video or channel first.
(() => {
  const TILE = [
    'ytd-rich-item-renderer', 'yt-lockup-view-model', 'ytd-video-renderer', 'ytd-grid-video-renderer',
    'ytd-compact-video-renderer', 'ytd-playlist-renderer', 'ytd-grid-playlist-renderer', 'ytd-compact-playlist-renderer',
    'ytd-reel-item-renderer', 'ytm-shorts-lockup-view-model', 'ytd-channel-renderer',
  ].join(',');
  const ORIGIN = 'https://www.youtube.com';

  const host = document.createElement('div');
  host.id = 'dailies-tile';
  const shadow = host.attachShadow({ mode: 'closed' });
  shadow.innerHTML = `<style>
    :host { all: initial; position: absolute; z-index: 2147483000; }
    .chip { all: unset; cursor: pointer; display: flex; align-items: center; gap: 5px; padding: 4px 9px 4px 5px;
      border-radius: 999px; background: rgba(90,55,32,.92); color: #fff; font: 600 12px/1 system-ui, -apple-system,
      "Segoe UI", sans-serif; box-shadow: 0 2px 8px rgba(0,0,0,.4); }
    .chip:hover { background: #6d4429; } .chip:focus-visible { outline: 2px solid #e08a45; outline-offset: 2px; }
    .chip img { width: 16px; height: 16px; border-radius: 3px; }
    .panel { position: absolute; top: 30px; left: 0; border-radius: 6px; overflow: hidden; box-shadow: 0 8px 28px rgba(0,0,0,.4); }
    .panel.right { left: auto; right: 0; }
    .panel[hidden] { display: none; }
  </style>
  <button class="chip" title="Send to Dailies" aria-expanded="false"><img alt="" src="${chrome.runtime.getURL('icon.png')}">Dailies</button>
  <div class="panel" hidden></div>`;
  const chip = shadow.querySelector('.chip');
  const panel = shadow.querySelector('.panel');
  let tile = null;
  let hideTimer = 0;

  const abs = href => { try { return new URL(href, ORIGIN); } catch (_) { return null; } };
  const text = n => (n?.getAttribute?.('title') || n?.textContent || '').replace(/\s+/g, ' ').trim();

  // What a tile points at: [{url, title, label}] for its video, channel and (non-mix) playlist.
  function choices(t) {
    const links = [...t.querySelectorAll('a[href]')];
    let video = null, channel = null, list = null;
    for (const a of links) {
      const u = abs(a.getAttribute('href'));
      if (!u || !/(^|\.)youtube\.com$/.test(u.hostname)) continue;
      const id = u.searchParams.get('list');
      if (id && !list && !/^(RD|WL$|LL$)/.test(id)) list = { id, a };
      if (!video && (u.pathname === '/watch' && u.searchParams.get('v'))) video = { url: `${ORIGIN}/watch?v=${u.searchParams.get('v')}`, a };
      else if (!video && /^\/shorts\/[\w-]+/.test(u.pathname)) video = { url: ORIGIN + u.pathname, a };
      const ch = u.pathname.match(/^\/(@[^/]+|channel\/[\w-]+|c\/[^/]+|user\/[^/]+)/);
      if (ch && (!channel || (!channel.name && text(a)))) channel = { url: `${ORIGIN}/${ch[1]}`, name: text(a), handle: ch[1] };
    }
    const titleNode = t.querySelector('#video-title, .yt-lockup-metadata-view-model__title, h3 a, h3, #title');
    const title = text(titleNode) || video?.a.getAttribute('aria-label') || '';
    const out = [];
    if (video) out.push({ url: video.url, title, label: `This video${title ? ': ' + title : ''}` });
    if (channel) {
      const name = channel.name || decodeURIComponent(channel.handle.replace(/^(channel|c|user)\//, ''));
      out.push({ url: channel.url, title: name, label: `Whole channel: ${name}` });
    }
    if (list) out.push({ url: `${ORIGIN}/playlist?list=${list.id}`, title, label: `Playlist${title ? ': ' + title : ''}` });
    return out;
  }

  function place(t) {
    const r = (t.querySelector('#thumbnail, yt-thumbnail-view-model, a#thumbnail, img') || t).getBoundingClientRect();
    host.style.top = `${r.top + scrollY + 8}px`;
    host.style.left = `${r.left + scrollX + 8}px`;
    panel.classList.toggle('right', r.left + 300 > innerWidth);
  }

  function show(t) {
    clearTimeout(hideTimer);
    if (t === tile && host.isConnected) return;
    if (!choices(t).length) return hide();
    tile = t;
    place(t);
    if (!host.isConnected) document.documentElement.append(host);
  }

  function hide() {
    clearTimeout(hideTimer);
    close();
    tile = null;
    host.remove();
  }

  function close() {
    panel.hidden = true;
    panel.replaceChildren();
    chip.setAttribute('aria-expanded', 'false');
  }

  document.addEventListener('mouseover', e => {
    if (!panel.hidden) return; // keep the open form put
    if (e.composedPath().includes(host)) return clearTimeout(hideTimer);
    const t = e.target.closest?.(TILE);
    if (t) return show(t);
    if (host.isConnected) {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => panel.hidden && hide(), 250);
    }
  }, true);

  chip.addEventListener('click', async e => {
    e.preventDefault();
    e.stopPropagation();
    if (!panel.hidden) return close();
    if (!tile) return;
    const list = choices(tile);
    panel.hidden = false;
    chip.setAttribute('aria-expanded', 'true');
    // default to the series when the tile has one: that's what this chip is for over the page button
    const series = list.findIndex(c => !/^This video/.test(c.label));
    if (series > 0) list.unshift(...list.splice(series, 1));
    await dailiesForm(panel, { choices: list, title: list[0].title });
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
  document.addEventListener('click', e => { if (!e.composedPath().includes(host)) hide(); });
  document.addEventListener('yt-navigate-finish', hide);
  window.addEventListener('resize', () => tile && place(tile));
})();
