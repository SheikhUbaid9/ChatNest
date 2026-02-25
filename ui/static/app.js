/* ═══════════════════════════════════════════════════
   ChatNest — Frontend Application
   ═══════════════════════════════════════════════════ */

const WS_PROTO = location.protocol === 'https:' ? 'wss' : 'ws';

const API = {
  status:       '/api/status',
  all:          '/api/messages/all',
  gmail:        '/api/messages/gmail',
  slack:        '/api/messages/slack',
  telegram:     '/api/messages/telegram',
  authMe:       '/api/auth/me',
  authLogin:    '/api/auth/login',
  authRegister: '/api/auth/register',
  authLogout:   '/api/auth/logout',
  providers:    '/api/providers/status',
  gmailDisconnect: '/api/providers/gmail/disconnect',
  slackDisconnect: '/api/providers/slack/disconnect',
  unread:       '/api/unread-counts',
  markRead:     '/api/mark-read',
  refresh:      '/api/refresh',
  toolLog:      '/api/tool-log',
  summarize:    '/api/summarize',
  sendReply:    '/api/send-reply',
  draftReply:   '/api/draft-reply',
  ollamaStatus: '/api/ollama/status',
  wsToolLog:    `${WS_PROTO}://${location.host}/ws/tool-log`,
};

/* ── State ─────────────────────────────────────────── */
const state = {
  activeTab:      'all',
  activePlatform: 'all',
  messages:       [],
  expandedCard:   null,
  unreadCounts:   { gmail: 0, slack: 0, telegram: 0 },
  demoMode:       true,
  wsConnected:    false,
  loading:        false,
  replyTarget:    null,
  user:           null,
  providers:      {},
};

/* ── Platform metadata ─────────────────────────────── */
const PLATFORMS = {
  gmail:    { icon: '📧', label: 'Gmail',    color: '#EA4335' },
  slack:    { icon: '💬', label: 'Slack',    color: '#9B59B6' },
  telegram: { icon: '✈️',  label: 'Telegram', color: '#2AABEE' },
};

/* ── DOM refs ──────────────────────────────────────── */
const $ = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);
const MOBILE_BREAKPOINT = 920;
let appBootstrapped = false;

function isMobileView() {
  return window.innerWidth <= MOBILE_BREAKPOINT;
}

function openToolLogDrawer() {
  if (!isMobileView()) return;
  document.body.classList.add('toollog-open');
}

function closeToolLogDrawer() {
  document.body.classList.remove('toollog-open');
}

function toggleToolLogDrawer() {
  if (!isMobileView()) return;
  document.body.classList.toggle('toollog-open');
}

function syncToolLogDrawerLayout() {
  if (!isMobileView()) {
    closeToolLogDrawer();
  }
}

async function bootstrapApp() {
  if (appBootstrapped) return;
  appBootstrapped = true;
  connectWebSocket();
  await loadProviderStatus();
  await loadStatus();
  await loadMessages();
  setInterval(async () => {
    await loadProviderStatus();
    await loadStatus();
  }, 30_000);
}

function formatDisplayName(email = '') {
  const username = (email.split('@')[0] || 'guest').trim();
  const clean = username
    .replace(/[^a-zA-Z0-9._-]/g, ' ')
    .split(/[._\-\s]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(' ');
  return clean || 'Guest';
}

function lockAppWithLogin() {
  const gate = $('login-gate');
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
    state.wsConnected = false;
    updateWsIndicator(false);
  }
  closeToolLogDrawer();
  document.body.classList.add('login-locked');
  if (gate) gate.hidden = false;
}

function unlockAppFromLogin() {
  const gate = $('login-gate');
  document.body.classList.remove('login-locked');
  if (gate) gate.hidden = true;
}

function applyAuthUI(session) {
  state.user = session || null;
  const chip = $('user-chip');
  const label = $('user-label');
  const logout = $('logout-btn');
  if (!chip || !label || !logout) return;

  if (session) {
    label.textContent = session.display_name || session.name || session.email || 'Guest';
    chip.style.display = 'inline-flex';
    logout.style.display = 'inline-flex';
  } else {
    chip.style.display = 'none';
    logout.style.display = 'none';
  }
}

