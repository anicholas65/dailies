const syncLine = document.getElementById('sync');

function ago(t) {
  const m = Math.round((Date.now() - t) / 60000);
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}

async function showSync() {
  const { lastSync } = await chrome.storage.local.get('lastSync');
  syncLine.className = lastSync && !lastSync.ok ? 'bad' : '';
  syncLine.textContent = !lastSync ? 'YouTube sign-in not sent to Dailies yet.'
    : lastSync.ok ? `YouTube sign-in sent to Dailies ${ago(lastSync.at)}.`
      : lastSync.message;
}

document.getElementById('sync-now').onclick = async () => {
  syncLine.textContent = 'Syncing…';
  const r = await dailiesSend('sync');
  if (!r.ok) {
    syncLine.className = 'bad';
    syncLine.textContent = r.error;
  } else {
    await showSync();
  }
};
document.getElementById('settings').onclick = () => chrome.runtime.openOptionsPage();

(async () => {
  showSync();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  // the tabs permission isn't requested: url/title are only visible on YouTube tabs (host permission)
  const url = tab?.url || '';
  const title = (tab?.title || '').replace(/^\(\d+\)\s*/, '').replace(/ - YouTube$/, '');
  // the YouTube page knows the video's channel too; fall back to what the address alone gives
  const fromPage = tab?.id && await chrome.tabs.sendMessage(tab.id, { type: 'pageChoices' }).catch(() => null);
  await dailiesForm(document.getElementById('form'),
    fromPage?.choices?.length ? fromPage : { url, title, choices: dailiesPageChoices(url, title, null) });
})();
