/* ── State ──────────────────────────────────────────────────────────────── */
let channels    = [];
let analytics   = {};
let uploadsChart = null;
let refreshTimer = 15;

/* ── Boot ───────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  refresh();
  setInterval(tick, 1000);
});

function tick() {
  refreshTimer--;
  const el = document.getElementById('refresh-tick');
  if (el) el.textContent = `Refreshing in ${refreshTimer}s`;
  if (refreshTimer <= 0) {
    refreshTimer = 15;
    refresh();
  }
}

/* ── Main refresh ────────────────────────────────────────────────────────── */
async function refresh() {
  await Promise.all([
    loadAnalytics(),
    loadChannels(),
    loadHistory(),
    loadLogs(),
  ]);
}

/* ── Analytics ───────────────────────────────────────────────────────────── */
async function loadAnalytics() {
  try {
    const data = await get('/api/analytics');
    analytics  = data;

    setText('s-channels', data.total_channels);
    setText('s-queue',    fmt(data.total_queue));
    setText('s-uploaded', fmt(data.total_uploaded));
    setText('s-today',    data.uploads_today);

    renderChart(data);
  } catch (e) {
    console.error('analytics:', e);
  }
}

function renderChart(data) {
  const ctx = document.getElementById('uploads-chart');
  if (!ctx) return;

  const labels   = data.days.map(d => {
    const dt = new Date(d + 'T00:00:00');
    return dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  });

  const chIds    = Object.keys(data.channel_names);
  const datasets = chIds.map(cid => ({
    label:           data.channel_names[cid] || cid,
    backgroundColor: data.channel_colors[cid] || '#ff4444',
    borderRadius:    4,
    data: data.days.map(d => (data.per_day[d] || {})[cid] || 0),
  }));

  if (datasets.length === 0) {
    datasets.push({
      label: 'Uploads',
      backgroundColor: '#ff2222',
      borderRadius: 4,
      data: data.days.map(() => 0),
    });
  }

  if (uploadsChart) {
    uploadsChart.data.labels   = labels;
    uploadsChart.data.datasets = datasets;
    uploadsChart.update();
    return;
  }

  uploadsChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: {
          labels: { color: '#888', boxWidth: 12, padding: 16, font: { size: 11 } },
        },
        tooltip: {
          backgroundColor: '#1a1a1a',
          borderColor: '#2a2a2a',
          borderWidth: 1,
          titleColor: '#e8e8e8',
          bodyColor: '#aaa',
        },
      },
      scales: {
        x: {
          stacked: true,
          grid:    { color: '#1e1e1e' },
          ticks:   { color: '#666', font: { size: 10 } },
        },
        y: {
          stacked:    true,
          beginAtZero: true,
          ticks:       { color: '#666', stepSize: 1, font: { size: 10 } },
          grid:        { color: '#1e1e1e' },
        },
      },
    },
  });
}

/* ── Channels ────────────────────────────────────────────────────────────── */
async function loadChannels() {
  try {
    channels = await get('/api/channels');
    renderChannels();
  } catch (e) {
    console.error('channels:', e);
  }
}

function renderChannels() {
  const grid   = document.getElementById('channels-grid');
  const noMsg  = document.getElementById('no-channels');

  if (!channels.length) {
    grid.innerHTML = '';
    if (noMsg) grid.appendChild(noMsg);
    return;
  }

  if (noMsg) noMsg.remove();

  // Remove cards for deleted channels
  const existingIds = new Set(channels.map(c => c.id));
  grid.querySelectorAll('.channel-card').forEach(el => {
    if (!existingIds.has(el.dataset.id)) el.remove();
  });

  for (const ch of channels) {
    const existing = grid.querySelector(`[data-id="${ch.id}"]`);
    const card     = buildChannelCard(ch);
    if (existing) {
      existing.replaceWith(card);
    } else {
      grid.appendChild(card);
    }
  }
}

