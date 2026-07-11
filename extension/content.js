(function () {
  if (document.getElementById('ccm-quick-capture-host')) return;

  const CAMERA_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>';
  const PLUS_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
  const X_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  const LOADER_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="ccm-spin"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>';

  // --- Shadow DOM host ---
  const host = document.createElement('div');
  host.id = 'ccm-quick-capture-host';
  host.style.cssText = 'position:fixed;z-index:2147483647;top:0;left:0;width:0;height:0;pointer-events:none;';
  document.body.appendChild(host);
  const shadow = host.attachShadow({ mode: 'closed' });

  // --- Styles ---
  const style = document.createElement('style');
  style.textContent = `
    @keyframes ccm-spin { to { transform: rotate(360deg); } }
    .ccm-spin { animation: ccm-spin 1s linear infinite; }

    .ccm-fab {
      position: fixed; bottom: 24px; right: 24px;
      width: 48px; height: 48px; border-radius: 50%;
      background: #4f46e5; color: #fff; border: none; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 4px 14px rgba(0,0,0,.35);
      transition: background .15s, transform .15s;
      pointer-events: auto; z-index: 2147483647;
    }
    .ccm-fab:hover { background: #6366f1; transform: scale(1.08); }
    .ccm-fab:disabled { opacity: .5; cursor: default; transform: none; }

    .ccm-overlay {
      position: fixed; inset: 0;
      background: rgba(0,0,0,.55); display: flex;
      align-items: center; justify-content: center;
      pointer-events: auto; z-index: 2147483647;
    }

    .ccm-modal {
      background: #1f2937; border-radius: 12px;
      width: 420px; max-width: calc(100vw - 32px);
      padding: 20px; box-shadow: 0 20px 60px rgba(0,0,0,.5);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      color: #e5e7eb;
    }

    .ccm-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .ccm-title { font-size: 15px; font-weight: 600; color: #f3f4f6; margin: 0; }
    .ccm-close { background: none; border: none; color: #9ca3af; cursor: pointer; padding: 4px; display: flex; }
    .ccm-close:hover { color: #fff; }

    .ccm-screenshots { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
    .ccm-thumb-wrap { position: relative; }
    .ccm-thumb-wrap:hover .ccm-thumb-del { opacity: 1; }
    .ccm-thumb { height: 80px; border-radius: 6px; border: 1px solid #374151; object-fit: cover; display: block; }
    .ccm-thumb-del {
      position: absolute; top: -5px; right: -5px;
      background: #dc2626; border: none; border-radius: 50%; color: #fff;
      width: 18px; height: 18px; display: flex; align-items: center; justify-content: center;
      cursor: pointer; opacity: 0; transition: opacity .15s;
    }
    .ccm-add-btn {
      height: 80px; width: 80px; border: 2px dashed #4b5563; border-radius: 6px;
      background: none; color: #9ca3af; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: border-color .15s, color .15s;
    }
    .ccm-add-btn:hover { border-color: #9ca3af; color: #d1d5db; }

    .ccm-textarea {
      width: 100%; box-sizing: border-box;
      background: #111827; border: 1px solid #374151; border-radius: 8px;
      padding: 10px 12px; font-size: 14px; color: #e5e7eb; resize: none;
      font-family: inherit; margin-bottom: 12px;
    }
    .ccm-textarea::placeholder { color: #6b7280; }
    .ccm-textarea:focus { outline: none; border-color: #6366f1; }

    .ccm-select {
      width: 100%; box-sizing: border-box;
      background: #111827; border: 1px solid #374151; border-radius: 8px;
      padding: 8px 12px; font-size: 14px; color: #e5e7eb;
      font-family: inherit; margin-bottom: 12px; cursor: pointer;
    }
    .ccm-select:focus { outline: none; border-color: #6366f1; }
    .ccm-select option { background: #1f2937; color: #e5e7eb; }

    .ccm-error { font-size: 12px; color: #f87171; margin-bottom: 10px; }

    .ccm-create-btn {
      width: 100%; padding: 10px; border: none; border-radius: 8px;
      background: #4f46e5; color: #fff; font-size: 14px; font-weight: 500;
      cursor: pointer; transition: background .15s; font-family: inherit;
    }
    .ccm-create-btn:hover { background: #6366f1; }
    .ccm-create-btn:disabled { opacity: .5; cursor: not-allowed; }

    .ccm-config-hint {
      text-align: center; padding: 24px 16px; font-size: 13px; color: #9ca3af;
    }
    .ccm-config-link {
      color: #818cf8; cursor: pointer; text-decoration: underline; background: none; border: none;
      font-size: 13px; font-family: inherit;
    }
  `;
  shadow.appendChild(style);

  // --- State ---
  let screenshots = []; // { dataUrl, blob }
  let projects = [];
  let capturing = false;
  let modalEl = null;

  // --- FAB ---
  const fab = document.createElement('button');
  fab.className = 'ccm-fab';
  fab.title = 'CCM Quick Capture';
  fab.innerHTML = CAMERA_SVG;
  fab.addEventListener('click', () => doCapture(true));
  shadow.appendChild(fab);

  // --- Helpers ---
  async function getConfig() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(
        { serverUrl: 'https://xiaoyu.claude-code-manager.com', authToken: '' },
        resolve,
      );
    });
  }

  function dataUrlToBlob(dataUrl) {
    const [header, b64] = dataUrl.split(',');
    const mime = header.match(/:(.*?);/)[1];
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: mime });
  }

  async function fetchProjects(config) {
    const res = await fetch(`${config.serverUrl}/api/projects`, {
      headers: { Authorization: `Bearer ${config.authToken}` },
    });
    if (!res.ok) throw new Error('Failed to fetch projects');
    return res.json();
  }

  async function uploadFile(config, blob, filename) {
    const form = new FormData();
    form.append('files', blob, filename);
    const res = await fetch(`${config.serverUrl}/api/uploads`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${config.authToken}` },
      body: form,
    });
    if (!res.ok) throw new Error('Upload failed');
    return res.json();
  }

  async function createTask(config, data) {
    const res = await fetch(`${config.serverUrl}/api/tasks`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${config.authToken}`,
      },
      body: JSON.stringify(data),
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || 'Failed to create task');
    }
    return res.json();
  }

  async function fetchSystemConfig(config) {
    const res = await fetch(`${config.serverUrl}/api/system/config`, {
      headers: { Authorization: `Bearer ${config.authToken}` },
    });
    if (!res.ok) return null;
    return res.json();
  }

  // --- Capture ---
  async function doCapture(openModal) {
    if (capturing) return;
    capturing = true;
    fab.disabled = true;
    fab.innerHTML = LOADER_SVG;

    try {
      const resp = await chrome.runtime.sendMessage({ type: 'capture' });
      if (resp.error) throw new Error(resp.error);

      const blob = dataUrlToBlob(resp.dataUrl);
      screenshots.push({ dataUrl: resp.dataUrl, blob });

      if (openModal && !modalEl) {
        showModal();
      } else if (modalEl) {
        renderScreenshots();
      }
    } catch (err) {
      if (openModal && !modalEl) {
        showModal(err.message);
      }
    } finally {
      capturing = false;
      fab.disabled = false;
      fab.innerHTML = CAMERA_SVG;
    }
  }

  // --- Modal ---
  function showModal(errorMsg) {
    const config$ = getConfig();

    const overlay = document.createElement('div');
    overlay.className = 'ccm-overlay';
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeModal();
    });

    const modal = document.createElement('div');
    modal.className = 'ccm-modal';
    overlay.appendChild(modal);
    shadow.appendChild(overlay);
    modalEl = overlay;

    config$.then(async (config) => {
      if (!config.authToken) {
        modal.innerHTML = `
          <div class="ccm-header">
            <h3 class="ccm-title">Quick Capture</h3>
            <button class="ccm-close" id="ccm-close">${X_SVG}</button>
          </div>
          <div class="ccm-config-hint">
            Please configure CCM server URL and auth token first.<br/><br/>
            <button class="ccm-config-link" id="ccm-open-options">Open Extension Settings</button>
          </div>
        `;
        modal.querySelector('#ccm-close').addEventListener('click', closeModal);
        modal.querySelector('#ccm-open-options').addEventListener('click', () => {
          chrome.runtime.sendMessage({ type: 'openOptions' });
        });
        return;
      }

      try {
        const [projectList, sysConfig] = await Promise.all([
          fetchProjects(config),
          fetchSystemConfig(config),
        ]);
        projects = projectList.filter((p) => p.show_in_selector !== false);

        renderFullModal(modal, config, sysConfig, errorMsg);
      } catch (err) {
        modal.innerHTML = `
          <div class="ccm-header">
            <h3 class="ccm-title">Quick Capture</h3>
            <button class="ccm-close" id="ccm-close">${X_SVG}</button>
          </div>
          <div class="ccm-error">Cannot connect to CCM: ${err.message}</div>
          <div class="ccm-config-hint">
            <button class="ccm-config-link" id="ccm-open-options">Check Settings</button>
          </div>
        `;
        modal.querySelector('#ccm-close').addEventListener('click', closeModal);
        modal.querySelector('#ccm-open-options').addEventListener('click', () => {
          chrome.runtime.sendMessage({ type: 'openOptions' });
        });
      }
    });
  }

  function renderFullModal(modal, config, sysConfig, errorMsg) {
    modal.innerHTML = `
      <div class="ccm-header">
        <h3 class="ccm-title">Quick Capture</h3>
        <button class="ccm-close" id="ccm-close">${X_SVG}</button>
      </div>
      <div class="ccm-screenshots" id="ccm-screenshots"></div>
      <textarea class="ccm-textarea" id="ccm-desc" rows="3" placeholder="What do you want Claude to do?" autofocus></textarea>
      <select class="ccm-select" id="ccm-project">
        <option value="">Select project...</option>
      </select>
      <div class="ccm-error" id="ccm-error" style="display:none"></div>
      <button class="ccm-create-btn" id="ccm-create" disabled>Create Task</button>
    `;

    const descEl = modal.querySelector('#ccm-desc');
    const projectEl = modal.querySelector('#ccm-project');
    const createBtn = modal.querySelector('#ccm-create');
    const errorEl = modal.querySelector('#ccm-error');

    modal.querySelector('#ccm-close').addEventListener('click', closeModal);

    // populate projects
    projects.forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      projectEl.appendChild(opt);
    });

    // validation
    function updateCanSubmit() {
      createBtn.disabled = !descEl.value.trim() || !projectEl.value;
    }
    descEl.addEventListener('input', updateCanSubmit);
    projectEl.addEventListener('change', updateCanSubmit);

    // render screenshots
    renderScreenshots();

    // show initial error
    if (errorMsg) {
      errorEl.textContent = errorMsg;
      errorEl.style.display = 'block';
    }

    // create handler
    createBtn.addEventListener('click', async () => {
      createBtn.disabled = true;
      createBtn.textContent = 'Creating...';
      errorEl.style.display = 'none';

      try {
        let filePaths = [];
        let attachments = [];

        if (screenshots.length > 0) {
          const uploadResults = [];
          for (let i = 0; i < screenshots.length; i++) {
            const results = await uploadFile(
              config,
              screenshots[i].blob,
              `capture-${Date.now()}-${i}.png`,
            );
            uploadResults.push(...results);
          }
          filePaths = uploadResults.map((r) => r.path);
          attachments = uploadResults.map((r) => ({
            url: r.url,
            name: r.filename || 'capture.png',
            is_image: r.is_image,
          }));
        }

        const defaultModel = sysConfig?.default_model || 'claude-opus-4-6';

        const taskData = {
          description: descEl.value.trim(),
          project_id: parseInt(projectEl.value),
          priority: 0,
          mode: 'auto',
          provider: 'claude',
          model: defaultModel,
        };
        if (filePaths.length > 0) taskData.file_paths = filePaths;
        if (attachments.length > 0) taskData.attachments = attachments;

        const task = await createTask(config, taskData);
        closeModal();
        window.open(`${config.serverUrl}#/tasks/chat/${task.id}`, '_blank');
      } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'block';
        createBtn.disabled = false;
        createBtn.textContent = 'Create Task';
      }
    });

    // focus textarea
    setTimeout(() => descEl.focus(), 50);
  }

  function renderScreenshots() {
    const container = modalEl?.querySelector('#ccm-screenshots');
    if (!container) return;
    container.innerHTML = '';

    screenshots.forEach((s, i) => {
      const wrap = document.createElement('div');
      wrap.className = 'ccm-thumb-wrap';

      const img = document.createElement('img');
      img.className = 'ccm-thumb';
      img.src = s.dataUrl;
      wrap.appendChild(img);

      const del = document.createElement('button');
      del.className = 'ccm-thumb-del';
      del.innerHTML = X_SVG;
      del.addEventListener('click', () => {
        screenshots.splice(i, 1);
        renderScreenshots();
      });
      wrap.appendChild(del);

      container.appendChild(wrap);
    });

    const addBtn = document.createElement('button');
    addBtn.className = 'ccm-add-btn';
    addBtn.innerHTML = PLUS_SVG;
    addBtn.title = 'Add another screenshot';
    addBtn.addEventListener('click', () => doCapture(false));
    container.appendChild(addBtn);
  }

  function closeModal() {
    if (modalEl) {
      modalEl.remove();
      modalEl = null;
    }
    screenshots = [];
    projects = [];
  }
})();
