
// ══════════════════════════════════════════
// CONFIG — update BASE_URL to your backend
// ══════════════════════════════════════════
const BASE_URL = 'http://127.0.0.1:8000';
const API = {
  register:   BASE_URL + '/auth/register',
  login:      BASE_URL + '/auth/login',
  me:         BASE_URL + '/auth/me',
  sentiment:  BASE_URL + '/sentiment',
  face:       BASE_URL + '/face',
  voice:      BASE_URL + '/voice',
  chat:       BASE_URL + '/chat',
  report:     BASE_URL + '/generate-report',
  reports:    BASE_URL + '/reports',
  sessions:   BASE_URL + '/sessions',
  sessionMsgs: (sid) => BASE_URL + '/sessions/' + sid + '/messages',
  reset:      BASE_URL + '/reset',
  findDoctors:BASE_URL + '/find-doctors',
  lastSession:(uid)  => BASE_URL + '/last-session/' + uid,
};

// ══════════════════════════════════════════
// STATE
// ══════════════════════════════════════════
let authToken   = localStorage.getItem('ms_token') || '';
let currentUser = JSON.parse(localStorage.getItem('ms_user') || 'null');
let sessionId   = localStorage.getItem('ms_session_id') || '';
let faceEmo     = 'unknown';
let lastSent    = 'Neutral';
let voiceEmo    = 'not_used';
let msgCount    = 0;
let negCount    = 0;
let isSending   = false;
let chatLoaded  = false;

// Camera / Mic state
let cameraStream    = null;
let cameraOn        = false;
let faceInterval    = null;
let recognition     = null;
let micOn           = false;

// ══════════════════════════════════════════
// THEME
// ══════════════════════════════════════════
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  document.getElementById('themeBtn').textContent = theme === 'dark' ? '🌙' : '☀️';
  localStorage.setItem('ms_theme', theme);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}
// Load saved theme on start
applyTheme(localStorage.getItem('ms_theme') || 'dark');

// ══════════════════════════════════════════
// API FETCH HELPER
// ══════════════════════════════════════════
async function apiFetch(url, opts = {}, retries = 2) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, { ...opts, headers });
      if (res.status === 401) {
        toast('Session expired — please log in again.', '#f04060');
        doLogout(); throw new Error('Unauthorised');
      }
      return res;
    } catch (e) {
      if (e.message === 'Unauthorised') throw e;
      if (attempt < retries) await new Promise(r => setTimeout(r, 800 * (attempt + 1)));
      else throw e;
    }
  }
}

// ══════════════════════════════════════════
// AUTH
// ══════════════════════════════════════════
function switchTab(t) {
  document.querySelectorAll('.tab-btn').forEach((b, i) => b.classList.toggle('active', (i === 0) === (t === 'login')));
  document.getElementById('loginForm').style.display  = t === 'login'  ? 'block' : 'none';
  document.getElementById('signupForm').style.display = t === 'signup' ? 'block' : 'none';
}

async function doLogin() {
  const u = v('li_user'), p = v('li_pass'), err = document.getElementById('li_err');
  if (!u || !p) { err.textContent = 'Please fill all fields.'; return; }
  setAuthBtnLoading('li_btn', true);
  try {
    const res  = await fetch(API.login, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:u,password:p}) });
    const data = await res.json();
    if (!res.ok) { err.textContent = data.detail || 'Login failed.'; return; }
    persistAuth(data.token, data.user); startApp();
  } catch (e) { err.textContent = 'Cannot reach server. Is the backend running?'; }
  finally { setAuthBtnLoading('li_btn', false); }
}