async function completeLogin(session) {
  applyAuthUI(session);
  unlockAppFromLogin();
  try {
    await bootstrapApp();
  } catch (e) {
    console.error('App bootstrap failed after login:', e);
    showToast('Signed in, but failed to load inbox data', 'error');
    return;
  }
  showToast(`Welcome ${session.display_name || session.email || 'Guest'}`, 'success');
}

async function initDemoLoginGate() {
  const gate = $('login-gate');
  if (!gate) {
    await bootstrapApp();
    return;
  }

  const emailInput = $('login-email');
  const passInput = $('login-password');
  const form = $('demo-login-form');
  const googleBtn = $('login-google');
  const appleBtn = $('login-apple');
  const submitBtn = $('login-submit');
  const togglePassBtn = $('login-toggle-password');
  const logoutBtn = $('logout-btn');
  const loginCard = gate.querySelector('.login-card');
  const controls = [emailInput, passInput, submitBtn, togglePassBtn, googleBtn, appleBtn]
    .filter(Boolean);

  const setLoading = (enabled) => {
    if (loginCard) loginCard.classList.remove('is-loading');
    controls.forEach(ctrl => {
      ctrl.disabled = enabled;
    });
  };

  const loginWithTransition = async (session) => {
    setLoading(true);
    try {
      await completeLogin(session);
    } catch (e) {
      console.error('Login flow failed:', e);
      showToast('Unable to sign in right now', 'error');
    } finally {
      setLoading(false);
    }
  };

  try {
    const auth = await apiFetch(API.authMe);
    if (auth?.authenticated && auth.user) {
      applyAuthUI(auth.user);
      unlockAppFromLogin();
      await bootstrapApp();
    } else {
      applyAuthUI(null);
      lockAppWithLogin();
    }
  } catch (e) {
    applyAuthUI(null);
    lockAppWithLogin();
  }

  if (togglePassBtn && passInput) {
    togglePassBtn.addEventListener('click', () => {
      const isHidden = passInput.type === 'password';
      passInput.type = isHidden ? 'text' : 'password';
      togglePassBtn.textContent = isHidden ? 'Hide' : 'Show';
    });
  }

  if (form && emailInput && passInput) {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const email = emailInput.value.trim().toLowerCase();
      const password = passInput.value;

      if (!email || !email.includes('@')) {
        showToast('Enter a valid email address', 'error');
        emailInput.focus();
        return;
      }
      if (!password || password.length < 8) {
        showToast('Password must be at least 8 characters', 'error');
        passInput.focus();
        return;
      }

      setLoading(true);
      try {
        let result = null;
        try {
          result = await apiFetch(API.authLogin, {
            method: 'POST',
            body: JSON.stringify({ email, password }),
          });
        } catch (loginErr) {
          if (loginErr.status === 404) {
            await apiFetch(API.authRegister, {
              method: 'POST',
              body: JSON.stringify({
                email,
                password,
                display_name: formatDisplayName(email),
              }),
            });
            result = await apiFetch(API.authLogin, {
              method: 'POST',
              body: JSON.stringify({ email, password }),
            });
          } else {
            throw loginErr;
          }
        }
        if (result?.user) {
          await loginWithTransition(result.user);
        } else {
          showToast('Login failed', 'error');
        }
      } catch (e) {
        showToast(e?.message || 'Unable to sign in', 'error');
      } finally {
        setLoading(false);
      }
    });
  }

  if (googleBtn) {
    googleBtn.addEventListener('click', () => {
      showToast('Use email/password to sign in, then connect Gmail from the Gmail status pill.', 'success');
    });
  }

  if (appleBtn) {
    appleBtn.addEventListener('click', () => {
      showToast('Apple login is not implemented in this branch yet.', 'error');
    });
  }

  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      try { await apiFetch(API.authLogout, { method: 'POST' }); } catch (e) {}
      applyAuthUI(null);
      state.providers = {};
      appBootstrapped = false;
      setLoading(false);
      lockAppWithLogin();
      if (emailInput) emailInput.focus();
      showToast('Signed out', 'success');
    });
  }
}

