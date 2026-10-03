// LLMProxy Modern Control Center Logic
window.loadStats = () => {}; // Compatibility stub for cached browser sessions

let currentTab = 'models';
let currentSubTab = 'traces';
let cachedKeys = [];
let cachedModels = [];
let cachedTraces = [];
let activeTriageModelValue = null;
let modelSortField = 'base_score';
let modelSortAsc = false;
let currentAnalyticsTimeframe = '7d';

// ============================================================================
// Core Initialization & Lazy Fetching Tab Management
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
  // Initial load for default tab
  loadModels();
  loadTriageSettings();

  // Lazy polling: only refresh the active tab in background
  setInterval(() => {
    if (currentTab === 'models') {
      loadModels();
    } else if (currentTab === 'traces' && currentSubTab === 'traces') {
      loadTraces();
    }
  }, 3000);
});

let terminalEventSource = null;

function switchTab(tabName) {
  currentTab = tabName;
  document.querySelectorAll('.nav-tab').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(pane => pane.classList.remove('active'));

  const btn = document.getElementById(`tab-btn-${tabName}`);
  const pane = document.getElementById(`view-${tabName}`);
  if (btn && pane) {
    btn.classList.add('active');
    pane.classList.add('active');
  }

  // Disconnect live terminal stream if moving away from traces tab
  if (tabName !== 'traces') {
    disconnectTerminalLogs();
  }

  // Lazy Fetching: Query data only when user actually navigates to that tab
  if (tabName === 'models') {
    loadModels();
    loadTriageSettings();
  } else if (tabName === 'traces') {
    switchSubTab(currentSubTab);
  } else if (tabName === 'analytics') {
    loadAnalytics(currentAnalyticsTimeframe);
  } else if (tabName === 'keys') {
    loadKeys();
  }
}

function switchSubTab(subTabName) {
  currentSubTab = subTabName;
  document.querySelectorAll('.subnav-tab').forEach(btn => btn.classList.remove('active'));
  const btn = document.getElementById(`subtab-btn-${subTabName}`);
  if (btn) btn.classList.add('active');

  const traceView = document.getElementById('subview-traces');
  const auditView = document.getElementById('subview-audit');
  const termView = document.getElementById('subview-terminal');

  if (traceView) traceView.style.display = subTabName === 'traces' ? 'block' : 'none';
  if (auditView) auditView.style.display = subTabName === 'audit' ? 'block' : 'none';
  if (termView) termView.style.display = subTabName === 'terminal' ? 'block' : 'none';

  if (subTabName === 'traces') {
    disconnectTerminalLogs();
    loadTraces();
  } else if (subTabName === 'audit') {
    disconnectTerminalLogs();
    loadAudit();
  } else if (subTabName === 'terminal') {
    connectTerminalLogs();
  }
}

function disconnectTerminalLogs() {
  if (terminalEventSource) {
    terminalEventSource.close();
    terminalEventSource = null;
  }
}

function connectTerminalLogs() {
  if (terminalEventSource) return;
  const consoleEl = document.getElementById('terminalConsole');
  if (!consoleEl) return;

  terminalEventSource = new EventSource('/api/admin/logs/stream');
  terminalEventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data && data.line) {
        appendTerminalLine(data.line);
      }
    } catch (e) {}
  };
}

function formatLogLine(rawLine) {
  let line = rawLine
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  if (line.includes('| INFO')) {
    line = line.replace(/\|\s*INFO\s*\|/, '| <span class="log-info">INFO</span>   |');
  } else if (line.includes('| WARNING')) {
    line = line.replace(/\|\s*WARNING\s*\|/, '| <span class="log-warn">WARNING</span>|');
  } else if (line.includes('| ERROR')) {
    line = line.replace(/\|\s*ERROR\s*\|/, '| <span class="log-error">ERROR</span>  |');
  } else if (line.includes('| DEBUG')) {
    line = line.replace(/\|\s*DEBUG\s*\|/, '| <span class="log-debug">DEBUG</span>  |');
  }
  return line;
}