async function doSignup() {
  const name = v('su_name'), email = v('su_email'), user = v('su_user'), pass = v('su_pass');
  const err  = document.getElementById('su_err');
  if (!name || !email || !user || !pass) { err.textContent = 'Fill all fields.'; return; }
  if (pass.length < 6) { err.textContent = 'Password min 6 characters.'; return; }
  setAuthBtnLoading('su_btn', true);
  try {
    const res  = await fetch(API.register, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name,email,username:user,password:pass}) });
    const data = await res.json();
    if (!res.ok) { err.textContent = data.detail || 'Registration failed.'; return; }
    persistAuth(data.token, data.user); startApp();
  } catch (e) { err.textContent = 'Cannot reach server. Is the backend running?'; }
  finally { setAuthBtnLoading('su_btn', false); }
}

function persistAuth(token, user) {
  authToken = token; currentUser = user;
  localStorage.setItem('ms_token', token);
  localStorage.setItem('ms_user', JSON.stringify(user));
}

function doLogout() {
  if (!confirm('Sign out?')) return;
  stopCamera(); stopMic();
  authToken = ''; currentUser = null;
  localStorage.removeItem('ms_token'); localStorage.removeItem('ms_user');
  location.reload();
}

window.addEventListener('load', async () => {
  if (!authToken || !currentUser) return;
  try {
    const res = await apiFetch(API.me);
    if (!res.ok) { localStorage.removeItem('ms_token'); return; }
    startApp();
  } catch {}
});

// ══════════════════════════════════════════
// START APP
// ══════════════════════════════════════════
async function startApp() {
  document.getElementById('loginScreen').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  const name = currentUser.name || currentUser.username || 'User';
  document.getElementById('hdrAvatar').textContent = name[0].toUpperCase();
  document.getElementById('hdrName').textContent   = name.split(' ')[0];
  await startCamera();
  await restoreOrCreateSession();
  updateStats();
}

// ══════════════════════════════════════════
// SESSION RESTORE — FIXED HISTORY LOADING
// ══════════════════════════════════════════
async function restoreOrCreateSession() {
  const box = document.getElementById('chatBox');

  // 1. Try localStorage session first
  if (sessionId) {
    try {
      const res = await apiFetch(API.sessionMsgs(sessionId), {}, 1);
      if (res.ok) {
        const msgs = await res.json();
        if (msgs.length > 0) {
          renderHistoryMsgs(box, msgs, `Session resumed · ${new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}`);
          return;
        }
      }
    } catch {}
  }

  // 2. Try last session from server
  try {
    const res = await apiFetch(API.lastSession(currentUser.id), {}, 1);
    if (res.ok) {
      const data = await res.json();
      if (data.session_id) {
        sessionId = data.session_id;
        localStorage.setItem('ms_session_id', sessionId);
        const msgRes = await apiFetch(API.sessionMsgs(sessionId), {}, 1);
        if (msgRes.ok) {
          const msgs = await msgRes.json();
          if (msgs.length > 0) {
            renderHistoryMsgs(box, msgs, `Last session restored · ${new Date(data.started_at).toLocaleString()}`);
            return;
          }
        }
      }
    }
  } catch {}

  // 3. Fresh session
  sessionId = (currentUser.username || 'user') + '_' + Date.now();
  localStorage.setItem('ms_session_id', sessionId);
  box.innerHTML = '<div class="sys-msg">New session started · Say hello 👋</div>';
  chatLoaded = true;
}

function renderHistoryMsgs(box, msgs, sysLabel) {
  // Prevent duplicates — only render once
  if (chatLoaded) return;
  box.innerHTML = `<div class="sys-msg">${escHtml(sysLabel)}</div>`;
  msgs.forEach(m => {
    if (m.role === 'user' || m.role === 'assistant')
      addMsg(m.content, m.role === 'user' ? 'user' : 'bot', m.created_at, false);
  });
  msgCount = msgs.filter(m => m.role === 'user').length;
  chatLoaded = true;
  scrollChat();
  updateStats();
  toast('Session restored ✓');
}

// ══════════════════════════════════════════
// CAMERA — TOGGLE ON/OFF
// ══════════════════════════════════════════
async function startCamera() {
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    document.getElementById('video').srcObject = cameraStream;
    cameraOn = true;
    updateCamUI();
    faceInterval = setInterval(analyzeFace, 5000);
  } catch {
    document.getElementById('elFace').textContent = 'No camera';
    cameraOn = false;
    updateCamUI();
  }
}

