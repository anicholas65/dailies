// The "send to Dailies" form, shared by the YouTube page button (content.js) and the toolbar popup.
// Builds into any element or shadow root, so YouTube's styles can't reach it.

const DAILIES_CSS = `
:host, .dly { --paper:#f7f8f5; --ink:#1a2124; --muted:#5b6669; --line:#d3d8d4; --accent:#a9531b; --accent-ink:#fff;
  --ok:#2e7d4f; --bad:#b3261e; --strip:#5a3720; }
@media (prefers-color-scheme: dark) { :host, .dly { --paper:#232a2e; --ink:#e7ebe8; --muted:#95a19f; --line:#334045;
  --accent:#e08a45; --accent-ink:#1d1208; --ok:#6cc58e; --bad:#f07a6a; } }
.dly { font: 13px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--ink); background: var(--paper);
  width: 280px; box-sizing: border-box; padding: 12px 14px 14px; border-top: 6px solid var(--strip);
  background-image: none; }
.dly * { box-sizing: border-box; font: inherit; }
.dly h1 { font-size: 14px; font-weight: 700; margin: 0 0 2px; }
.dly .what { color: var(--muted); margin: 0 0 10px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; }
.dly label[hidden] { display: none; }
.dly label { display: block; font-size: 12px; color: var(--muted); margin: 0 0 8px; }
.dly select { display: block; width: 100%; margin-top: 3px; padding: 6px 8px; color: var(--ink); background: transparent;
  border: 1px solid var(--line); border-radius: 4px; }
.dly select option { color: #1a2124; background: #fff; }
.dly button { cursor: pointer; border: 1px solid transparent; border-radius: 4px; padding: 7px 12px; font-weight: 600; }
.dly .go { width: 100%; background: var(--accent); color: var(--accent-ink); margin-top: 4px; }
.dly .go:disabled { opacity: .6; cursor: default; }
.dly .ghost { background: transparent; color: var(--ink); border-color: var(--line); padding: 4px 9px; font-size: 12px; }
.dly .msg { margin: 10px 0 0; min-height: 1em; }
.dly .msg.ok { color: var(--ok); } .dly .msg.bad { color: var(--bad); }
.dly .msg a { color: inherit; }
.dly button:focus-visible, .dly select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
`;

function dailiesSend(type, extra = {}) {
  return new Promise(resolve => chrome.runtime.sendMessage({ type, ...extra }, r =>
    resolve(r || { ok: false, error: chrome.runtime.lastError?.message || 'No answer from the extension.' })));
}