function calcSchedule(timeStr, uploadsPerDay) {
  const [h, m] = (timeStr || '09:00').split(':').map(Number);
  const n = Math.max(1, uploadsPerDay || 1);
  const intervalMins = Math.floor(24 * 60 / n);
  const slots = [];
  for (let i = 0; i < n; i++) {
    const total = (h * 60 + m + i * intervalMins) % (24 * 60);
    const sh = String(Math.floor(total / 60)).padStart(2, '0');
    const sm = String(total % 60).padStart(2, '0');
    slots.push(`${sh}:${sm}`);
  }
  return slots;
}

function buildChannelCard(ch) {
  const st      = ch.state || {};
  const status  = st.status || 'idle';
  const byChMap = analytics.by_channel || {};
  const total   = byChMap[ch.id] || 0;
  const color   = ch.color || '#ff4444';

  // Today count for this channel
  const todayStr   = new Date().toLocaleDateString('en-CA');
  const todayCount = ((analytics.per_day || {})[todayStr] || {})[ch.id] || 0;

  // Upload schedule — slots are dynamic (now+15min) so just show the cadence
  const n         = ch.uploads_per_day || 1;
  const intervalH = Math.round(24 / n);
  const intervalM = Math.round((24 * 60 / n) % 60);
  const cadenceStr = intervalM ? `${intervalH}h ${intervalM}m` : `${intervalH}h`;
  const slotsHtml  = `<span class="slot-time">${n} video${n > 1 ? 's' : ''}</span>`;
  const cadence    = n > 1 ? `every ${cadenceStr} from trigger` : 'once on trigger';

  const card = el('div', 'channel-card');
  card.dataset.id = ch.id;
  card.style.borderLeftColor = color;

  card.innerHTML = `
    <div class="ch-header">
      <span class="ch-name">${esc(ch.name)}</span>
      <span class="platform-badge">${esc(ch.platform || 'youtube').toUpperCase()}</span>
    </div>

    <div class="ch-stats">
      <div class="ch-stat">
        <div class="val">${st.queue_count != null ? fmt(st.queue_count) : '…'}</div>
        <div class="lbl">Queue</div>
      </div>
      <div class="ch-stat">
        <div class="val">${fmt(total)}</div>
        <div class="lbl">Uploaded</div>
      </div>
      <div class="ch-stat">
        <div class="val">${todayCount}</div>
        <div class="lbl">Today</div>
      </div>
    </div>

    <div class="ch-schedule">
      <span class="schedule-icon">📅</span>
      <span class="schedule-slots">${slotsHtml}</span>
      <span class="schedule-cadence">${cadence}</span>
    </div>

    <div class="ch-last">
      ${st.last_upload_at
        ? `Last: ${st.last_upload_at.replace(/^\d{4}-\d{2}-\d{2} /, '')}${st.last_video_url
            ? ` · <a href="${st.last_video_url}" target="_blank">watch ↗</a>`
            : ''}`
        : 'No uploads yet'}
    </div>
    ${st.last_video_title ? `<div class="ch-last-title">"${esc(st.last_video_title)}"</div>` : ''}
    ${st.error_msg ? `<div class="ch-error">⚠ ${esc(st.error_msg)}</div>` : ''}

    <div class="ch-actions" id="actions-${ch.id}"></div>
  `;

  // Status badge
  const statusLabels = { idle:'Idle', uploading:'Uploading…', error:'Error', paused:'Paused', noauth:'Not Auth' };
  const badge = el('div', `status-badge st-${status}`);
  badge.innerHTML = `<div class="dot${status === 'uploading' ? ' pulse' : ''}"></div>
                     <span>${statusLabels[status] || status}</span>`;

  const actions = card.querySelector(`#actions-${ch.id}`);

  // Schedule Batch
  const btnUp = el('button', 'btn btn-primary btn-sm');
  btnUp.textContent = `Schedule ${ch.uploads_per_day || 1}`;
  btnUp.title       = `Upload & schedule ${ch.uploads_per_day || 1} videos on YouTube now`;
  btnUp.disabled    = status === 'uploading';
  btnUp.onclick     = () => triggerUpload(ch.id, btnUp);

  // Pause / Resume
  const btnPause = el('button', 'btn btn-ghost btn-sm' + (status === 'paused' ? ' active' : ''));
  btnPause.textContent = status === 'paused' ? 'Resume' : 'Pause';
  btnPause.onclick     = () => togglePause(ch.id);

  // Authenticate
  const btnAuth = el('button', 'btn btn-ghost btn-sm');
  btnAuth.textContent = '🔑 Auth';
  btnAuth.title       = 'Re-authenticate this channel (opens browser)';
  btnAuth.onclick     = () => authenticateChannel(ch.id, btnAuth);

  // Edit
  const btnEdit = el('button', 'btn btn-ghost btn-sm');
  btnEdit.textContent = 'Edit';
  btnEdit.onclick     = () => openModal(ch);

  // Reset (clear uploaded list)
  const btnReset = el('button', 'btn btn-ghost btn-sm danger');
  btnReset.textContent = 'Reset';
  btnReset.title       = 'Clear uploaded list — lets the system re-upload from the beginning';
  btnReset.onclick     = () => clearChannelData(ch.id, ch.name);

  // Delete
  const btnDel = el('button', 'btn btn-ghost btn-sm danger');
  btnDel.textContent = '✕';
  btnDel.title       = 'Delete channel';
  btnDel.onclick     = () => deleteChannel(ch.id, ch.name);

  actions.append(btnUp, btnPause, btnAuth, btnEdit, btnReset, btnDel);
  actions.appendChild(badge);

  return card;
}