function stopCamera() {
  if (faceInterval) { clearInterval(faceInterval); faceInterval = null; }
  if (cameraStream) { cameraStream.getTracks().forEach(t => t.stop()); cameraStream = null; }
  const video = document.getElementById('video');
  video.srcObject = null;
  cameraOn = false;
  updateCamUI();
}

async function toggleCamera() {
  if (cameraOn) {
    stopCamera();
    document.getElementById('elFace').textContent = 'Camera off';
  } else {
    await startCamera();
  }
}

function updateCamUI() {
  const btn  = document.getElementById('camTogBtn');
  const wrap = document.getElementById('camWrap');
  const dot  = document.getElementById('camDot');
  btn.classList.toggle('on', cameraOn);
  btn.textContent = cameraOn ? '📷 Cam On' : '📷 Cam Off';
  wrap.classList.toggle('cam-off', !cameraOn);
  dot.classList.toggle('off', !cameraOn);
}

async function analyzeFace() {
  if (!cameraOn) return;
  const video = document.getElementById('video');
  if (!video.videoWidth) return;
  const c = document.createElement('canvas');
  c.width = video.videoWidth; c.height = video.videoHeight;
  c.getContext('2d').drawImage(video, 0, 0);
  c.toBlob(async blob => {
    const fd = new FormData();
    fd.append('file', blob, 'frame.jpg');
    try {
      const res  = await fetch(API.face, { method:'POST', body:fd });
      const data = await res.json();
      faceEmo = data.emotion || 'unknown';
      document.getElementById('elFace').textContent = faceEmo;
    } catch {}
  }, 'image/jpeg', 0.85);
}

// ══════════════════════════════════════════
// MIC — SPEECH RECOGNITION TOGGLE
// ══════════════════════════════════════════
function toggleMic() {
  if (micOn) { stopMic(); return; }
  try {
    recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
    recognition.lang = 'en-IN'; recognition.interimResults = false;
    recognition.start();
    micOn = true;
    updateMicUI();
    recognition.onresult = e => {
      document.getElementById('userInput').value = e.results[0][0].transcript;
      voiceEmo = 'voice_used';
      document.getElementById('elVoice').textContent = 'Captured';
    };
    recognition.onerror = e => { toast('Mic error: ' + e.error, '#f04060'); stopMic(); };
    recognition.onend  = () => stopMic();
  } catch { toast('Speech recognition not supported in this browser.'); }
}

function stopMic() {
  recognition?.stop(); recognition = null;
  micOn = false;
  updateMicUI();
  document.getElementById('elVoice').textContent = 'Idle';
}

function updateMicUI() {
  const btn = document.getElementById('micTogBtn');
  btn.classList.toggle('recording', micOn);
  btn.classList.toggle('on', false);
  btn.textContent = micOn ? '⏹ Recording' : '🎤 Mic';
}

