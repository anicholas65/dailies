// A "Dailies" button on YouTube video, playlist and channel pages, like Internet Download Manager's.
(() => {
  const host = document.createElement('div');
  host.id = 'dailies-extension';
  const shadow = host.attachShadow({ mode: 'closed' });
  shadow.innerHTML = `<style>
    :host { all: initial; position: fixed; right: 24px; bottom: 24px; z-index: 2147483000; }
    .btn { all: unset; cursor: pointer; display: flex; align-items: center; gap: 8px; padding: 9px 14px 9px 10px;
      border-radius: 999px; background: #5a3720; color: #fff; font: 600 13px/1 system-ui, -apple-system, "Segoe UI", sans-serif;
      box-shadow: 0 4px 14px rgba(0,0,0,.35); }
    .btn:hover { background: #6d4429; } .btn:focus-visible { outline: 2px solid #e08a45; outline-offset: 2px; }
    .btn img { width: 20px; height: 20px; border-radius: 4px; }
    .panel { position: absolute; right: 0; bottom: 48px; border-radius: 6px; overflow: hidden;
      box-shadow: 0 8px 28px rgba(0,0,0,.4); }
    .panel[hidden] { display: none; }
  </style>
  <div class="panel" hidden></div>
  <button class="btn" aria-expanded="false"><img alt="" src="${chrome.runtime.getURL('icon.png')}"><span></span></button>`;
  const btn = shadow.querySelector('.btn');
  const label = btn.querySelector('span');
  const panel = shadow.querySelector('.panel');

  // who made the video on a watch page, so the panel can offer the whole channel too
  function owner() {
    for (const a of document.querySelectorAll('ytd-watch-metadata ytd-video-owner-renderer a[href], #owner a[href], ytd-reel-player-overlay-renderer a[href^="/@"]')) {
      const m = (a.getAttribute('href') || '').match(/^(?:https:\/\/www\.youtube\.com)?\/(@[^/?#]+|channel\/[\w-]+)/);
      const name = (a.textContent || '').replace(/\s+/g, ' ').trim();
      if (m) return { url: `https://www.youtube.com/${m[1]}`, name: name || decodeURIComponent(m[1]) };
    }
    return null;
  }

  function pageInfo() {
    const title = document.title.replace(/^\(\d+\)\s*/, '').replace(/ - YouTube$/, '');
    return { url: location.href, title, choices: dailiesPageChoices(location.href, title, owner()) };
  }
  // the toolbar popup asks the page for the same choices
  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.type === 'pageChoices') reply(pageInfo());
  });

  function update() {
    const kind = dailiesKind(location.href);
    if (!kind || document.fullscreenElement) {
      host.remove();
      return;
    }
    label.textContent = kind === 'video' ? 'Download' : 'Add to Dailies';
    if (!host.isConnected) document.documentElement.append(host);
    close();
  }

  function close() {
    panel.hidden = true;
    panel.replaceChildren();
    btn.setAttribute('aria-expanded', 'false');
  }

  btn.addEventListener('click', async () => {
    if (!panel.hidden) return close();
    panel.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    await dailiesForm(panel, pageInfo());
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  document.addEventListener('click', e => { if (!e.composedPath().includes(host)) close(); });

  // YouTube is a single-page app: it fires this after every in-app navigation.
  document.addEventListener('yt-navigate-finish', update);
  document.addEventListener('fullscreenchange', update);
  window.addEventListener('popstate', update);
  update();
})();