function appendTerminalLine(rawLine) {
  const consoleEl = document.getElementById('terminalConsole');
  if (!consoleEl) return;

  const lineSpan = document.createElement('div');
  lineSpan.innerHTML = formatLogLine(rawLine);
  consoleEl.appendChild(lineSpan);

  // Keep max 1000 lines in DOM to prevent lag
  if (consoleEl.childNodes.length > 1000) {
    consoleEl.removeChild(consoleEl.firstChild);
  }

  const autoScroll = document.getElementById('terminalAutoScroll');
  if (autoScroll && autoScroll.checked) {
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

function clearTerminalLogs() {
  const consoleEl = document.getElementById('terminalConsole');
  if (consoleEl) consoleEl.innerHTML = '';
}

function showToast(message, type = 'success') {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 200);
  }, 3000);
}

function formatCompactNumber(num) {
  if (num === null || num === undefined || isNaN(num) || num === '') return '0';
  const val = Number(num);
  if (Math.abs(val) < 1000) return val.toLocaleString();
  const units = ['K', 'M', 'B', 'T'];
  let unitIdx = -1;
  let scaled = val;
  while (Math.abs(scaled) >= 1000 && unitIdx < units.length - 1) {
    scaled /= 1000;
    unitIdx++;
  }
  const formatted = scaled % 1 === 0 ? scaled.toFixed(0) : scaled.toFixed(1);
  return `${formatted}${units[unitIdx]}`;
}


// ============================================================================
// Tab 1: Models & Quota Management (RPM, TPM, RPD, TPD)
// ============================================================================

async function loadModels() {
  try {
    const res = await fetch('/api/admin/models');
    if (!res.ok) return;
    cachedModels = await res.json();

    const triageSel = document.getElementById('triageModelSelect');
    if (triageSel && cachedModels) {
      const targetVal = activeTriageModelValue || triageSel.value;
      const modelOptions = cachedModels.map(m =>
        `<option value="${m.platform}:${m.model_id}">${m.display_name || m.model_id}</option>`
      ).join('');
      triageSel.innerHTML = `<option value="auto:auto">Auto</option>${modelOptions}`;
      if (targetVal) triageSel.value = targetVal;
    }

    renderModelsTable();
  } catch (err) {
    console.error('Failed to load models:', err);
  }
}

function sortByField(field) {
  if (modelSortField === field) {
    modelSortAsc = !modelSortAsc;
  } else {
    modelSortField = field;
    modelSortAsc = (field === 'display_name' || field === 'platform');
  }
  renderModelsTable();
}