// ══════════════════════════════════════════
// SEND CHAT MESSAGE
// ══════════════════════════════════════════
async function sendMessage() {
  if (isSending) return;
  const input = document.getElementById('userInput');
  const text  = input.value.trim();
  if (!text) return;

  addMsg(text, 'user');
  input.value = '';
  isSending = true;
  document.getElementById('sendBtn').disabled = true;
  showTyping(true);

  try {
    // 1 — Sentiment (text preprocessing happens on backend)
    try {
      const sRes  = await fetch(API.sentiment, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text}) });
      if (sRes.ok) {
        const sData = await sRes.json();
        lastSent = sData.prediction || 'Neutral';
        updateSentimentUI(lastSent);
      }
    } catch {}

    // 2 — Chat
    const cRes = await apiFetch(API.chat, {
      method: 'POST',
      body: JSON.stringify({ text, sentiment:lastSent, face:faceEmo, voice:voiceEmo, session_id:sessionId })
    });
    if (!cRes.ok) {
      const err = await cRes.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${cRes.status}`);
    }
    const cData = await cRes.json();
    showTyping(false);
    addMsg(cData.reply || 'No response', 'bot');
    msgCount++;
    if (lastSent === 'Negative') negCount++;
    updateStats();

  } catch (e) {
    showTyping(false);
    addMsg('⚠️ ' + (e.message || 'Connection issue'), 'bot');
    toast(e.message || 'Failed to send message', '#f04060');
  } finally {
    isSending = false;
    document.getElementById('sendBtn').disabled = false;
  }
}

// ══════════════════════════════════════════
// GENERATE REPORT
// ══════════════════════════════════════════
async function generateReport() {
  gotoView('report');
  const spinner = document.getElementById('rptSpinner');
  const content = document.getElementById('rptContent');
  const meta    = document.getElementById('rptMeta');
  spinner.classList.add('show'); content.innerHTML = ''; meta.textContent = 'Analysing your session…';

  try {
    const res = await apiFetch(API.report, { method:'POST', body:JSON.stringify({session_id:sessionId}) });
    if (!res.ok) { const e = await res.json().catch(()=>{}); throw new Error(e?.detail || `HTTP ${res.status}`); }
    const data = await res.json();
    spinner.classList.remove('show');
    renderReport(data, content, meta);
    updateSeverityUI(data.severity);
    fillSuggestions(data.suggestions || []);
    toast('Report generated ✓');
  } catch (e) {
    spinner.classList.remove('show');
    content.innerHTML = `<div class="empty"><div class="empty-ico">⚠️</div>${escHtml(e.message)}</div>`;
    meta.textContent = 'Error'; toast(e.message, '#f04060');
  }
}

function renderReport(data, container, meta) {
  meta.textContent = 'Generated · ' + new Date().toLocaleString();
  const sev = data.severity || {};
  const emo = data.emotional_summary || {};
  const mhi = data.mental_health_indicators || {};
  const doc = data.doctor_referral || {};

  container.innerHTML = `
    <div class="card">
      <h4>🎯 Severity Assessment</h4>
      <div style="display:flex;align-items:center;gap:14px;margin-bottom:8px">
        <div style="font-family:'Syne',sans-serif;font-size:32px;font-weight:800">${sev.score ?? '—'}<span style="font-size:14px;color:var(--muted)">/100</span></div>
        <div>
          <div class="badge b-${(sev.level||'').toLowerCase()}" style="font-size:12px;padding:4px 12px">${sev.level || '—'}</div>
          <div style="font-size:12px;color:var(--muted);margin-top:4px">${escHtml(sev.description || '')}</div>
        </div>
      </div>
    </div>
    <div class="card">
      <h4>🧠 Emotional Summary</h4>
      <p>${escHtml(emo.overview || '—')}</p>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">
        ${(emo.dominant_emotions || []).map(e => `<span class="badge b-neu">${escHtml(e)}</span>`).join('')}
        ${emo.emotional_consistency ? `<span class="badge b-moderate">${escHtml(emo.emotional_consistency)}</span>` : ''}
      </div>
      ${(emo.key_observations||[]).length ? `<div style="margin-top:10px">
        ${emo.key_observations.map(o => `<div style="font-size:12px;color:var(--muted);padding:5px 0;border-bottom:1px solid var(--border)">• ${escHtml(o)}</div>`).join('')}
      </div>` : ''}
    </div>
    <div class="card">
      <h4>⚠️ Mental Health Indicators</h4>
      ${(mhi.possible_concerns || []).map(c => `<div style="font-size:12px;padding:5px 0;border-bottom:1px solid var(--border)">• ${escHtml(c)}</div>`).join('')}
      ${(mhi.protective_factors || []).map(f => `<div style="font-size:12px;padding:5px 0;color:var(--accent2)">✓ ${escHtml(f)}</div>`).join('')}
      ${mhi.risk_notes ? `<p style="margin-top:8px">⚡ ${escHtml(mhi.risk_notes)}</p>` : ''}
    </div>
    <div class="card">
      <h4>💡 Personalised Suggestions</h4>
      ${(data.suggestions || []).map(s => `
        <div class="sug-item">
          <div class="sug-cat">${escHtml(s.category)}</div>
          <div class="sug-txt">${escHtml(s.suggestion)}</div>
          <div class="sug-why">${escHtml(s.reason)}</div>
        </div>`).join('')}
    </div>
    <div class="card" style="border-color:${doc.recommended ? 'rgba(240,64,96,.3)' : 'var(--border)'}">
      <h4>🏥 Doctor Referral</h4>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
        <span class="badge ${doc.recommended ? 'b-high' : 'b-pos'}">${doc.recommended ? 'Recommended' : 'Not Required'}</span>
        ${doc.urgency       ? `<span class="badge b-neu">${escHtml(doc.urgency)}</span>` : ''}
        ${doc.specialist_type ? `<span class="badge b-neu">${escHtml(doc.specialist_type)}</span>` : ''}
      </div>
      <p>${escHtml(doc.reason || '')}</p>
      ${doc.recommended ? `<button class="sf-btn" style="margin-top:12px" onclick="gotoView('doctors')">Find Doctors Near You →</button>` : ''}
    </div>
    ${(data.general_wellness_tips || []).length ? `
    <div class="card">
      <h4>🌿 Daily Wellness Tips</h4>
      ${data.general_wellness_tips.map(t => `<div style="font-size:12px;padding:5px 0;border-bottom:1px solid var(--border);color:var(--muted)">→ ${escHtml(t)}</div>`).join('')}
    </div>` : ''}
    <div style="font-size:11px;color:var(--muted);text-align:center;padding:14px 0">${escHtml(data.disclaimer || 'AI-generated · Not a clinical diagnosis')}</div>`;
}

// ══════════════════════════════════════════
// HISTORY
// ══════════════════════════════════════════
async function loadHistory() {
  const spinner = document.getElementById('histSpinner');
  const content = document.getElementById('histContent');
  spinner.classList.add('show'); content.innerHTML = '';
  try {
    const [sessRes, repRes] = await Promise.all([apiFetch(API.sessions), apiFetch(API.reports)]);
    const sessions = sessRes.ok ? await sessRes.json() : [];
    const reports  = repRes.ok  ? await repRes.json()  : [];
    const repMap   = Object.fromEntries(reports.map(r => [r.session_id, r]));
    spinner.classList.remove('show');

    if (!sessions.length) {
      content.innerHTML = '<div class="empty"><div class="empty-ico">🕒</div>No past sessions yet.</div>';
      return;
    }
    content.innerHTML = sessions.map(s => `
      <div class="hist-item" onclick="loadSession('${escHtml(s.session_id)}')">
        <div class="hist-date">${new Date(s.started_at).toLocaleString()}</div>
        <div class="hist-txt">Session ${escHtml(s.session_id.split('_').pop())}</div>
        <div class="hist-badges">
          <span class="hb">💬 ${s.message_count} msgs</span>
          ${repMap[s.session_id] ? `<span class="hb" style="color:var(--accent2)">📋 Report</span>` : ''}
          ${repMap[s.session_id]?.severity_level ? `<span class="hb" style="color:var(--accent3)">${escHtml(repMap[s.session_id].severity_level)}</span>` : ''}
        </div>
      </div>`).join('');
  } catch (e) {
    spinner.classList.remove('show');
    content.innerHTML = `<div class="empty"><div class="empty-ico">⚠️</div>${escHtml(e.message)}</div>`;
  }
}

async function loadSession(sid) {
  try {
    const repRes = await apiFetch(API.reports);
    if (repRes.ok) {
      const reports = await repRes.json();
      const rep = reports.find(r => r.session_id === sid);
      if (rep) {
        gotoView('report');
        renderReport(rep.report, document.getElementById('rptContent'), document.getElementById('rptMeta'));
        updateSeverityUI(rep.report.severity);
        fillSuggestions(rep.report.suggestions || []);
        toast('Report loaded from history');
        return;
      }
    }
    const msgRes = await apiFetch(API.sessionMsgs(sid));
    if (!msgRes.ok) throw new Error('Could not load messages');
    const msgs = await msgRes.json();
    gotoView('chat');
    const box = document.getElementById('chatBox');
    box.innerHTML = `<div class="sys-msg">Past session · ${escHtml(sid)}</div>`;
    msgs.forEach(m => { if (m.role === 'user' || m.role === 'assistant') addMsg(m.content, m.role === 'user' ? 'user' : 'bot', m.created_at, false); });
    scrollChat(); toast('Session loaded');
  } catch (e) { toast(e.message, '#f04060'); }
}

// ══════════════════════════════════════════
// NEW SESSION
// ══════════════════════════════════════════
async function newSession() {
  if (!confirm('Start a new session? Current session is already saved.')) return;
  try { await apiFetch(API.reset, { method:'POST', body:JSON.stringify({session_id:sessionId}) }); } catch {}
  sessionId = (currentUser.username || 'user') + '_' + Date.now();
  localStorage.setItem('ms_session_id', sessionId);
  chatLoaded = false;
  msgCount = 0; negCount = 0; faceEmo = 'unknown'; lastSent = 'Neutral'; voiceEmo = 'not_used';
  document.getElementById('chatBox').innerHTML = '<div class="sys-msg">New session started · Say hello 👋</div>';
  document.getElementById('sevNum').textContent = '—';
  document.getElementById('sevFill').style.width = '0%';
  document.getElementById('sevBadge').textContent = 'Not assessed';
  document.getElementById('sevBadge').className = 'badge b-neu';
  document.getElementById('elFace').textContent = cameraOn ? 'Detecting…' : 'Camera off';
  document.getElementById('elSent').textContent = 'Neutral';
  document.getElementById('rSug').innerHTML = '<div class="empty"><div class="empty-ico">💡</div>Suggestions appear after you generate a report.</div>';
  updateStats(); gotoView('chat'); toast('New session started ✓');
}

// ══════════════════════════════════════════
// FIND DOCTORS
// ══════════════════════════════════════════
async function findDoctors() {
  const city    = document.getElementById('docCity').value.trim();
  const lang    = document.getElementById('docLang').value.trim() || 'Hindi';
  const concern = document.getElementById('docConcern').value.trim();
  if (!city) { toast('Please enter your city.', '#f04060'); return; }

  const btn     = document.getElementById('docBtn');
  const spinner = document.getElementById('docSpinner');
  const content = document.getElementById('docContent');
  btn.disabled = true; spinner.classList.add('show'); content.innerHTML = '';

  try {
    const res = await apiFetch(API.findDoctors, { method:'POST', body:JSON.stringify({session_id:sessionId,city,language:lang,concern}) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    spinner.classList.remove('show'); btn.disabled = false;
    renderDoctors(data, content);
  } catch (e) {
    spinner.classList.remove('show'); btn.disabled = false;
    content.innerHTML = `<div class="empty"><div class="empty-ico">⚠️</div>${escHtml(e.message)}</div>`;
    toast(e.message, '#f04060');
  }
}

function renderDoctors(data, container) {
  let html = '';
  const sev = data.session_severity || '';
  if (sev) html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
    <span class="badge b-${sev.toLowerCase()}" style="font-size:12px;padding:4px 12px">Severity: ${escHtml(sev)}</span>
    <span class="badge b-neu" style="font-size:12px;padding:4px 12px">Specialist: ${escHtml(data.recommended_specialist||'—')}</span>
  </div>`;
  if ((data.local_doctors||[]).length) {
    html += `<div style="font-family:'Syne',sans-serif;font-size:15px;font-weight:700;margin-bottom:11px">📍 Local Doctors in ${escHtml(data.city||'')}</div>`;
    data.local_doctors.forEach(d => { html += `
      <div class="doc-card">
        <div class="doc-name">${escHtml(d.name||'—')}</div>
        <div class="doc-spec">${escHtml(d.specialization||'')}</div>
        ${d.clinic_or_hospital ? `<div class="doc-row">🏥 <span>${escHtml(d.clinic_or_hospital)}</span></div>` : ''}
        ${d.address ? `<div class="doc-row">📍 <span>${escHtml(d.address)}</span></div>` : ''}
        ${d.phone ? `<div class="doc-row">📞 <span><a href="tel:${escHtml(d.phone)}" style="color:var(--accent)">${escHtml(d.phone)}</a></span></div>` : ''}
        ${d.consultation_fee ? `<div class="doc-row">💰 <span>${escHtml(d.consultation_fee)}</span></div>` : ''}
        ${d.source_url ? `<div style="margin-top:8px"><a href="${escHtml(d.source_url)}" target="_blank" style="color:var(--accent);font-size:11px">View source →</a></div>` : ''}
      </div>`; });
  }
  if ((data.online_platforms||[]).length) {
    html += `<div style="font-family:'Syne',sans-serif;font-size:15px;font-weight:700;margin:13px 0 10px">💻 Online Platforms</div>`;
    data.online_platforms.forEach(p => { html += `
      <div class="doc-card">
        <div class="doc-name">${escHtml(p.name||'')}</div>
        <div class="doc-spec">${escHtml(p.specialization||'')}</div>
        ${p.approx_fee ? `<div class="doc-row">💰 <span>${escHtml(p.approx_fee)}</span></div>` : ''}
        ${p.url ? `<div style="margin-top:8px"><a href="${escHtml(p.url)}" target="_blank" style="color:var(--accent);font-size:11px">Visit website →</a></div>` : ''}
      </div>`; });
  }
  if ((data.emergency_helplines||[]).length) {
    html += `<div style="font-family:'Syne',sans-serif;font-size:15px;font-weight:700;margin:13px 0 10px">🆘 Emergency Helplines</div>`;
    data.emergency_helplines.forEach(h => { html += `
      <div class="hl-card">
        <div class="hl-name">${escHtml(h.name||'')}</div>
        <div class="hl-num"><a href="tel:${escHtml((h.number||'').replace(/[-\s]/g,''))}" style="color:inherit;text-decoration:none">${escHtml(h.number||'')}</a></div>
        <div class="hl-meta">${escHtml(h.available||'')} · ${escHtml((h.languages||[]).join(', '))}</div>
        ${h.for_situations ? `<div style="font-size:11px;color:var(--muted);margin-top:3px">${escHtml(h.for_situations)}</div>` : ''}
      </div>`; });
  }
  if (data.search_note) html += `<div style="font-size:11px;color:var(--muted);padding:10px;background:var(--bg3);border-radius:8px;margin-top:6px">${escHtml(data.search_note)}</div>`;
  container.innerHTML = html || '<div class="empty"><div class="empty-ico">🔍</div>No results found.</div>';
}