/* ══════════════════════════════════════════════════
   INIT
   ══════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', async () => {
  setupEventListeners();
  await initDemoLoginGate();
  const gmailFlag = consumeQueryFlag('gmail');
  const slackFlag = consumeQueryFlag('slack');
  if (gmailFlag === 'connected') {
    await loadProviderStatus();
    await loadStatus();
    await loadMessages();
    showToast('Gmail connected', 'success');
  } else if (slackFlag === 'connected') {
    await loadProviderStatus();
    await loadStatus();
    await loadMessages();
    showToast('Slack connected', 'success');
  }
});

/* ══════════════════════════════════════════════════
   API HELPERS
   ══════════════════════════════════════════════════ */
async function apiFetch(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const res = await fetch(url, {
    credentials: 'same-origin',
    headers,
    ...options,
  });
  const ct = res.headers.get('content-type') || '';
  let payload = null;
  if (ct.includes('application/json')) {
    try { payload = await res.json(); } catch (e) { payload = null; }
  }
  if (!res.ok) {
    const err = new Error(payload?.detail || payload?.message || `HTTP ${res.status}`);
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

/* ══════════════════════════════════════════════════
   STATUS / UNREAD COUNTS
   ══════════════════════════════════════════════════ */
async function loadProviderStatus() {
  try {
    const data = await apiFetch(API.providers);
    state.providers = data || {};
    renderProviderActions();
  } catch (e) {
    if (e?.status === 401) {
      applyAuthUI(null);
      appBootstrapped = false;
      lockAppWithLogin();
      return;
    }
    console.warn('Provider status fetch failed:', e);
  }
}

async function loadStatus() {
  try {
    const data = await apiFetch(API.status);
    state.demoMode = data.demo_mode;
    state.unreadCounts = {
      gmail:    data.platforms.gmail.unread,
      slack:    data.platforms.slack.unread,
      telegram: data.platforms.telegram.unread,
    };
    if (data.user) {
      applyAuthUI(data.user);
    }
    renderStatus(data);
    renderSidebarCounts();
  } catch (e) {
    if (e?.status === 401) {
      applyAuthUI(null);
      appBootstrapped = false;
      lockAppWithLogin();
      return;
    }
    console.warn('Status fetch failed:', e);
  }
}

function renderStatus(data) {
  // Demo badge
  const demoBadge = $('demo-badge');
  if (demoBadge) demoBadge.style.display = data.demo_mode ? 'inline-flex' : 'none';

  // Status pills in header
  for (const [platform, info] of Object.entries(data.platforms)) {
    const pill = $(`status-${platform}`);
    if (!pill) continue;
    const dot = pill.querySelector('.status-dot');
    if (dot) {
      dot.className = 'status-dot ' + (info.connected ? 'connected' : 'demo');
    }
    const label = pill.querySelector('.status-label');
    if (label) label.textContent = info.connected ? 'Live' : 'Demo';
  }

  renderProviderActions();
}

function renderProviderActions() {
  const gmailPill = $('status-gmail');
  if (gmailPill) {
    const gmail = state.providers.gmail || {};
    const connected = !!gmail.connected;
    gmailPill.dataset.connected = connected ? '1' : '0';
    gmailPill.style.cursor = 'pointer';
    gmailPill.title = connected
      ? `Gmail connected${gmail.account_email ? ` as ${gmail.account_email}` : ''}. Click to disconnect.`
      : 'Click to connect Gmail';
  }

  const slackPill = $('status-slack');
  if (slackPill) {
    const slack = state.providers.slack || {};
    const connected = !!slack.connected;
    slackPill.dataset.connected = connected ? '1' : '0';
    slackPill.style.cursor = 'pointer';
    slackPill.title = connected
      ? `Slack connected${slack.workspace ? ` to ${slack.workspace}` : ''}. Click to disconnect.`
      : 'Click to connect Slack';
  }
}

function renderSidebarCounts() {
  const total = Object.values(state.unreadCounts).reduce((a, b) => a + b, 0);

  for (const platform of ['gmail', 'slack', 'telegram']) {
    const badge = $(`badge-${platform}`);
    if (!badge) continue;
    const count = state.unreadCounts[platform];
    badge.textContent = count;
    badge.style.display = count > 0 ? 'flex' : 'none';
  }

  const totalEl = $('total-unread');
  if (totalEl) totalEl.textContent = total;

  // All badge
  const allBadge = $('badge-all');
  if (allBadge) {
    allBadge.textContent = total;
    allBadge.style.display = total > 0 ? 'flex' : 'none';
  }
}

/* ══════════════════════════════════════════════════
   MESSAGES
   ══════════════════════════════════════════════════ */
async function loadMessages(platform = state.activeTab) {
  state.loading = true;
  showSkeletons();

  try {
    const url = platform === 'all' ? API.all : API[platform];
    const data = await apiFetch(url + '?limit=50');
    state.messages = data.messages || [];
    renderMessages();
  } catch (e) {
    if (e?.status === 401) {
      applyAuthUI(null);
      appBootstrapped = false;
      lockAppWithLogin();
      return;
    }
    console.error('Messages fetch failed:', e);
    showError('Failed to load messages');
  } finally {
    state.loading = false;
  }
}

function renderMessages() {
  const list = $('messages-list');
  if (!list) return;

  if (state.messages.length === 0) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📭</div>
        <div class="empty-state-title">All caught up!</div>
        <div class="empty-state-sub">No messages in this view.</div>
      </div>`;
    return;
  }

  list.innerHTML = state.messages.map((msg, i) => buildCard(msg, i)).join('');
}

function buildCard(msg, index) {
  const p = msg.platform;
  const meta = PLATFORMS[p] || { icon: '💌', label: p };
  const initials = getInitials(msg.sender);
  const timeStr = relativeTime(msg.timestamp);
  const subject = msg.subject || msg.channel || '';
  const preview = msg.preview || '';
  const body = msg.body || preview;
  const isUnread = msg.is_unread;

  return `
    <div class="message-card ${p} ${isUnread ? 'unread' : ''}" data-id="${esc(msg.id)}">
      <div class="card-header">
        <div class="avatar ${p}">${initials}</div>
        <div class="card-meta">
          <div class="card-top">
            <span class="sender-name">${esc(msg.sender)}</span>
            <div class="card-right">
              <span class="platform-tag ${p}">${meta.icon} ${meta.label}</span>
              <span class="timestamp">${timeStr}</span>
              ${isUnread ? '<span class="unread-dot"></span>' : ''}
            </div>
          </div>
          ${subject ? `<div class="subject-line">${esc(subject)}</div>` : ''}
          <div class="preview-text">${esc(preview)}</div>
        </div>
      </div>
      <div class="card-body" id="body-${esc(msg.id)}">
        <div class="message-body-text">${esc(body)}</div>
        <div class="card-actions">
          <button class="btn primary btn-reply" data-id="${esc(msg.id)}">↩ Reply</button>
          <button class="btn success btn-read" data-id="${esc(msg.id)}">✓ Mark Read</button>
          <button class="btn btn-summarize" data-id="${esc(msg.id)}">✦ Summarize</button>
        </div>
      </div>
    </div>`;
}

function findCard(id) {
  return Array.from($$('.message-card')).find(c => c.dataset.id === id) || null;
}

function toggleCard(id) {
  const card = findCard(id);
  if (!card) return;
  const body = card.querySelector('.card-body');
  if (!body) return;

  const isExpanded = card.dataset.expanded === '1';

  // Collapse any other expanded card
  $$('.message-card').forEach(c => {
    if (c.dataset.expanded === '1') {
      c.dataset.expanded = '';
      c.classList.remove('expanded');
      const b = c.querySelector('.card-body');
      if (b) b.style.display = 'none';
    }
  });

  if (!isExpanded) {
    card.dataset.expanded = '1';
    card.classList.add('expanded');
    body.style.display = 'block';
    state.expandedCard = id;
  } else {
    state.expandedCard = null;
  }
}

/* ══════════════════════════════════════════════════
   ACTIONS
   ══════════════════════════════════════════════════ */
async function doMarkRead(msgId) {
  try {
    await apiFetch(API.markRead, {
      method: 'POST',
      body: JSON.stringify({ message_id: msgId }),
    });

    // Update UI
    const card = findCard(msgId);
    if (card) {
      card.classList.remove('unread');
      const dot = card.querySelector('.unread-dot');
      if (dot) dot.remove();
    }

    // Decrement sidebar badge
    const msg = state.messages.find(m => m.id === msgId);
    if (msg && msg.is_unread) {
      msg.is_unread = false;
      state.unreadCounts[msg.platform] = Math.max(0, (state.unreadCounts[msg.platform] || 1) - 1);
      renderSidebarCounts();
    }

    showToast('✓ Marked as read', 'success');
  } catch (e) {
    showToast('Failed to mark as read', 'error');
  }
}

async function doSummarize(msgId) {
  const msg = state.messages.find(m => m.id === msgId);
  if (!msg) return;

  // Always expand the card so the summary is visible
  const card = findCard(msgId);
  const cardBody = card?.querySelector('.card-body');
  if (!card || !cardBody) return;

  // Expand if collapsed
  if (card.dataset.expanded !== '1') {
    $$('.message-card').forEach(c => {
      if (c.dataset.expanded === '1') {
        c.dataset.expanded = '';
        c.classList.remove('expanded');
        const b = c.querySelector('.card-body');
        if (b) b.style.display = 'none';
      }
    });
    card.dataset.expanded = '1';
    card.classList.add('expanded');
    cardBody.style.display = 'block';
  }

  // Show loading placeholder where summary will appear
  cardBody.querySelector('.summary-result')?.remove();
  const placeholder = document.createElement('div');
  placeholder.className = 'summary-result';
  placeholder.innerHTML = `
    <div class="summary-box summary-loading">
      <div class="summary-header">
        <span class="summary-icon">✦</span>
        <span class="summary-label">AI Summary</span>
        <span class="summary-model" style="margin-left:auto;opacity:0.5">thinking…</span>
      </div>
      <div class="summary-text" style="color:var(--muted)">
        <span class="calling-pulse">Summarizing with AI…</span>
      </div>
    </div>`;
  cardBody.insertBefore(placeholder, cardBody.querySelector('.card-actions'));

  const body = msg.body || msg.preview || '(no content)';

  try {
    const data = await apiFetch(API.summarize, {
      method: 'POST',
      body: JSON.stringify({
        message_id: msg.id,
        platform:   msg.platform,
        sender:     msg.sender || '',
        body:       body,
      }),
    });

    // Replace placeholder with real summary
    cardBody.querySelector('.summary-result')?.remove();
    const div = document.createElement('div');
    div.className = 'summary-result';
    div.innerHTML = `
      <div class="summary-box">
        <div class="summary-header">
          <span class="summary-icon">✦</span>
          <span class="summary-label">AI Summary</span>
          <span class="summary-model">${esc(data.model || 'ollama')}</span>
          ${!data.ollama_running ? '<span class="summary-fallback">extractive</span>' : ''}
        </div>
        <div class="summary-text">${esc(data.summary)}</div>
      </div>`;
    cardBody.insertBefore(div, cardBody.querySelector('.card-actions'));

    const label = data.ollama_running
      ? `✦ Summarized with ${data.model}`
      : '✦ Summary ready (AI offline — extractive)';
    showToast(label, 'success');

  } catch (e) {
    cardBody.querySelector('.summary-result')?.remove();
    showToast('Summarization failed', 'error');
  }
}

/* ══════════════════════════════════════════════════
   REPLY MODAL
   ══════════════════════════════════════════════════ */
function openReplyModal(msgId) {
  const msg = state.messages.find(m => m.id === msgId);
  if (!msg) return;

  state.replyTarget = msg;

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'reply-modal';

  const to = msg.sender_email || msg.sender || '';
  const subject = msg.subject || '';

  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-title">↩ Reply to ${esc(msg.sender)}</div>
      <div class="modal-field">
        <label class="modal-label">To</label>
        <input class="modal-input" id="reply-to" value="${esc(to)}" placeholder="recipient@example.com" />
      </div>
      ${subject ? `
      <div class="modal-field">
        <label class="modal-label">Subject</label>
        <input class="modal-input" id="reply-subject" value="Re: ${esc(subject)}" />
      </div>` : ''}
      <div class="modal-field">
        <label class="modal-label">Message</label>
        <textarea class="modal-textarea" id="reply-body" placeholder="Write your reply… or click ✦ AI Draft"></textarea>
      </div>
      <div class="modal-actions">
        <button class="btn" id="reply-cancel">Cancel</button>
        <button class="btn" id="reply-ai-draft">✦ AI Draft</button>
        <button class="btn primary" id="reply-send">↩ Send Reply</button>
      </div>
    </div>`;

  document.body.appendChild(overlay);

  $('reply-cancel').addEventListener('click', closeModal);
  overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
  $('reply-send').addEventListener('click', sendReply);
  $('reply-ai-draft').addEventListener('click', aiDraftReply);

  setTimeout(() => { const el = $('reply-body'); if (el) el.focus(); }, 50);
}

function closeModal() {
  const modal = $('reply-modal');
  if (modal) modal.remove();
  state.replyTarget = null;
}

async function aiDraftReply() {
  const msg = state.replyTarget;
  if (!msg) return;

  const draftBtn = $('reply-ai-draft');
  if (draftBtn) { draftBtn.textContent = '✦ Drafting…'; draftBtn.disabled = true; }

  try {
    const data = await apiFetch(API.draftReply, {
      method: 'POST',
      body: JSON.stringify({
        original_body: msg.body || msg.preview || '',
        platform:      msg.platform,
        sender:        msg.sender_email || msg.sender || '',
      }),
    });

    const textarea = $('reply-body');
    if (textarea) {
      if (data.draft) {
        textarea.value = data.draft;
        const label = data.ollama_running
          ? `✦ AI draft ready (${data.model})`
          : '✦ Draft ready (template fallback)';
        showToast(label, 'success');
      } else {
        showToast(data.message || 'AI draft unavailable', 'error');
      }
    }
  } catch (e) {
    showToast('AI draft failed', 'error');
  } finally {
    if (draftBtn) { draftBtn.textContent = '✦ AI Draft'; draftBtn.disabled = false; }
  }
}

async function sendReply() {
  const msg = state.replyTarget;
  if (!msg) return;

  const body = ($('reply-body')?.value || '').trim();
  if (!body) { showToast('Please write a message', 'error'); return; }

  const subject = ($('reply-subject')?.value || msg.subject || '').trim();

  const sendBtn = $('reply-send');
  if (sendBtn) { sendBtn.textContent = 'Sending…'; sendBtn.disabled = true; }

  try {
    const data = await apiFetch(API.sendReply, {
      method: 'POST',
      body: JSON.stringify({
        message_id:   msg.id,
        platform:     msg.platform,
        thread_id:    msg.thread_id || '',
        sender_email: msg.sender_email || '',
        subject:      subject,
        channel:      msg.channel || '',
        chat_id:      String(msg.chat_id || ''),
        body:         body,
      }),
    });

    const demoNote = data.demo_mode ? ' (Demo Mode)' : '';
    showToast(`✓ Reply sent${demoNote}`, 'success');
    closeModal();
    await doMarkRead(msg.id);
  } catch (e) {
    showToast('Failed to send reply', 'error');
    if (sendBtn) { sendBtn.textContent = '↩ Send Reply'; sendBtn.disabled = false; }
  }
}

/* ══════════════════════════════════════════════════
   TOOL LOG (WebSocket)
   ══════════════════════════════════════════════════ */
let ws = null;
let wsReconnectTimer = null;

function connectWebSocket() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  try {
    ws = new WebSocket(API.wsToolLog);

    ws.onopen = () => {
      state.wsConnected = true;
      updateWsIndicator(true);
      if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'snapshot') {
          renderToolLogSnapshot(data.entries || []);
        } else if (data.type === 'tool_log') {
          prependLogEntry(data.entry);
        }
        // ignore ping
      } catch (e) { /* ignore parse errors */ }
    };

    ws.onclose = () => {
      state.wsConnected = false;
      updateWsIndicator(false);
      // Reconnect after 3s
      wsReconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = () => { ws.close(); };

  } catch (e) {
    // WebSocket not available — fall back to polling
    setInterval(pollToolLog, 2000);
  }
}

