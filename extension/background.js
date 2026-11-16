// Dailies extension service worker: talks to the Dailies server and keeps its YouTube session fresh.

// Cookies that make up a YouTube sign-in. When Chrome rotates one of them, Dailies gets the new set.
const AUTH = new Set(['LOGIN_INFO', 'SID', 'HSID', 'SSID', 'APISID', 'SAPISID', '__Secure-1PSID', '__Secure-3PSID',
  '__Secure-1PAPISID', '__Secure-3PAPISID', '__Secure-1PSIDTS', '__Secure-3PSIDTS']);

async function config() {
  return chrome.storage.local.get(['server', 'key']);
}

async function api(path, body) {
  const { server, key } = await config();
  if (!server || !key) throw new Error('Connect the extension to Dailies first (extension settings).');
  let r;
  try {
    r = await fetch(server + path, {
      method: body ? 'POST' : 'GET',
      headers: { Authorization: 'Bearer ' + key, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (_) {
    throw new Error(`Can't reach Dailies at ${server}. Is GORT on and are you on your home network?`);
  }
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (r.status === 401) throw new Error('Dailies rejected the key. Create a new one on Dailies\' YouTube page.');
  if (!r.ok) throw new Error(data.message || data.error || `Dailies answered ${r.status}.`);
  return data;
}

async function syncCookies(force = false) {
  const cookies = await chrome.cookies.getAll({ domain: 'youtube.com' });
  const sig = cookies.filter(c => AUTH.has(c.name)).map(c => `${c.domain}|${c.name}=${c.value}`).sort().join(';');
  const { lastSig } = await chrome.storage.local.get('lastSig');
  if (!force && sig === lastSig) return { ok: true, skipped: true };
  let res;
  try {
    res = await api('/api/ext/cookies', {
      cookies: cookies.map(c => ({
        domain: c.domain, name: c.name, value: c.value, path: c.path, secure: c.secure,
        httpOnly: c.httpOnly, hostOnly: c.hostOnly, expirationDate: c.expirationDate,
      })),
    });
  } catch (e) {
    await chrome.storage.local.set({ lastSync: { at: Date.now(), ok: false, message: e.message } });
    throw e;
  }
  await chrome.storage.local.set({
    lastSig: res.ok ? sig : '',
    lastSync: { at: Date.now(), ok: !!res.ok, message: res.message || '' },
  });
  return res;
}

function isYouTube(url) {
  return /^https?:\/\/(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\//.test(url || '');
}

async function download({ url, title, profile_id, folder_id, since }) {
  await syncCookies(false).catch(() => {}); // make sure Dailies has the current session first
  return api('/api/ext/download', { url, title, profile_id, folder_id, since });
}

async function flash(text, color) {
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
  setTimeout(() => chrome.action.setBadgeText({ text: '' }), 4000);
}

// ---- events -------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'dailies-download', title: 'Download with Dailies', contexts: ['link', 'page', 'video'],
    documentUrlPatterns: ['https://*.youtube.com/*'],
    targetUrlPatterns: ['https://*.youtube.com/*', 'https://youtu.be/*'],
  });
  chrome.alarms.create('sync', { periodInMinutes: 30 });
  chrome.runtime.openOptionsPage();
});

chrome.runtime.onStartup.addListener(() => syncCookies(false).catch(() => {}));

chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (removed || !cookie.domain.endsWith('youtube.com') || !AUTH.has(cookie.name)) return;
  chrome.alarms.create('sync-soon', { delayInMinutes: 0.5 }); // coalesce bursts of changes
});

// every 30 min resend even if nothing changed, in case Dailies' copy was replaced or lost
chrome.alarms.onAlarm.addListener(a => syncCookies(a.name === 'sync').catch(() => {}));

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = [info.linkUrl, info.srcUrl, info.pageUrl].find(isYouTube);
  if (!url) return;
  const { profile_id, folder_id } = await chrome.storage.local.get(['profile_id', 'folder_id']);
  try {
    await download({ url, title: url === info.pageUrl ? (tab?.title || '').replace(/ - YouTube$/, '') : '',
      profile_id, folder_id });
    flash('✓', '#2e7d4f');
  } catch (_) {
    flash('!', '#b3261e');
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  const run = {
    info: () => api('/api/ext/info'),
    sync: () => syncCookies(true),
    download: () => download(msg),
    options: async () => chrome.runtime.openOptionsPage(),
  }[msg.type];
  if (!run) return false;
  run().then(data => reply({ ok: true, data }), e => reply({ ok: false, error: e.message }));
  return true; // reply asynchronously
});
