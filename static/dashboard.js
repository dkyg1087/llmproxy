// Enterprise Multi-Tab Dashboard Logic

let currentTab = 'overview';
let cachedKeys = [];
let cachedModels = [];
let activeTriageModelValue = null;
let modelSortField = 'base_score';
let modelSortAsc = false;
let currentAnalyticsTimeframe = '7d';

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

document.addEventListener('DOMContentLoaded', () => {
  fetchDashboardData();
  loadTriageSettings();
  setInterval(fetchDashboardData, 3000);
});

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
}

async function fetchDashboardData() {
  await Promise.all([
    loadStats(),
    loadModels(),
    loadKeys(),
    loadAnalytics(currentAnalyticsTimeframe)
  ]);
}

async function loadStats() {
  try {
    const res = await fetch('/api/admin/stats');
    if (!res.ok) return;
    const data = await res.json();

    document.getElementById('metricTotalRequests').textContent = formatCompactNumber(data.total_requests || 0);
    document.getElementById('metricTotalTokens').textContent = formatCompactNumber(data.total_tokens || 0);
    document.getElementById('metricAvgTtft').textContent = `${data.avg_ttft_ms || 0} ms`;
    document.getElementById('metricActiveCooldowns').textContent = `${data.active_cooldowns || 0} / ${data.healthy_models || 0}`;

    // 1. Daily RPD Capacity
    const rpdCapacity = data.total_capacity_rpd || 0;
    const rpdUsed = data.current_used_rpd || 0;
    const rpdUtil = rpdCapacity > 0 ? Math.min(100, Math.round((rpdUsed / rpdCapacity) * 100)) : 0;
    const rpdAvailPct = 100 - rpdUtil;

    const rpdStatsEl = document.getElementById('capacityRpdStats');
    if (rpdStatsEl) rpdStatsEl.textContent = `${formatCompactNumber(rpdUsed)} / ${rpdCapacity ? formatCompactNumber(rpdCapacity) : '∞'} RPD Used (${rpdAvailPct}% Available)`;
    const rpdBar = document.getElementById('capacityRpdBar');
    if (rpdBar) {
      rpdBar.style.width = `${rpdUtil}%`;
      rpdBar.className = `meter-fill ${rpdUtil >= 90 ? 'crit' : rpdUtil >= 75 ? 'warn' : ''}`;
    }

    // 2. Daily TPD Capacity
    const tpdCapacity = data.total_capacity_tpd || 0;
    const tpdUsed = data.current_used_tpd || 0;
    const tpdUtil = tpdCapacity > 0 ? Math.min(100, Math.round((tpdUsed / tpdCapacity) * 100)) : 0;
    const tpdAvailPct = 100 - tpdUtil;

    const tpdStatsEl = document.getElementById('capacityTpdStats');
    if (tpdStatsEl) tpdStatsEl.textContent = `${formatCompactNumber(tpdUsed)} / ${tpdCapacity ? formatCompactNumber(tpdCapacity) : '∞'} TPD Used (${tpdAvailPct}% Available)`;
    const tpdBar = document.getElementById('capacityTpdBar');
    if (tpdBar) {
      tpdBar.style.width = `${tpdUtil}%`;
      tpdBar.className = `meter-fill ${tpdUtil >= 90 ? 'crit' : tpdUtil >= 75 ? 'warn' : ''}`;
    }

    // 3. Sliding 60s RPM Capacity
    const rpmCapacity = data.total_capacity_rpm || 0;
    const rpmUsed = data.current_used_rpm || 0;
    const rpmUtil = rpmCapacity > 0 ? Math.min(100, Math.round((rpmUsed / rpmCapacity) * 100)) : 0;

    const rpmStatsEl = document.getElementById('capacityRpmStats');
    if (rpmStatsEl) rpmStatsEl.textContent = `${formatCompactNumber(rpmUsed)} / ${rpmCapacity ? formatCompactNumber(rpmCapacity) : '∞'} RPM (${rpmUtil}%)`;
    const rpmBar = document.getElementById('capacityRpmBar');
    if (rpmBar) {
      rpmBar.style.width = `${rpmUtil}%`;
      rpmBar.className = `meter-fill ${rpmUtil >= 90 ? 'crit' : rpmUtil >= 75 ? 'warn' : ''}`;
    }

    // 4. Sliding 60s TPM Capacity
    const tpmCapacity = data.total_capacity_tpm || 0;
    const tpmUsed = data.current_used_tpm || 0;
    const tpmUtil = tpmCapacity > 0 ? Math.min(100, Math.round((tpmUsed / tpmCapacity) * 100)) : 0;

    const tpmStatsEl = document.getElementById('capacityTpmStats');
    if (tpmStatsEl) tpmStatsEl.textContent = `${formatCompactNumber(tpmUsed)} / ${tpmCapacity ? formatCompactNumber(tpmCapacity) : '∞'} TPM (${tpmUtil}%)`;
    const tpmBar = document.getElementById('capacityTpmBar');
    if (tpmBar) {
      tpmBar.style.width = `${tpmUtil}%`;
      tpmBar.className = `meter-fill ${tpmUtil >= 90 ? 'crit' : tpmUtil >= 75 ? 'warn' : ''}`;
    }

    const badgeText = document.getElementById('systemStatusText');
    const badge = document.getElementById('systemStatusBadge');
    if (data.healthy_models > 0) {
      badgeText.textContent = 'Operational';
      badge.className = 'status-indicator';
    } else {
      badgeText.textContent = 'All Models Quarantined';
      badge.className = 'status-indicator status-offline';
    }
  } catch (err) {
    console.error('Failed to load stats:', err);
  }
}