async function pollToolLog() {
  try {
    const data = await apiFetch(API.toolLog + '?limit=20');
    renderToolLogSnapshot(data.entries || []);
  } catch (e) { /* silent */ }
}

function updateWsIndicator(connected) {
  const dot = $('ws-dot');
  if (dot) dot.className = 'ws-indicator ' + (connected ? 'connected' : '');
}

function renderToolLogSnapshot(entries) {
  const list = $('tool-log-list');
  if (!list) return;
  list.innerHTML = entries.map(buildLogEntry).join('');
  updateLogCount(entries.length);
}

function prependLogEntry(entry) {
  const list = $('tool-log-list');
  if (!list) return;

  const el = document.createElement('div');
  el.innerHTML = buildLogEntry(entry);
  const node = el.firstElementChild;
  list.insertBefore(node, list.firstChild);

  // Keep max 30 entries
  while (list.children.length > 30) list.removeChild(list.lastChild);

  updateLogCount(list.children.length);
}

function buildLogEntry(entry) {
  const status = entry.status || 'calling';
  const icon = status === 'done'    ? '✅'
             : status === 'error'   ? '❌'
             : '<span class="calling-pulse">⚡</span>';

  const platform = entry.platform || '';
  const platformTag = platform
    ? `<span class="log-platform-tag ${platform}">${platform}</span>`
    : '';

  const duration = entry.duration_ms != null
    ? `<span class="log-duration">${entry.duration_ms}ms</span>`
    : '';

  const summary = esc(entry.result_summary || (status === 'calling' ? 'Calling…' : ''));
  const time = relativeTime(entry.called_at);

  return `
    <div class="log-entry status-${status}">
      <div class="log-entry-header">
        <span class="log-status-icon">${icon}</span>
        <span class="log-tool-name">${esc(entry.tool_name)}</span>
        ${platformTag}
      </div>
      <div class="log-entry-footer">
        <span class="log-summary">${summary}</span>
        ${duration}
      </div>
    </div>`;
}

