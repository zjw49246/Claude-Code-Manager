const serverUrlEl = document.getElementById('serverUrl');
const authTokenEl = document.getElementById('authToken');
const saveBtn = document.getElementById('save');
const testBtn = document.getElementById('test');
const statusEl = document.getElementById('status');

chrome.storage.sync.get(
  { serverUrl: 'https://xiaoyu.claude-code-manager.com', authToken: '' },
  (cfg) => {
    serverUrlEl.value = cfg.serverUrl;
    authTokenEl.value = cfg.authToken;
  },
);

function showStatus(msg, type) {
  statusEl.textContent = msg;
  statusEl.className = 'status ' + type;
  statusEl.style.display = 'block';
}

saveBtn.addEventListener('click', () => {
  const serverUrl = serverUrlEl.value.trim().replace(/\/+$/, '');
  const authToken = authTokenEl.value.trim();

  if (!serverUrl) {
    showStatus('Server URL is required.', 'error');
    return;
  }
  if (!authToken) {
    showStatus('Auth Token is required.', 'error');
    return;
  }

  chrome.storage.sync.set({ serverUrl, authToken }, () => {
    showStatus('Settings saved.', 'success');
  });
});

testBtn.addEventListener('click', async () => {
  const serverUrl = serverUrlEl.value.trim().replace(/\/+$/, '');
  const authToken = authTokenEl.value.trim();

  if (!serverUrl || !authToken) {
    showStatus('Please fill in both fields first.', 'error');
    return;
  }

  testBtn.disabled = true;
  testBtn.textContent = 'Testing...';

  try {
    const res = await fetch(`${serverUrl}/api/system/stats`, {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (res.ok) {
      showStatus('Connected successfully!', 'success');
    } else if (res.status === 401) {
      showStatus('Authentication failed. Check your auth token.', 'error');
    } else {
      showStatus(`Server responded with status ${res.status}.`, 'error');
    }
  } catch (err) {
    showStatus(`Cannot reach server: ${err.message}`, 'error');
  } finally {
    testBtn.disabled = false;
    testBtn.textContent = 'Test Connection';
  }
});
