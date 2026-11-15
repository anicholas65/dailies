const $ = id => document.getElementById(id);
const status = (text, cls = '') => { $('status').textContent = text; $('status').className = cls; };

chrome.storage.local.get(['server', 'key']).then(({ server, key }) => {
  if (server) $('server').value = server;
  if (key) $('key').value = key;
  if (server && key) status('Connected settings saved. Press Save and connect to test again.');
});

function send(type) {
  return new Promise(resolve => chrome.runtime.sendMessage({ type }, r => resolve(r || { ok: false, error: 'No answer.' })));
}

$('f').addEventListener('submit', async e => {
  e.preventDefault();
  let server = $('server').value.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//.test(server)) server = 'http://' + server;
  let origin;
  try { origin = new URL(server).origin; } catch (_) { return status('That address doesn\'t look right.', 'bad'); }
  // Chrome asks you once to let the extension talk to this address.
  const granted = await chrome.permissions.request({ origins: [origin + '/*'] });
  if (!granted) return status('Chrome needs your permission to reach Dailies at ' + origin + '.', 'bad');
  $('save').disabled = true;
  await chrome.storage.local.set({ server: origin, key: $('key').value.trim(), lastSig: '' });
  status('Connecting…');
  const info = await send('info');
  if (!info.ok) {
    $('save').disabled = false;
    return status(info.error, 'bad');
  }
  const sync = await send('sync');
  $('save').disabled = false;
  if (!sync.ok) return status(`Connected to Dailies ${info.data.version}, but sending your YouTube sign-in failed:\n${sync.error}`, 'bad');
  if (!sync.data.ok) return status(`Connected to Dailies ${info.data.version}.\n${sync.data.message}`, 'bad');
  status(`Connected to Dailies ${info.data.version}. Dailies now downloads with your Chrome's YouTube sign-in.\n` +
    'Open any YouTube video and use the Download button at the bottom right.', 'ok');
});