function updateLogCount(n) {
  const el = $('log-count');
  if (el) el.textContent = n + ' calls';
}

async function toggleGmailConnection() {
  if (!state.user) {
    lockAppWithLogin();
    return;
  }

  const gmail = state.providers.gmail || {};
  if (gmail.connected) {
    try {
      await apiFetch(API.gmailDisconnect, { method: 'POST' });
      await loadProviderStatus();
      await loadStatus();
      await loadMessages();
      showToast('Gmail disconnected', 'success');
    } catch (e) {
      showToast(e?.message || 'Failed to disconnect Gmail', 'error');
    }
    return;
  }

  const connectUrl = gmail.connect_url || '/auth/google/start?redirect=/';
  window.location.href = connectUrl;
}

async function toggleSlackConnection() {
  if (!state.user) {
    lockAppWithLogin();
    return;
  }

  const slack = state.providers.slack || {};
  if (slack.connected) {
    try {
      await apiFetch(API.slackDisconnect, { method: 'POST' });
      await loadProviderStatus();
      await loadStatus();
      await loadMessages();
      showToast('Slack disconnected', 'success');
    } catch (e) {
      showToast(e?.message || 'Failed to disconnect Slack', 'error');
    }
    return;
  }

  const connectUrl = slack.connect_url || '/auth/slack/start?redirect=/';
  window.location.href = connectUrl;
}