// ══════════════════════════════════════════
// UI HELPERS
// ══════════════════════════════════════════
function scrollChat(smooth = true) {
  const box = document.getElementById('chatBox');
  box.scrollTo({ top: box.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

function addMsg(text, type, timestamp, doScroll = true) {
  const box = document.getElementById('chatBox');
  // Prevent exact duplicate of last bubble
  const bubbles = box.querySelectorAll('.bubble');
  if (bubbles.length > 0) {
    const last = bubbles[bubbles.length - 1];
    if (last.textContent === text && last.closest('.msg').classList.contains(type)) return;
  }
  const div    = document.createElement('div'); div.className = 'msg ' + type;
  const bubble = document.createElement('div'); bubble.className = 'bubble'; bubble.textContent = text;
  const meta   = document.createElement('div'); meta.className = 'msg-meta';
  const ts = timestamp ? new Date(timestamp) : new Date();
  meta.textContent = ts.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  div.append(bubble, meta);
  const typing = document.getElementById('typingDots');
  if (typing?.parentNode === box) box.insertBefore(div, typing);
  else box.appendChild(div);
  if (doScroll) scrollChat();
}

function showTyping(show) {
  document.getElementById('typingDots').classList.toggle('show', show);
  const box = document.getElementById('chatBox');
  box.scrollTop = box.scrollHeight;
}

function updateSentimentUI(s) {
  document.getElementById('elSent').textContent = s;
  const b = document.getElementById('sentBadge');
  b.textContent = s; b.className = 'badge b-' + s.toLowerCase();
}

function updateSeverityUI(sev) {
  if (!sev) return;
  const score = sev.score ?? 0;
  const level = (sev.level || 'low').toLowerCase();
  document.getElementById('sevNum').textContent = score + '/100';
  document.getElementById('sevFill').style.width = score + '%';
  const b = document.getElementById('sevBadge');
  b.textContent = sev.level || '—'; b.className = 'badge b-' + level;
}

function fillSuggestions(suggestions) {
  const panel = document.getElementById('rSug');
  if (!suggestions.length) return;
  panel.innerHTML = suggestions.map(s => `
    <div class="sug-item">
      <div class="sug-cat">${escHtml(s.category)}</div>
      <div class="sug-txt">${escHtml(s.suggestion)}</div>
      <div class="sug-why">${escHtml(s.reason)}</div>
    </div>`).join('');
}

function updateStats() {
  document.getElementById('rStats').innerHTML = `
    <div class="card">
      <h4>📊 Live Session Stats</h4>
      <div style="display:flex;flex-direction:column;gap:7px;margin-top:4px">
        <div class="doc-row">💬 <span>Messages: <strong>${msgCount}</strong></span></div>
        <div class="doc-row">😔 <span>Negative signals: <strong>${negCount}</strong></span></div>
        <div class="doc-row">🎥 <span>Face: <strong>${faceEmo}</strong></span></div>
        <div class="doc-row">💭 <span>Last sentiment: <strong>${lastSent}</strong></span></div>
        <div class="doc-row" style="font-size:10px;color:var(--muted)">🔑 ${escHtml(sessionId)}</div>
      </div>
    </div>`;
}

function gotoView(name) {
  const views = { chat:'viewChat', report:'viewReport', history:'viewHistory', doctors:'viewDoctors' };
  const navs  = { chat:'navChat',  report:'navReport',  history:'navHistory' };
  Object.values(views).forEach(id => {
    const el = document.getElementById(id); if (!el) return;
    el.style.display = 'none'; el.classList.remove('active');
  });
  Object.values(navs).forEach(id => document.getElementById(id)?.classList.remove('active'));
  const target = document.getElementById(views[name]);
  if (!target) return;
  if (target.classList.contains('page-view')) { target.style.display = 'block'; target.classList.add('active'); }
  else target.style.display = 'flex';
  document.getElementById(navs[name])?.classList.add('active');
  if (name === 'history') loadHistory();
}

function switchRTab(id, btn) {
  document.querySelectorAll('.rpanel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.rtab').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active'); btn.classList.add('active');
}

function toast(msg, color = '') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.borderColor = color || 'var(--border2)';
  t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 3200);
}

function setAuthBtnLoading(btnId, loading) {
  const b = document.getElementById(btnId); if (!b) return;
  b.disabled = loading;
  b.textContent = loading ? 'Please wait…' : (btnId === 'li_btn' ? 'Sign In →' : 'Create Account →');
}

function escHtml(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function v(id) { return (document.getElementById(id)?.value || '').trim(); }

// ══════════════════════════════════════════
// KEYBOARD SHORTCUTS
// ══════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.activeElement?.id === 'userInput') sendMessage();
});
['li_pass', 'li_user'].forEach(id =>
  document.getElementById(id)?.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); }));
['su_name','su_email','su_user','su_pass'].forEach(id =>
  document.getElementById(id)?.addEventListener('keydown', e => { if (e.key === 'Enter') doSignup(); }));