function dailiesKind(url) {
  if (/[?&]v=|youtu\.be\/|\/shorts\/|\/live\//.test(url)) return 'video';
  if (/[?&]list=/.test(url)) return 'playlist';
  if (/youtube\.com\/(@|channel\/|c\/|user\/)/.test(url)) return 'channel';
  return '';
}

// Everything a YouTube page offers, in one list: the video, its whole channel and its playlist.
// `channel` = {url, name} when the page shows who made the video (read from the page by content.js).
function dailiesPageChoices(url, title, channel) {
  const out = [];
  let u;
  try { u = new URL(url); } catch (_) { return out; }
  const kind = dailiesKind(url);
  const list = u.searchParams.get('list');
  const v = u.searchParams.get('v') || (u.pathname.match(/^\/(?:shorts|live)\/([\w-]+)/) || [])[1]
    || (u.hostname === 'youtu.be' ? u.pathname.slice(1) : '');
  if (kind === 'video' && v) {
    const href = /^\/shorts\//.test(u.pathname) ? `https://www.youtube.com/shorts/${v}` : `https://www.youtube.com/watch?v=${v}`;
    out.push({ url: href, title, label: `This video${title ? ': ' + title : ''}` });
  }
  if (kind === 'channel') {
    const ch = u.pathname.match(/^\/(@[^/]+|channel\/[\w-]+|c\/[^/]+|user\/[^/]+)/);
    if (ch) channel = { url: `https://www.youtube.com/${ch[1]}`, name: title || decodeURIComponent(ch[1]) };
  }
  if (channel) out.push({ url: channel.url, title: channel.name, label: `Whole channel: ${channel.name}` });
  if (list && !/^(RD|WL$|LL$)/.test(list)) {
    const name = kind === 'playlist' ? title : '';
    out.push({ url: `https://www.youtube.com/playlist?list=${list}`, title: name, // blank: Dailies names it from YouTube
      label: `Playlist${name ? ': ' + name : ''}` });
  }
  // the page itself first: a playlist page opens on the playlist, a channel page on the channel
  const first = out.findIndex(c => dailiesKind(c.url) === kind);
  if (first > 0) out.unshift(...out.splice(first, 1));
  return out;
}

// Renders the form into `root`. `page` = {url, title} or {choices: [{url, title, label}], title}.
// With choices (a tile on the home feed) the user picks what to send: the video, its channel or its playlist.
async function dailiesForm(root, page) {
  const el = (tag, props = {}, kids = []) => {
    const n = Object.assign(document.createElement(tag), props);
    n.append(...kids);
    return n;
  };
  if (!root.querySelector('style.dly-css')) root.append(el('style', { className: 'dly-css', textContent: DAILIES_CSS }));
  const box = el('div', { className: 'dly' });
  root.append(box);
  const choices = (page.choices || [{ url: page.url, title: page.title }])
    .map(c => ({ ...c, kind: dailiesKind(c.url) })).filter(c => c.kind);
  const msg = el('p', { className: 'msg', role: 'status' });
  const head = el('h1');
  const what = el('p', { className: 'what' });
  box.append(head, what);
  const heading = c => {
    head.textContent = c.kind === 'video' ? 'Download with Dailies' : 'Add to Dailies';
    what.textContent = c.title || c.label || c.url;
  };
  heading(choices[0] || { kind: 'video', title: page.title, url: page.url });

  if (!choices.length) {
    box.append(el('p', { className: 'what', textContent: 'Open a YouTube video, playlist or channel to send it to Dailies.' }));
    return box;
  }
  const info = await dailiesSend('info');
  if (!info.ok) {
    msg.className = 'msg bad';
    msg.textContent = info.error;
    const open = el('button', { className: 'ghost', textContent: 'Extension settings' });
    open.onclick = () => chrome.runtime.sendMessage({ type: 'options' });
    box.append(msg, open);
    return box;
  }
  const saved = await chrome.storage.local.get(['profile_id', 'folder_id']);
  const opt = (value, text, sel) => el('option', { value, textContent: text, selected: String(value) === String(sel ?? '') });
  const target = el('select', {}, choices.map((c, i) => opt(i, c.label || c.title || c.url, 0)));
  const profile = el('select', {}, info.data.profiles.map(p => opt(p.id, p.name, saved.profile_id)));
  const folder = el('select', {}, [opt('', 'Automatic', saved.folder_id),
    ...info.data.folders.map(f => opt(f.id, f.name, saved.folder_id))]);
  // channels and playlists: which of their existing videos to download (new uploads always download)
  const ranges = info.data.since || [{ key: 'new', label: 'Only new uploads' }, { key: 'all', label: 'Everything' }];
  const since = el('select', {}, ranges.map(r => opt(r.key, r.label, 'new')));
  const sinceRow = el('label', { textContent: 'Download' }, [since]);
  let sinceTouched = false;
  since.onchange = () => { sinceTouched = true; };
  const go = el('button', { className: 'go' });
  const pick = () => choices[Number(target.value)] || choices[0];
  const label = () => {
    const c = pick();
    heading(c);
    sinceRow.hidden = c.kind === 'video';
    if (!sinceTouched) since.value = c.kind === 'playlist' ? 'all' : 'new';
    go.textContent = c.kind === 'video' ? 'Download' : c.kind === 'channel' ? 'Add channel as a series' : 'Add playlist';
  };
  target.onchange = label;
  label();
  if (choices.length > 1) box.append(el('label', { textContent: 'What' }, [target]));
  box.append(sinceRow, el('label', { textContent: 'Format' }, [profile]), el('label', { textContent: 'Save to' }, [folder]), go, msg);
  if (!info.data.youtube.signed_in) {
    msg.textContent = 'Dailies isn\'t using your Chrome sign-in yet. It will sync when you download.';
  }
  go.onclick = async () => {
    go.disabled = true;
    msg.className = 'msg';
    msg.textContent = 'Sending…';
    const ids = { profile_id: Number(profile.value), folder_id: folder.value ? Number(folder.value) : null };
    chrome.storage.local.set(ids);
    const c = pick();
    const r = await dailiesSend('download', { url: c.url, title: c.title, ...ids,
      ...(c.kind === 'video' ? {} : { since: since.value }) });
    msg.className = 'msg ' + (r.ok ? 'ok' : 'bad');
    msg.textContent = r.ok ? r.data.message : r.error;
    go.disabled = false;
  };
  return box;
}