async function loadModels() {
  try {
    const res = await fetch('/api/admin/models');
    if (!res.ok) return;
    cachedModels = await res.json();

    const triageSel = document.getElementById('triageModelSelect');
    if (triageSel && cachedModels) {
      const targetVal = activeTriageModelValue || triageSel.value;
      triageSel.innerHTML = cachedModels.map(m =>
        `<option value="${m.platform}:${m.model_id}">${m.display_name} (${m.platform}/${m.model_id})</option>`
      ).join('');
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
    tbody.innerHTML = '<tr><td colspan="9" class="table-empty">No models configured in catalog. Add one above!</td></tr>';
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
    const rpmUtil = m.rpm_limit ? Math.min(100, Math.round((m.current_rpm / m.rpm_limit) * 100)) : 0;
    const tpmUtil = m.tpm_limit ? Math.min(100, Math.round((m.current_tpm / m.tpm_limit) * 100)) : 0;

    const rpmMeterClass = rpmUtil >= 90 ? 'crit' : rpmUtil >= 75 ? 'warn' : '';
    const tpmMeterClass = tpmUtil >= 90 ? 'crit' : tpmUtil >= 75 ? 'warn' : '';

    const statusBadge = m.is_cooldown 
      ? '<span class="badge badge-warning">Cooldown</span>'
      : m.enabled 
      ? '<span class="badge badge-success">Ready</span>'
      : '<span class="badge badge-neutral">Disabled</span>';

    return `
      <tr>
        <td>
          <div><strong>${m.display_name || m.model_id}</strong></div>
          <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;"><code>${m.model_id}</code></div>
        </td>
        <td><span class="badge badge-neutral">${m.platform.toUpperCase()}</span></td>
        <td>${m.shared_quota_group || '—'}</td>
        <td>${m.base_score}</td>
        <td>
          <div class="meter-container">
            <div class="meter-labels">
              <span>${formatCompactNumber(m.current_rpm)} RPM (${formatCompactNumber(m.current_rpd || 0)} RPD)</span>
              <span>${rpmUtil}%</span>
            </div>
            <div class="meter-track">
              <div class="meter-fill ${rpmMeterClass}" style="width: ${rpmUtil}%"></div>
            </div>
            <div style="font-size: 10px; color: var(--text-muted); margin-top: 2px;">Limits: ${m.rpm_limit ? formatCompactNumber(m.rpm_limit) : '∞'} RPM / ${m.rpd_limit ? formatCompactNumber(m.rpd_limit) : '∞'} RPD</div>
          </div>
        </td>
        <td>
          <div class="meter-container">
            <div class="meter-labels">
              <span>${formatCompactNumber(m.current_tpm || 0)} TPM (${formatCompactNumber(m.current_tpd || 0)} TPD)</span>
              <span>${tpmUtil}%</span>
            </div>
            <div class="meter-track">
              <div class="meter-fill ${tpmMeterClass}" style="width: ${tpmUtil}%"></div>
            </div>
            <div style="font-size: 10px; color: var(--text-muted); margin-top: 2px;">Limits: ${m.tpm_limit ? formatCompactNumber(m.tpm_limit) : '∞'} TPM / ${m.tpd_limit ? formatCompactNumber(m.tpd_limit) : '∞'} TPD</div>
          </div>
        </td>
        <td>${statusBadge}</td>
        <td>
          <label class="switch">
            <input type="checkbox" ${m.enabled ? 'checked' : ''} onchange="toggleModel('${m.platform}', '${m.model_id}', this.checked)">
            <span class="switch-slider"></span>
          </label>
        </td>
        <td>
          <button class="btn btn-neutral btn-sm" onclick="openEditModelModalByIndex(${origIdx})">Edit</button>
          <button class="btn btn-danger-link btn-sm" onclick="deleteModel('${m.platform}', '${m.model_id}')">Delete</button>
        </td>
      </tr>
    `;
  }).join('');
}