/* ── History table ───────────────────────────────────────────────────────── */
async function loadHistory() {
  try {
    const rows = await get('/api/history?limit=30');
    const tbody = document.getElementById('history-body');
    const count = document.getElementById('log-count');
    if (count) count.textContent = rows.length ? `${rows.length} recent` : '';

    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted-sm" style="text-align:center;padding:20px">No uploads yet</td></tr>`;
      return;
    }

    const chColors = Object.fromEntries((channels || []).map(c => [c.id, c.color || '#ff4444']));
    const chNames  = Object.fromEntries((channels || []).map(c => [c.id, c.name]));

    tbody.innerHTML = rows.map(r => {
      const color = chColors[r.channel_id] || '#ff4444';
      const name  = chNames[r.channel_id]  || r.channel_name || r.channel_id;
      const time  = new Date(r.timestamp).toLocaleString('en-US', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
      });
      return `<tr>
        <td style="color:var(--muted)">${time}</td>
        <td><div class="td-channel"><div class="ch-dot" style="background:${color}"></div>${esc(name)}</div></td>
        <td>${esc(r.title || '—')}</td>
        <td>${r.url ? `<a href="${r.url}" target="_blank" style="color:#ff5555">watch ↗</a>` : '—'}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.error('history:', e);
  }
}

/* ── System log ──────────────────────────────────────────────────────────── */
async function loadLogs() {
  try {
    const logs      = await get('/api/logs?limit=120');
    const box       = document.getElementById('log-box');
    const scroll    = document.getElementById('autoscroll-toggle')?.checked;
    const wasBottom = box && (box.scrollHeight - box.scrollTop - box.clientHeight < 60);

    box.innerHTML = logs.map(renderLogLine).join('');

    if (scroll && wasBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {}
}

function renderLogLine(l) {
  const msg = l.message || '';

  // Job separator lines (─────... or ═════...)
  if (/^[─═]{5,}/.test(msg)) return '<div class="log-sep"></div>';

  const isErr  = l.level === 'ERROR';
  const isWarn = l.level === 'WARNING';

  // Phase detection → icon + colour
  let phase = 'default';
  if (/Drive download|Downloading from/i.test(msg))              phase = 'download';
  else if (/Download complete/i.test(msg))                       phase = 'done';
  else if (/Gemini|Groq|AI metadata|Generating metadata/i.test(msg)) phase = 'ai';
  else if (/YouTube upload|Uploading to YouTube/i.test(msg))    phase = 'upload';
  else if (/upload complete|Recorded in|youtu\.be/i.test(msg))  phase = 'done';
  else if (/Batch done|videos scheduled/i.test(msg))            phase = 'done';
  else if (/Batch start|Batch:/i.test(msg))                     phase = 'upload';
  else if (/Video \d+\/\d+.*scheduled/i.test(msg))              phase = 'upload';
  else if (/Scheduled for:/i.test(msg))                         phase = 'ai';
  else if (/Next video:/i.test(msg))                            phase = 'file';
  else if (isErr)                                               phase = 'error';
  else if (isWarn)                                              phase = 'warn';

  const icons = {
    download: '⬇', ai: '🤖', upload: '⬆', done: '✓',
    file: '▶', error: '✗', warn: '⚠', default: '·'
  };

  // Progress bar for lines with XX%
  const pctMatch = msg.match(/(\d+)%/);
  const pct      = pctMatch ? Math.min(100, parseInt(pctMatch[1])) : null;

  // Linkify YouTube URLs
  let htmlMsg = esc(msg);
  const urlMatch = msg.match(/(https:\/\/youtu\.be\/[A-Za-z0-9_-]+)/);
  if (urlMatch) {
    const safeUrl = esc(urlMatch[1]);
    htmlMsg = htmlMsg.replace(safeUrl,
      `<a href="${urlMatch[1]}" target="_blank" class="log-link">${safeUrl}</a>`);
  }

  const progressBar = pct !== null
    ? `<div class="log-progress"><div class="log-progress-bar" style="width:${pct}%"></div></div>`
    : '';

  return `<div class="log-line${isErr ? ' log-err' : isWarn ? ' log-warn' : ''}">
    <span class="log-icon phase-${phase}">${icons[phase]}</span>
    <span class="log-body">
      <span class="log-msg">${htmlMsg}</span>
      ${progressBar}
    </span>
    <span class="log-time">${l.time || ''}</span>
  </div>`;
}

/* ── Channel actions ─────────────────────────────────────────────────────── */
async function triggerUpload(chId, btn) {
  btn.disabled = true;
  btn.textContent = 'Triggering…';
  const r = await post(`/api/channels/${chId}/trigger`);
  if (!r.ok) alert(r.message || 'Failed to trigger');
  setTimeout(refresh, 500);
}

async function togglePause(chId) {
  await post(`/api/channels/${chId}/pause`);
  loadChannels();
}

async function authenticateChannel(chId, btn) {
  btn.disabled = true;
  btn.textContent = '⏳ Opening browser…';
  try {
    const r = await post(`/api/channels/${chId}/authenticate`);
    if (r.ok) {
      btn.textContent = '✓ Authenticated';
      btn.style.color = 'var(--green)';
    } else {
      throw new Error(r.detail || 'Auth failed');
    }
  } catch (e) {
    alert(`Authentication error: ${e.message || e}`);
    btn.textContent = '🔑 Auth';
    btn.disabled = false;
  }
}

async function deleteChannel(chId, name) {
  if (!confirm(`Delete channel "${name}"? This cannot be undone.`)) return;
  await fetch(`/api/channels/${chId}`, { method: 'DELETE' });
  refresh();
}

/* ── Add / Edit Modal ────────────────────────────────────────────────────── */
function openModal(ch = null) {
  const backdrop = document.getElementById('modal-backdrop');
  const title    = document.getElementById('modal-title');
  backdrop.classList.add('open');

  if (ch) {
    title.textContent = 'Edit Channel';
    document.getElementById('edit-id').value    = ch.id;
    document.getElementById('f-name').value     = ch.name || '';
    document.getElementById('f-color').value    = ch.color || '#ff4444';
    document.getElementById('f-platform').value = ch.platform || 'youtube';
    document.getElementById('f-time').value     = ch.upload_time     || '09:00';
    document.getElementById('f-upd').value      = ch.uploads_per_day || 1;
    document.getElementById('f-privacy').value  = ch.privacy_status  || 'public';
    document.getElementById('f-queue').value    = ch.drive_queue_folder_id || '';
    document.getElementById('f-done').value     = ch.drive_done_folder_id  || '';
    document.getElementById('f-gemini').value   = ch.gemini_extra_prompt   || '';
    document.getElementById('f-enabled').checked = ch.enabled !== false;
  } else {
    title.textContent = 'Add Channel';
    document.getElementById('edit-id').value = '';
    document.getElementById('channel-form').reset();
    document.getElementById('f-color').value = '#ff4444';
    document.getElementById('f-enabled').checked = true;
  }
  updateSlotPreview();
}

function updateSlotPreview() {
  const updVal  = parseInt(document.getElementById('f-upd')?.value) || 1;
  const preview = document.getElementById('slot-preview');
  if (!preview) return;

  const totalMins  = Math.round(24 * 60 / updVal);
  const intervalH  = Math.floor(totalMins / 60);
  const intervalM  = totalMins % 60;
  const cadenceStr = intervalM ? `${intervalH}h ${intervalM}m` : `${intervalH}h`;

  const example = [];
  const now = new Date();
  const base = new Date(now.getTime() + 15 * 60 * 1000);
  base.setSeconds(0, 0);
  for (let i = 0; i < updVal; i++) {
    const t = new Date(base.getTime() + i * totalMins * 60 * 1000);
    example.push(`<strong>${t.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', hour12: false})}</strong>`);
  }

  preview.innerHTML = `📅 <strong>${updVal}</strong> videos · every <strong>${cadenceStr}</strong><br>
    <span style="color:var(--muted);font-size:.72rem">If triggered now → ${example.join(' &nbsp;·&nbsp; ')}</span>`;
  preview.className = 'slot-preview visible';
}

async function clearChannelData(chId, name) {
  if (!confirm(`Reset uploaded list for "${name}"?\n\nThis removes all upload records so the system starts from the first video again. Your existing YouTube videos are NOT deleted.`)) return;
  await fetch(`/api/channels/${chId}/uploaded`, { method: 'DELETE' });
  refresh();
}

async function clearHistory() {
  if (!confirm('Clear all recent upload history from the dashboard?\n\n(This only removes the dashboard log — your YouTube videos and Google Drive files are untouched.)')) return;
  await fetch('/api/history', { method: 'DELETE' });
  loadHistory();
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modal-backdrop')) return;
  document.getElementById('modal-backdrop').classList.remove('open');
}

async function saveChannel(e) {
  e.preventDefault();
  const editId = document.getElementById('edit-id').value;
  const body = {
    name:                  document.getElementById('f-name').value.trim(),
    color:                 document.getElementById('f-color').value,
    platform:              document.getElementById('f-platform').value,
    upload_time:           document.getElementById('f-time').value,
    privacy_status:        document.getElementById('f-privacy').value,
    drive_queue_folder_id: document.getElementById('f-queue').value.trim(),
    drive_done_folder_id:  document.getElementById('f-done').value.trim(),
    uploads_per_day:       parseInt(document.getElementById('f-upd').value) || 1,
    gemini_extra_prompt:   document.getElementById('f-gemini').value.trim(),
    enabled:               document.getElementById('f-enabled').checked,
  };

  const btn = document.getElementById('save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    if (editId) {
      await fetch(`/api/channels/${editId}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } else {
      await fetch('/api/channels', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
    document.getElementById('modal-backdrop').classList.remove('open');
    refresh();
  } catch (err) {
    alert('Save failed: ' + err);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save Channel';
  }
}

/* ── Settings modal ─────────────────────────────────────────────────────── */
async function openSettings() {
  document.getElementById('settings-backdrop').classList.add('open');
  try {
    const s = await get('/api/settings');

    // Secrets status
    const sEl = document.getElementById('secrets-status');
    sEl.textContent = s.secrets_file_exists ? '✓ client_secrets.json found' : '✗ client_secrets.json not found';
    sEl.className   = 'status-pill ' + (s.secrets_file_exists ? 'ok' : 'missing');

    // Groq status
    const qEl = document.getElementById('groq-status');
    qEl.textContent = s.groq_api_key_is_set ? `✓ Groq key set (${s.groq_api_key})` : '✗ API key not set';
    qEl.className   = 'status-pill ' + (s.groq_api_key_is_set ? 'ok' : 'missing');
    document.getElementById('s-groq-key').placeholder = s.groq_api_key_is_set ? '••••••••' : 'gsk_…';

    // Gemini status
    const gEl = document.getElementById('gemini-status');
    gEl.textContent = s.gemini_api_key_is_set ? `✓ API key set (${s.gemini_api_key})` : '✗ API key not set';
    gEl.className   = 'status-pill ' + (s.gemini_api_key_is_set ? 'ok' : 'missing');
    document.getElementById('s-gemini-key').placeholder = s.gemini_api_key_is_set ? '••••••••' : 'AIza…';

    // Defaults
    document.getElementById('s-time').value    = s.default_upload_time || '09:00';
    document.getElementById('s-privacy').value = s.default_privacy     || 'public';
  } catch (e) {
    console.error('settings load:', e);
  }
}

function closeSettings(e) {
  if (e && e.target !== document.getElementById('settings-backdrop')) return;
  document.getElementById('settings-backdrop').classList.remove('open');
}

async function saveSecrets() {
  const raw = document.getElementById('s-secrets').value.trim();
  if (!raw) return alert('Paste your client_secrets.json content first.');
  try {
    const r = await fetch('/api/secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ json_content: raw }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Save failed');
    document.getElementById('secrets-status').textContent = '✓ client_secrets.json saved!';
    document.getElementById('secrets-status').className   = 'status-pill ok';
    document.getElementById('s-secrets').value = '';
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

function toggleGroqKeyVisibility() {
  const inp = document.getElementById('s-groq-key');
  const btn = inp.nextElementSibling;
  if (inp.type === 'password') { inp.type = 'text';     btn.textContent = 'Hide'; }
  else                         { inp.type = 'password'; btn.textContent = 'Show'; }
}

async function saveGroqKey() {
  const key = document.getElementById('s-groq-key').value.trim();
  if (!key) return alert('Enter your Groq API key first.');
  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ groq_api_key: key }),
  });
  const d = await r.json();
  if (d.ok) {
    document.getElementById('groq-status').textContent = '✓ Groq key saved!';
    document.getElementById('groq-status').className   = 'status-pill ok';
    document.getElementById('s-groq-key').value = '';
  }
}

function toggleKeyVisibility() {
  const inp = document.getElementById('s-gemini-key');
  const btn = inp.nextElementSibling;
  if (inp.type === 'password') { inp.type = 'text';     btn.textContent = 'Hide'; }
  else                         { inp.type = 'password'; btn.textContent = 'Show'; }
}

async function saveGeminiKey() {
  const key = document.getElementById('s-gemini-key').value.trim();
  if (!key) return alert('Enter your Gemini API key first.');
  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ gemini_api_key: key }),
  });
  const d = await r.json();
  if (d.ok) {
    document.getElementById('gemini-status').textContent = '✓ API key saved!';
    document.getElementById('gemini-status').className   = 'status-pill ok';
    document.getElementById('s-gemini-key').value = '';
  }
}

async function saveDefaults() {
  await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      default_upload_time: document.getElementById('s-time').value,
      default_privacy:     document.getElementById('s-privacy').value,
    }),
  });
  alert('Defaults saved.');
}


/* ── Helpers ─────────────────────────────────────────────────────────────── */
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}

function el(tag, cls) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

function setText(id, val) {
  const e = document.getElementById(id);
  if (e) e.textContent = val ?? '—';
}

function fmt(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString();
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
