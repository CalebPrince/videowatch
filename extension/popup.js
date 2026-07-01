const DEFAULT_SERVER = 'https://videowatch.duckdns.org';

async function load() {
  const { vw_server, vw_token } = await chrome.storage.local.get(['vw_server', 'vw_token']);
  const server = vw_server || DEFAULT_SERVER;
  document.getElementById('server-url').value = server;
  document.getElementById('api-token').value = vw_token || '';
  document.getElementById('dashboard-link').href = server;

  // Pre-fill the current tab's URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.url) {
    try {
      const u = new URL(tab.url);
      document.getElementById('site-url').value = u.origin + u.pathname.replace(/\/$/, '');
      document.getElementById('site-name').value = tab.title || '';
    } catch(e) {}
  }
}

function showStatus(msg, type) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'status ' + type;
}

document.getElementById('btn-save-creds').addEventListener('click', async () => {
  const server = document.getElementById('server-url').value.trim().replace(/\/$/, '');
  const token  = document.getElementById('api-token').value.trim();
  await chrome.storage.local.set({ vw_server: server, vw_token: token });
  document.getElementById('dashboard-link').href = server;
  showStatus('Credentials saved.', 'ok');
});

document.getElementById('btn-save-server').addEventListener('click', async () => {
  const server = document.getElementById('server-url').value.trim().replace(/\/$/, '');
  await chrome.storage.local.set({ vw_server: server });
  document.getElementById('dashboard-link').href = server;
  showStatus('Server URL saved.', 'ok');
});

document.getElementById('btn-add').addEventListener('click', async () => {
  const btn = document.getElementById('btn-add');
  const { vw_server, vw_token } = await chrome.storage.local.get(['vw_server', 'vw_token']);
  const server = (vw_server || DEFAULT_SERVER).replace(/\/$/, '');
  const token  = vw_token || '';

  if (!token) { showStatus('Please save your API token first.', 'err'); return; }

  const url      = document.getElementById('site-url').value.trim();
  const name     = document.getElementById('site-name').value.trim();
  const interval = parseInt(document.getElementById('site-interval').value, 10);

  if (!url) { showStatus('No URL detected.', 'err'); return; }

  btn.disabled = true;
  btn.textContent = 'Adding…';

  try {
    const res = await fetch(`${server}/api/public/sites`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({ url, name, scan_interval: interval }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      showStatus(`✓ Added! First scan will start soon.`, 'ok');
    } else if (res.status === 409) {
      showStatus('This site is already in your monitor.', 'err');
    } else {
      showStatus(data.detail || `Error ${res.status}`, 'err');
    }
  } catch(e) {
    showStatus('Could not reach server: ' + e.message, 'err');
  }

  btn.disabled = false;
  btn.textContent = 'Add to VideoWatch';
});

load();