async function loadKeys() {
  try {
    const res = await fetch('/api/admin/keys');
    if (!res.ok) return;
    cachedKeys = await res.json();

    const tbody = document.getElementById('keysTableBody');
    if (!cachedKeys || cachedKeys.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No API keys registered. Add a key above.</td></tr>';
      return;
    }

    tbody.innerHTML = cachedKeys.map((k) => `
      <tr>
        <td><span class="badge badge-neutral">${k.platform.toUpperCase()}</span></td>
        <td><strong>${k.display_name || k.platform}</strong></td>
        <td><code>${k.api_url}</code></td>
        <td><span class="badge ${k.status === 'healthy' ? 'badge-success' : 'badge-warning'}">${k.status}</span></td>
        <td>
          <label class="switch">
            <input type="checkbox" ${k.enabled ? 'checked' : ''} onchange="toggleKey('${k.platform}', this.checked)">
            <span class="switch-slider"></span>
          </label>
        </td>
        <td>${k.last_used_at ? new Date(k.last_used_at).toLocaleTimeString() : 'Never'}</td>
        <td>
          <button class="btn btn-neutral btn-sm" onclick="openEditKeyModal('${k.platform}')">Edit</button>
          <button class="btn btn-danger-link btn-sm" onclick="deleteKey('${k.platform}')">Delete</button>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Failed to load keys:', err);
  }
}

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

async function toggleModel(platform, modelId, enabled) {
  try {
    await fetch('/api/admin/models/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, model_id: modelId, enabled: enabled ? 1 : 0 })
    });
    fetchDashboardData();
  } catch (err) {
    console.error('Failed to toggle model:', err);
  }
}

async function toggleKey(platform, enabled) {
  try {
    await fetch('/api/admin/keys/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, enabled: enabled ? 1 : 0 })
    });
    fetchDashboardData();
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

function openEditKeyModalByIndex(idx) {
  const k = cachedKeys[idx];
  if (!k) return;
  document.getElementById('keyModalTitle').textContent = `Edit Key: ${k.platform.toUpperCase()}`;
  document.getElementById('keyPlatform').value = k.platform;
  document.getElementById('keyPlatform').disabled = true;
  document.getElementById('keyDisplayName').value = k.display_name;
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
      fetchDashboardData();
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
    fetchDashboardData();
  } catch (err) {
    console.error('Failed to delete key:', err);
    showToast('Failed to delete API Key', 'error');
  }
}

function openAddModelModal() {
  if (!cachedKeys || cachedKeys.length === 0) {
    showToast('Please register an API Key in the Vault tab before adding a model.', 'error');
    switchTab('keys');
    return;
  }

  document.getElementById('modelModalTitle').textContent = 'Add New Model';
  document.getElementById('editModelMode').value = 'add';
  document.getElementById('editOldPlatform').value = '';
  document.getElementById('editOldModelId').value = '';

  const platformSelect = document.getElementById('editModelPlatformSelect');
  platformSelect.innerHTML = cachedKeys.map(k => `<option value="${k.platform}">${k.display_name} (${k.platform.toUpperCase()})</option>`).join('');

  document.getElementById('editModelId').readOnly = false;
  document.getElementById('editModelForm').reset();
  document.getElementById('modelModal').style.display = 'flex';
}

function openEditModelModalByIndex(idx) {
  const m = cachedModels[idx];
  if (!m) return;

  document.getElementById('modelModalTitle').textContent = 'Edit Model Parameters';
  document.getElementById('editModelMode').value = 'edit';
  document.getElementById('editOldPlatform').value = m.platform;
  document.getElementById('editOldModelId').value = m.model_id;

  const platformSelect = document.getElementById('editModelPlatformSelect');
  platformSelect.innerHTML = cachedKeys.map(k => `<option value="${k.platform}">${k.display_name} (${k.platform.toUpperCase()})</option>`).join('');
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
      fetchDashboardData();
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
    fetchDashboardData();
  } catch (err) {
    console.error('Failed to delete model:', err);
  }
}

async function loadTriageSettings() {
  try {
    const res = await fetch('/api/admin/triage');
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('triageStrategy').value = data.triage_strategy || 'llm';
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
  let model = 'gemini-3.1-flash-lite';
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
    showToast('Error saving triage settings', 'error');
  }
}