/* ══════════════════════════════════════════════════
   EVENT LISTENERS
   ══════════════════════════════════════════════════ */
function setupEventListeners() {
  // Single delegated handler for all message card interactions (attached once)
  const list = $('messages-list');
  if (list) {
    list.addEventListener('click', e => {
      const btn = e.target.closest('.btn');
      if (btn) {
        if (btn.classList.contains('btn-reply'))     { openReplyModal(btn.dataset.id); return; }
        if (btn.classList.contains('btn-read'))      { doMarkRead(btn.dataset.id);     return; }
        if (btn.classList.contains('btn-summarize')) { doSummarize(btn.dataset.id);    return; }
        return;
      }
      const card = e.target.closest('.message-card');
      if (card) toggleCard(card.dataset.id);
    });
  }

  // Tab bar clicks
  $$('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const platform = tab.dataset.platform;
      $$('.tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      state.activeTab = platform;
      loadMessages(platform);
      closeToolLogDrawer();
    });
  });

  // Sidebar platform buttons
  $$('.platform-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const platform = btn.dataset.platform;
      $$('.platform-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activePlatform = platform;

      // Sync tab bar
      $$('.tab').forEach(t => {
        t.classList.toggle('active', t.dataset.platform === platform);
      });

      state.activeTab = platform;
      loadMessages(platform);
      closeToolLogDrawer();
    });
  });

  // Refresh button
  const refreshBtn = $('refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      closeToolLogDrawer();
      refreshBtn.classList.add('spinning');
      try {
        await apiFetch(API.refresh, { method: 'POST' });
        await loadProviderStatus();
        await loadStatus();
        await loadMessages();
        showToast('✓ Refreshed all platforms', 'success');
      } catch (e) {
        showToast('Refresh failed', 'error');
      } finally {
        refreshBtn.classList.remove('spinning');
      }
    });
  }

  const gmailPill = $('status-gmail');
  if (gmailPill) {
    gmailPill.addEventListener('click', toggleGmailConnection);
  }
  const slackPill = $('status-slack');
  if (slackPill) {
    slackPill.addEventListener('click', toggleSlackConnection);
  }

  // Mobile tool-log drawer
  const toolLogToggle = $('toollog-toggle');
  const toolLogBackdrop = $('toollog-backdrop');

  if (toolLogToggle) {
    toolLogToggle.addEventListener('click', toggleToolLogDrawer);
  }

  if (toolLogBackdrop) {
    toolLogBackdrop.addEventListener('click', closeToolLogDrawer);
  }

  window.addEventListener('resize', syncToolLogDrawerLayout);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeToolLogDrawer();
  });
  syncToolLogDrawerLayout();
}