function renderModelsTable() {
  const tbody = document.getElementById('modelsTableBody');
  if (!tbody) return;

  if (!cachedModels || cachedModels.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="table-empty">No models configured in catalog. Click "+ Add New Model" to add one!</td></tr>';
    return;
  }

  ['display_name', 'platform', 'base_score'].forEach(f => {
    const iconEl = document.getElementById(`sort-icon-${f}`);
    if (iconEl) {
      if (modelSortField === f) {
        iconEl.textContent = modelSortAsc ? ' ↑' : ' ↓';
      } else {
        iconEl.textContent = '';
      }
    }
  });

  const sorted = [...cachedModels].sort((a, b) => {
    let valA = a[modelSortField];
    let valB = b[modelSortField];

    if (modelSortField === 'display_name') {
      valA = (a.display_name || a.model_id || '').toLowerCase();
      valB = (b.display_name || b.model_id || '').toLowerCase();
    } else if (modelSortField === 'platform') {
      valA = (a.platform || '').toLowerCase();
      valB = (b.platform || '').toLowerCase();
    } else if (typeof valA === 'string') {
      valA = (valA || '').toLowerCase();
      valB = (valB || '').toLowerCase();
    }

    if (valA < valB) return modelSortAsc ? -1 : 1;
    if (valA > valB) return modelSortAsc ? 1 : -1;
    return 0;
  });

  tbody.innerHTML = sorted.map((m) => {
    const origIdx = cachedModels.findIndex(item => item.platform === m.platform && item.model_id === m.model_id);

    // RPM & RPD Calculations
    const rpmUtil = m.rpm_limit ? Math.min(100, Math.round((m.current_rpm / m.rpm_limit) * 100)) : 0;
    const rpdUtil = m.rpd_limit ? Math.min(100, Math.round(((m.current_rpd || 0) / m.rpd_limit) * 100)) : 0;
    const rpmMeterClass = rpmUtil >= 90 ? 'crit' : rpmUtil >= 75 ? 'warn' : '';
    const rpdMeterClass = rpdUtil >= 90 ? 'crit' : rpdUtil >= 75 ? 'warn' : '';

    // TPM & TPD Calculations
    const tpmUtil = m.tpm_limit ? Math.min(100, Math.round(((m.current_tpm || 0) / m.tpm_limit) * 100)) : 0;
    const tpdUtil = m.tpd_limit ? Math.min(100, Math.round(((m.current_tpd || 0) / m.tpd_limit) * 100)) : 0;
    const tpmMeterClass = tpmUtil >= 90 ? 'crit' : tpmUtil >= 75 ? 'warn' : '';
    const tpdMeterClass = tpdUtil >= 90 ? 'crit' : tpdUtil >= 75 ? 'warn' : '';

    let statusCell = '';
    if (m.is_cooldown) {
      const normStr = m.cooldown_until ? (m.cooldown_until.includes('T') ? m.cooldown_until : m.cooldown_until.replace(' ', 'T') + 'Z') : '';
      const localExpiry = normStr ? new Date(normStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
      const remText = formatRemainingCooldown(m.cooldown_until);
      statusCell = `
        <div style="display: flex; flex-direction: column; gap: 3px; align-items: flex-start; max-width: 125px; white-space: nowrap;">
          <span class="badge badge-warning" style="font-size: 11px; padding: 2px 6px; white-space: nowrap; line-height: 1.2;" title="Quarantined until local time ${localExpiry}">Cooldown (${remText})</span>
          <button class="btn btn-warning btn-sm" style="padding: 1px 6px; font-size: 10px; line-height: 1.2; border-radius: 4px;" onclick="clearModelCooldown('${m.platform}', '${m.model_id}', '${m.shared_quota_group || ''}')">Reset</button>
        </div>
      `;
    } else if (m.enabled) {
      statusCell = '<span class="badge badge-success">Ready</span>';
    } else {
      statusCell = '<span class="badge badge-neutral">Disabled</span>';
    }

    return `
      <tr>
        <td>
          <div><strong>${m.display_name || m.model_id}</strong></div>
          <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;"><code>${m.model_id}</code></div>
        </td>
        <td><span class="badge badge-neutral">${m.platform.toUpperCase()}</span></td>
        <td>${m.shared_quota_group ? `<code>${m.shared_quota_group}</code>` : '<span style="color: var(--text-muted);">Per-Model</span>'}</td>
        <td><span class="badge badge-neutral" style="font-weight: 600;">${m.base_score} / 5</span></td>
        
        <!-- Requests Quota: RPM & RPD -->
        <td class="quota-cell">
          <div class="quota-line">
            <div class="quota-header-row">
              <span class="label">RPM: ${formatCompactNumber(m.current_rpm)}</span>
              <span class="limit">/ ${m.rpm_limit ? formatCompactNumber(m.rpm_limit) : '∞'} (${rpmUtil}%)</span>
            </div>
            <div class="meter-track">
              <div class="meter-fill ${rpmMeterClass}" style="width: ${rpmUtil}%"></div>
            </div>
          </div>
          <div class="quota-line" style="margin-top: 8px;">
            <div class="quota-header-row">
              <span class="label" style="font-weight: 500; color: var(--text-muted);">RPD: ${formatCompactNumber(m.current_rpd || 0)}</span>
              <span class="limit">/ ${m.rpd_limit ? formatCompactNumber(m.rpd_limit) : '∞'} (${rpdUtil}%)</span>
            </div>
            <div class="meter-track">
              <div class="meter-fill ${rpdMeterClass}" style="width: ${rpdUtil}%"></div>
            </div>
          </div>
        </td>

        <!-- Tokens Quota: TPM & TPD -->
        <td class="quota-cell">
          <div class="quota-line">
            <div class="quota-header-row">
              <span class="label">TPM: ${formatCompactNumber(m.current_tpm || 0)}</span>
              <span class="limit">/ ${m.tpm_limit ? formatCompactNumber(m.tpm_limit) : '∞'} (${tpmUtil}%)</span>
            </div>
            <div class="meter-track">
              <div class="meter-fill ${tpmMeterClass}" style="width: ${tpmUtil}%"></div>
            </div>
          </div>
          <div class="quota-line" style="margin-top: 8px;">
            <div class="quota-header-row">
              <span class="label" style="font-weight: 500; color: var(--text-muted);">TPD: ${formatCompactNumber(m.current_tpd || 0)}</span>
              <span class="limit">/ ${m.tpd_limit ? formatCompactNumber(m.tpd_limit) : '∞'} (${tpdUtil}%)</span>
            </div>
            <div class="meter-track">
              <div class="meter-fill ${tpdMeterClass}" style="width: ${tpdUtil}%"></div>
            </div>
          </div>
        </td>

        <td>${statusCell}</td>
        <td>
          <label class="switch">
            <input type="checkbox" ${m.enabled ? 'checked' : ''} onchange="toggleModel('${m.platform}', '${m.model_id}', this.checked)">
            <span class="switch-slider"></span>
          </label>
        </td>
        <td>
          <div style="display: flex; gap: 4px; align-items: center;">
            <button class="btn btn-neutral btn-sm" onclick="openEditModelModalByIndex(${origIdx})">Edit</button>
            <button class="btn btn-danger-link btn-sm" onclick="deleteModel('${m.platform}', '${m.model_id}')">Delete</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

async function toggleModel(platform, modelId, enabled) {
  try {
    await fetch('/api/admin/models/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, model_id: modelId, enabled: enabled ? 1 : 0 })
    });
    loadModels();
  } catch (err) {
    console.error('Failed to toggle model:', err);
  }
}

async function clearModelCooldown(platform, modelId, sharedQuotaGroup) {
  try {
    const res = await fetch('/api/admin/models/clear-cooldown', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        platform,
        model_id: modelId,
        shared_quota_group: sharedQuotaGroup || null
      })
    });
    if (res.ok) {
      showToast(`Cleared cooldown for ${modelId}`, 'success');
      loadModels();
    } else {
      showToast('Failed to clear cooldown', 'error');
    }
  } catch (err) {
    console.error('Failed to clear cooldown:', err);
  }
}

function openAddModelModal() {
  if (!cachedKeys || cachedKeys.length === 0) {
    fetch('/api/admin/keys').then(r => r.json()).then(keys => {
      cachedKeys = keys;
      if (!cachedKeys || cachedKeys.length === 0) {
        showToast('Please register an API Key in the Vault tab before adding a model.', 'error');
        switchTab('keys');
        return;
      }
      populateModelModalForAdd();
    }).catch(() => {
      showToast('Please register an API Key in the Vault tab before adding a model.', 'error');
      switchTab('keys');
    });
    return;
  }
  populateModelModalForAdd();
}

function populateModelModalForAdd() {
  document.getElementById('modelModalTitle').textContent = 'Add New Model';
  document.getElementById('editModelMode').value = 'add';
  document.getElementById('editOldPlatform').value = '';
  document.getElementById('editOldModelId').value = '';

  const platformSelect = document.getElementById('editModelPlatformSelect');
  platformSelect.innerHTML = cachedKeys.map(k => `<option value="${k.platform}">${k.display_name || k.platform} (${k.platform.toUpperCase()})</option>`).join('');

  document.getElementById('editModelId').readOnly = false;
  document.getElementById('editModelForm').reset();
  document.getElementById('modelModal').style.display = 'flex';
}

function openEditModelModalByIndex(idx) {
  const m = cachedModels[idx];
  if (!m) return;

  const ensureKeys = cachedKeys.length > 0 ? Promise.resolve() : fetch('/api/admin/keys').then(r => r.json()).then(keys => { cachedKeys = keys; });

  ensureKeys.then(() => {
    document.getElementById('modelModalTitle').textContent = 'Edit Model Parameters';
    document.getElementById('editModelMode').value = 'edit';
    document.getElementById('editOldPlatform').value = m.platform;
    document.getElementById('editOldModelId').value = m.model_id;

    const platformSelect = document.getElementById('editModelPlatformSelect');
    platformSelect.innerHTML = cachedKeys.map(k => `<option value="${k.platform}">${k.display_name || k.platform} (${k.platform.toUpperCase()})</option>`).join('');
    platformSelect.value = m.platform;

    document.getElementById('editModelId').value = m.model_id;
    document.getElementById('editModelId').readOnly = false;
    document.getElementById('editDisplayName').value = m.display_name || m.model_id;
    document.getElementById('editSharedGroup').value = m.shared_quota_group || '';
    document.getElementById('editBaseScore').value = m.base_score !== undefined ? m.base_score : 3;
    document.getElementById('editRpmLimit').value = m.rpm_limit !== null && m.rpm_limit !== undefined ? m.rpm_limit : '';
    document.getElementById('editRpdLimit').value = m.rpd_limit !== null && m.rpd_limit !== undefined ? m.rpd_limit : '';
    document.getElementById('editTpmLimit').value = m.tpm_limit !== null && m.tpm_limit !== undefined ? m.tpm_limit : '';
    document.getElementById('editTpdLimit').value = m.tpd_limit !== null && m.tpd_limit !== undefined ? m.tpd_limit : '';

    document.getElementById('modelModal').style.display = 'flex';
  });
}

function closeModelModal() {
  document.getElementById('modelModal').style.display = 'none';
}

async function saveModelParameters(event) {
  event.preventDefault();
  const mode = document.getElementById('editModelMode').value;
  const platform = document.getElementById('editModelPlatformSelect').value;
  const oldPlatform = document.getElementById('editOldPlatform').value;
  const oldModelId = document.getElementById('editOldModelId').value;
  const modelId = document.getElementById('editModelId').value;
  const displayName = document.getElementById('editDisplayName').value;
  const sharedGroup = document.getElementById('editSharedGroup').value;
  const baseScore = parseInt(document.getElementById('editBaseScore').value, 10);
  const rpmLimit = document.getElementById('editRpmLimit').value ? parseInt(document.getElementById('editRpmLimit').value, 10) : null;
  const rpdLimit = document.getElementById('editRpdLimit').value ? parseInt(document.getElementById('editRpdLimit').value, 10) : null;
  const tpmLimit = document.getElementById('editTpmLimit').value ? parseInt(document.getElementById('editTpmLimit').value, 10) : null;
  const tpdLimit = document.getElementById('editTpdLimit').value ? parseInt(document.getElementById('editTpdLimit').value, 10) : null;

  const endpoint = mode === 'add' ? '/api/admin/models/add' : '/api/admin/models/update';

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        platform,
        model_id: modelId,
        old_platform: oldPlatform,
        old_model_id: oldModelId,
        display_name: displayName,
        shared_quota_group: sharedGroup,
        base_score: baseScore,
        rpm_limit: rpmLimit,
        rpd_limit: rpdLimit,
        tpm_limit: tpmLimit,
        tpd_limit: tpdLimit
      })
    });

    if (res.ok) {
      closeModelModal();
      showToast(mode === 'add' ? 'Model added to catalog' : 'Model parameters updated', 'success');
      loadModels();
    } else {
      showToast('Failed to save model parameters', 'error');
    }
  } catch (err) {
    console.error('Failed to save model:', err);
    showToast('Error saving model parameters', 'error');
  }
}

async function deleteModel(platform, modelId) {
  if (!confirm(`Are you sure you want to delete model '${modelId}' from catalog?`)) return;
  try {
    await fetch('/api/admin/models/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, model_id: modelId })
    });
    loadModels();
  } catch (err) {
    console.error('Failed to delete model:', err);
  }
}


// ============================================================================
// Prompt Triage Classifier Control
// ============================================================================

async function loadTriageSettings() {
  try {
    const res = await fetch('/api/admin/triage');
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('triageStrategy').value = data.triage_strategy || 'heuristic';
    toggleTriageModelSelect();

    if (data.triage_platform && data.triage_model) {
      activeTriageModelValue = `${data.triage_platform}:${data.triage_model}`;
      const sel = document.getElementById('triageModelSelect');
      if (sel && sel.options.length > 0) {
        sel.value = activeTriageModelValue;
      }
    }
  } catch (err) {
    console.error('Failed to load triage settings:', err);
  }
}

function toggleTriageModelSelect() {
  const strat = document.getElementById('triageStrategy').value;
  const fld = document.getElementById('triageModelField');
  if (fld) {
    fld.style.display = strat === 'heuristic' ? 'none' : 'block';
  }
}

async function saveTriageSettings() {
  const strategy = document.getElementById('triageStrategy').value;
  const selVal = document.getElementById('triageModelSelect').value;
  let platform = 'google';
  let model = 'gemini-3.5-flash-lite';
  if (selVal && selVal.includes(':')) {
    const parts = selVal.split(':');
    platform = parts[0];
    model = parts.slice(1).join(':');
  }

  try {
    const res = await fetch('/api/admin/triage/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        triage_strategy: strategy,
        triage_platform: platform,
        triage_model: model
      })
    });
    if (res.ok) {
      activeTriageModelValue = `${platform}:${model}`;
      showToast('Triage settings saved successfully!', 'success');
    } else {
      showToast('Failed to save triage settings', 'error');
    }
  } catch (err) {
    console.error('Failed to save triage settings:', err);
  }
}


// ============================================================================
// Tab 2: Traces & System Audit Logs (Limit 20, No Request ID)
// ============================================================================

async function loadTraces() {
  try {
    const res = await fetch('/api/admin/traces?limit=20');
    if (!res.ok) return;
    cachedTraces = await res.json();

    const tbody = document.getElementById('tracesTableBody');
    if (!tbody) return;

    if (!cachedTraces || cachedTraces.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No request traces logged yet. Send a request to see routing traces!</td></tr>';
      return;
    }

    tbody.innerHTML = cachedTraces.map((t, idx) => {
      const timeStr = t.created_at ? new Date(t.created_at).toLocaleTimeString() : '—';
      const statusBadge = t.final_status === 200
        ? '<span class="badge badge-success">200 OK</span>'
        : `<span class="badge badge-danger">${t.final_status}</span>`;

      // Parse attempt failover path
      let attemptSummary = 'Direct';
      try {
        const attempts = JSON.parse(t.attempts_detail || '[]');
        if (Array.isArray(attempts) && attempts.length > 1) {
          attemptSummary = `<span class="badge badge-warning">${attempts.length} attempts (Failover)</span>`;
        } else if (Array.isArray(attempts) && attempts.length === 1) {
          attemptSummary = `<span style="color: var(--text-muted); font-size: 12px;">1 attempt</span>`;
        }
      } catch (e) {
        attemptSummary = 'Direct';
      }

      return `
        <tr>
          <td><span style="font-size: 12px; color: var(--text-muted);">${timeStr}</span></td>
          <td>
            <div>Grade <strong>${t.triage_difficulty !== null ? t.triage_difficulty : '—'}</strong> / 5</div>
            ${t.has_tools ? '<div style="margin-top: 2px;"><span class="badge badge-neutral" style="font-size: 10px;">Tools</span></div>' : ''}
          </td>
          <td>
            <div><strong>${t.final_model_id || 'unknown'}</strong></div>
            <div style="font-size: 11px; color: var(--text-muted);">${(t.final_platform || '').toUpperCase()}</div>
          </td>
          <td>${statusBadge}</td>
          <td>${formatCompactNumber(t.tokens_output || 0)}</td>
          <td>${attemptSummary}</td>
          <td>
            <button class="btn btn-neutral btn-sm" onclick="openTraceModalByIndex(${idx})">Inspect</button>
          </td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    console.error('Failed to load traces:', err);
  }
}

function openTraceModalByIndex(idx) {
  const t = cachedTraces[idx];
  if (!t) return;

  const modalBody = document.getElementById('traceModalBody');
  if (!modalBody) return;

  let attemptsHtml = '<p style="color: var(--text-muted);">No detailed attempt waterfall available.</p>';
  try {
    const attempts = JSON.parse(t.attempts_detail || '[]');
    if (Array.isArray(attempts) && attempts.length > 0) {
      attemptsHtml = attempts.map(a => `
        <div class="trace-attempt-card">
          <div class="trace-attempt-header">
            <span>Attempt #${a.attempt}: <code>${a.route}</code></span>
            <span class="badge ${a.status === 200 ? 'badge-success' : 'badge-danger'}">${a.status}</span>
          </div>
          ${a.error ? `<div style="font-size: 11px; color: var(--accent-red); margin-top: 4px;">${a.error}</div>` : ''}
        </div>
      `).join('');
    }
  } catch (e) {}

  modalBody.innerHTML = `
    <div style="display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap;">
      <div>
        <span style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Difficulty</span>
        <div><strong>Grade ${t.triage_difficulty || '—'} / 5</strong></div>
      </div>
      <div>
        <span style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Tool Calling</span>
        <div><strong>${t.has_tools ? 'Required' : 'None'}</strong></div>
      </div>
      <div>
        <span style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Final Route</span>
        <div><strong>${t.final_platform ? t.final_platform.toUpperCase() : ''} / ${t.final_model_id || 'unknown'}</strong></div>
      </div>
      <div>
        <span style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Status</span>
        <div><strong>${t.final_status}</strong></div>
      </div>
    </div>

    <div style="margin-bottom: 16px;">
      <h4 style="font-size: 13px; font-weight: 600; margin-bottom: 8px;">Routing Waterfall & Failovers</h4>
      ${attemptsHtml}
    </div>

    ${t.response_preview ? `
      <div>
        <h4 style="font-size: 13px; font-weight: 600; margin-bottom: 8px;">Response Snippet</h4>
        <pre class="code-preview">${t.response_preview}</pre>
      </div>
    ` : ''}
  `;

  document.getElementById('traceModal').style.display = 'flex';
}

function closeTraceModal() {
  document.getElementById('traceModal').style.display = 'none';
}

async function loadAudit() {
  try {
    const res = await fetch('/api/admin/audit?limit=20');
    if (!res.ok) return;
    const logs = await res.json();

    const tbody = document.getElementById('auditTableBody');
    if (!tbody) return;

    if (!logs || logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="table-empty">No administrative audit records logged yet.</td></tr>';
      return;
    }

    tbody.innerHTML = logs.map(l => `
      <tr>
        <td><span style="font-size: 12px; color: var(--text-muted);">${new Date(l.created_at).toLocaleString()}</span></td>
        <td><span class="badge badge-neutral" style="font-weight: 600;">${l.action}</span></td>
        <td>${l.target_type}</td>
        <td><code>${l.target_id}</code></td>
        <td style="font-size: 12px; color: var(--text-muted);">${l.details || '—'}</td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Failed to load audit logs:', err);
  }
}


// ============================================================================
// Tab 3: Analytics (Lazy Fetched)
// ============================================================================

async function changeAnalyticsTimeframe(tf) {
  currentAnalyticsTimeframe = tf;
  ['today', '7d', '30d', 'all'].forEach(t => {
    const btn = document.getElementById(`tf-${t}`);
    if (btn) {
      if (t === tf) {
        btn.className = 'btn btn-primary btn-sm';
      } else {
        btn.className = 'btn btn-neutral btn-sm';
      }
    }
  });
  await loadAnalytics(tf);
}

async function loadAnalytics(tf = currentAnalyticsTimeframe) {
  try {
    const res = await fetch(`/api/admin/analytics?timeframe=${tf}`);
    if (!res.ok) return;
    const data = await res.json();

    document.getElementById('analyticsTotalReqs').textContent = formatCompactNumber(data.total_requests || 0);
    document.getElementById('analyticsSuccessRate').textContent = `${data.success_rate}% Success Rate (${formatCompactNumber(data.successful_requests || 0)} passed)`;

    document.getElementById('analyticsTotalTokens').textContent = formatCompactNumber(data.total_tokens || 0);
    document.getElementById('analyticsTokenSplit').textContent = `${formatCompactNumber(data.total_input_tokens || 0)} In / ${formatCompactNumber(data.total_output_tokens || 0)} Out`;

    document.getElementById('analyticsAvgLatency').textContent = `${data.avg_latency_ms || 0} ms`;
    document.getElementById('analyticsAvgTtft').textContent = `TTFT: ${data.avg_ttft_ms || 0} ms`;

    document.getElementById('analyticsFailedReqs').textContent = formatCompactNumber(data.failed_requests || 0);

    const tbody = document.getElementById('analyticsTableBody');
    if (!tbody) return;

    if (!data.models_breakdown || data.models_breakdown.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="table-empty">No usage data logged for this timeframe.</td></tr>';
      return;
    }

    tbody.innerHTML = data.models_breakdown.map(m => `
      <tr>
        <td><strong>${m.model_id}</strong></td>
        <td><span class="badge badge-neutral">${m.platform.toUpperCase()}</span></td>
        <td>${formatCompactNumber(m.request_count)}</td>
        <td>${formatCompactNumber(m.total_tokens)}</td>
        <td>${m.avg_latency_ms} ms</td>
        <td>
          <span class="badge ${m.success_rate >= 95 ? 'badge-success' : m.success_rate >= 80 ? 'badge-warning' : 'badge-neutral'}">
            ${m.success_rate}%
          </span>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Failed to load analytics:', err);
  }
}


// ============================================================================
// Tab 4: API Key Vault (Lazy Fetched & Fixed Edit Key)
// ============================================================================

async function loadKeys() {
  try {
    const res = await fetch('/api/admin/keys');
    if (!res.ok) return;
    cachedKeys = await res.json();

    const tbody = document.getElementById('keysTableBody');
    if (!tbody) return;

    if (!cachedKeys || cachedKeys.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No API keys registered. Add a provider key above to activate models.</td></tr>';
      return;
    }

    tbody.innerHTML = cachedKeys.map((k) => `
      <tr>
        <td><span class="badge badge-neutral">${k.platform.toUpperCase()}</span></td>
        <td><strong>${k.display_name || k.platform}</strong></td>
        <td><code>${k.api_url}</code></td>
        <td><span class="badge ${k.is_healthy ? 'badge-success' : 'badge-danger'}">${k.is_healthy ? 'Healthy' : 'Auth Error'}</span></td>
        <td>
          <label class="switch">
            <input type="checkbox" ${k.enabled ? 'checked' : ''} onchange="toggleKey('${k.platform}', this.checked)">
            <span class="switch-slider"></span>
          </label>
        </td>
        <td>${k.last_used_at ? new Date(k.last_used_at).toLocaleTimeString() : 'Never'}</td>
        <td>
          <div style="display: flex; gap: 4px; align-items: center;">
            <button class="btn btn-neutral btn-sm" onclick="openEditKeyModal('${k.platform}')">Edit</button>
            <button class="btn btn-danger-link btn-sm" onclick="deleteKey('${k.platform}')">Delete</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Failed to load keys:', err);
  }
}

async function toggleKey(platform, enabled) {
  try {
    await fetch('/api/admin/keys/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, enabled: enabled ? 1 : 0 })
    });
    loadKeys();
  } catch (err) {
    console.error('Failed to toggle key:', err);
  }
}

function openKeyModal() {
  document.getElementById('keyModalTitle').textContent = 'Add Provider Key';
  document.getElementById('keyPlatform').disabled = false;
  document.getElementById('addKeyForm').reset();
  document.getElementById('keyModal').style.display = 'flex';
}

function openEditKeyModal(platform) {
  const k = cachedKeys.find(item => item.platform === platform);
  if (!k) return;
  document.getElementById('keyModalTitle').textContent = `Edit Key: ${k.platform.toUpperCase()}`;
  document.getElementById('keyPlatform').value = k.platform;
  document.getElementById('keyPlatform').disabled = true;
  document.getElementById('keyDisplayName').value = k.display_name || k.platform;
  document.getElementById('keyApiUrl').value = k.api_url || '';
  document.getElementById('keySecret').value = '';
  document.getElementById('keyModal').style.display = 'flex';
}

function closeKeyModal() {
  document.getElementById('keyModal').style.display = 'none';
}

async function saveApiKey(event) {
  event.preventDefault();
  const platform = document.getElementById('keyPlatform').value;
  const displayName = document.getElementById('keyDisplayName').value;
  const apiUrl = document.getElementById('keyApiUrl').value;
  const apiKey = document.getElementById('keySecret').value;

  try {
    const res = await fetch('/api/admin/keys/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        platform,
        display_name: displayName,
        api_url: apiUrl,
        api_key: apiKey
      })
    });

    if (res.ok) {
      closeKeyModal();
      document.getElementById('addKeyForm').reset();
      showToast(`Saved API Key for platform '${platform}'`, 'success');
      loadKeys();
    } else {
      showToast('Failed to save API Key', 'error');
    }
  } catch (err) {
    console.error('Failed to save key:', err);
    showToast('Error saving API Key', 'error');
  }
}

async function deleteKey(platform) {
  if (!confirm(`Are you sure you want to delete the API key for '${platform.toUpperCase()}'?`)) return;
  try {
    await fetch('/api/admin/keys/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform })
    });
    showToast(`Deleted API Key for platform '${platform}'`, 'success');
    loadKeys();
  } catch (err) {
    console.error('Failed to delete key:', err);
  }
}

function formatRemainingCooldown(expiryStr) {
  if (!expiryStr) return '';
  const normStr = expiryStr.includes('T') ? expiryStr : expiryStr.replace(' ', 'T') + 'Z';
  const expiryDate = new Date(normStr);
  const diffMs = expiryDate.getTime() - Date.now();
  if (isNaN(diffMs) || diffMs <= 0) return '0s left';
  const totalSecs = Math.floor(diffMs / 1000);
  const hours = Math.floor(totalSecs / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  if (hours > 0) return `${hours}h ${mins}m left`;
  if (mins > 0) return `${mins}m ${secs}s left`;
  return `${secs}s left`;
}