/* ══════════════════════════════════════════════════
   SKELETON LOADERS
   ══════════════════════════════════════════════════ */
function showSkeletons(count = 5) {
  const list = $('messages-list');
  if (!list) return;
  list.innerHTML = Array.from({ length: count }, () => `
    <div class="skeleton-card">
      <div class="skeleton skeleton-avatar"></div>
      <div class="skeleton-content">
        <div class="skeleton skeleton-line w-60"></div>
        <div class="skeleton skeleton-line w-80"></div>
        <div class="skeleton skeleton-line w-40"></div>
      </div>
    </div>`).join('');
}

/* ══════════════════════════════════════════════════
   TOAST NOTIFICATIONS
   ══════════════════════════════════════════════════ */
function showToast(message, type = '') {
  let container = $('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    container.id = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('leaving');
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

function showError(msg) {
  const list = $('messages-list');
  if (list) list.innerHTML = `
    <div class="empty-state">
      <div class="empty-state-icon">⚠️</div>
      <div class="empty-state-title">Something went wrong</div>
      <div class="empty-state-sub">${esc(msg)}</div>
    </div>`;
}

/* ══════════════════════════════════════════════════
   UTILITIES
   ══════════════════════════════════════════════════ */
function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function getInitials(name) {
  if (!name) return '?';
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.substring(0, 2).toUpperCase();
}

function relativeTime(isoStr) {
  if (!isoStr) return '';
  const now = Date.now();
  const then = new Date(isoStr).getTime();
  const diff = Math.floor((now - then) / 1000);

  if (diff < 5)   return 'just now';
  if (diff < 60)  return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function consumeQueryFlag(name) {
  const url = new URL(window.location.href);
  const value = url.searchParams.get(name);
  if (value != null) {
    url.searchParams.delete(name);
    const clean = url.pathname + (url.search ? url.search : '') + (url.hash || '');
    window.history.replaceState({}, '', clean);
  }
  return value;
}
