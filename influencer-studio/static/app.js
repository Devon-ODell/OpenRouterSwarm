'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const state = { influencers: [], active: null, posts: [] };

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await res.json().catch(() => ({ error: `Request failed (${res.status})` }));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.className = error ? 'show error' : 'show';
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.className = ''; }, 3200);
}
function busy(button, on, label = 'Working…') {
  if (on) { button.dataset.label = button.innerHTML; button.innerHTML = label; button.disabled = true; }
  else { button.innerHTML = button.dataset.label || button.innerHTML; button.disabled = false; }
}
function showView(name) {
  $('#studioView').classList.toggle('hidden', name !== 'studio');
  $('#createView').classList.toggle('hidden', name !== 'create');
  $('#pageTitle').textContent = name === 'create' ? 'Create a persona' : 'Your studio';
  $$('.nav').forEach((n) => n.classList.toggle('active', n.dataset.view === name));
}
function initials(name) { return name.split(/\s+/).slice(0, 2).map((x) => x[0]).join('').toUpperCase(); }

async function boot() {
  try {
    const [health, people] = await Promise.all([api('/api/health'), api('/api/influencers')]);
    $('#modeName').textContent = health.mode === 'demo' ? 'Offline demo' : 'OpenRouter live';
    $('#modeModels').textContent = `${health.chat_model} · ${health.image_model}`;
    state.influencers = people.influencers;
    state.active = state.influencers[0] || null;
    render();
  } catch (e) { toast(e.message, true); }
}
function render() {
  $('#empty').classList.toggle('hidden', !!state.active);
  $('#dashboard').classList.toggle('hidden', !state.active);
  if (!state.active) return;
  const p = state.active;
  $('#personaSelect').innerHTML = state.influencers.map((x) => `<option value="${x.id}" ${x.id === p.id ? 'selected' : ''}>${escapeHtml(x.name)} · @${escapeHtml(x.handle)}</option>`).join('');
  $('#avatar').textContent = initials(p.name); $('#profileName').textContent = p.name;
  $('#profileHandle').textContent = `@${p.handle}`; $('#profileBio').textContent = p.bio;
  $('#profileValues').innerHTML = p.values.map((v) => `<span class="chip">${escapeHtml(v)}</span>`).join('');
  loadPosts(); loadThread();
}
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }
async function loadPosts() {
  const id = state.active.id;
  try { const data = await api(`/api/influencers/${id}/posts`); if (state.active.id !== id) return; state.posts = data.posts; renderPosts(); }
  catch (e) { toast(e.message, true); }
}
function renderPosts() {
  $('#postGrid').innerHTML = state.posts.length ? state.posts.map((p) => `<article class="post"><img src="${encodeURI(p.image_url)}" alt="AI-generated post"><span class="ai-badge">✦ AI GENERATED</span><div class="post-overlay"><p>${escapeHtml(p.caption)}</p></div></article>`).join('') : '<button class="blank-post" data-generate><span><b>No posts yet</b><br>Direct the first campaign<br><br>＋ Generate</span></button>';
}
function openPost() { if (state.active) $('#postDialog').showModal(); }

$('#personaForm').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = event.submitter; busy(button, true, 'Creating…');
  const data = Object.fromEntries(new FormData(event.currentTarget));
  data.values = data.values.split(',').map((x) => x.trim()).filter(Boolean);
  try { const out = await api('/api/influencers', { method: 'POST', body: JSON.stringify(data) }); state.influencers.unshift(out.influencer); state.active = out.influencer; event.currentTarget.reset(); showView('studio'); render(); toast('Persona created'); }
  catch (e) { toast(e.message, true); } finally { busy(button, false); }
});
$('#postForm').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = event.submitter; busy(button, true, 'Directing the swarm…');
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try { const out = await api(`/api/influencers/${state.active.id}/posts/generate`, { method: 'POST', body: JSON.stringify(data) }); state.posts.unshift(out.post); renderPosts(); $('#postDialog').close(); event.currentTarget.reset(); toast('New AI post generated'); }
  catch (e) { toast(e.message, true); } finally { busy(button, false); }
});
$('#chatForm').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = event.submitter, message = $('#fanMessage').value.trim(); if (!message) return;
  appendMessage('fan', $('#fanName').value, message); $('#fanMessage').value = ''; busy(button, true, '…');
  try { const out = await api(`/api/influencers/${state.active.id}/chat`, { method: 'POST', body: JSON.stringify({ fan_id: $('#fanId').value, fan_name: $('#fanName').value, message }) }); appendMessage('influencer', state.active.name + ' · AI', out.reply.content); }
  catch (e) { toast(e.message, true); } finally { busy(button, false); }
});
async function loadThread() {
  $('#messages').innerHTML = '<div class="welcome">Start a conversation. Replies use this persona\'s voice and remember the thread.</div>';
}
function appendMessage(role, name, content) {
  const box = $('#messages'); $('.welcome')?.remove(); const el = document.createElement('div'); el.className = `bubble ${role === 'influencer' ? 'ai' : ''}`;
  const small = document.createElement('small'), text = document.createElement('div'); small.textContent = name; text.textContent = content; el.append(small, text); box.append(el); box.scrollTop = box.scrollHeight;
}
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-go-create],#newPersona')) showView('create');
  if (e.target.closest('[data-go-studio]')) showView('studio');
  const nav = e.target.closest('.nav'); if (nav) showView(nav.dataset.view);
  if (e.target.closest('#generateTop,#generateCircle,[data-generate]')) openPost();
  if (e.target.closest('.close')) $('#postDialog').close();
  const tab = e.target.closest('.tab'); if (tab) { $$('.tab').forEach((x) => x.classList.toggle('active', x === tab)); $('#contentTab').classList.toggle('hidden', tab.dataset.tab !== 'content'); $('#inboxTab').classList.toggle('hidden', tab.dataset.tab !== 'inbox'); }
});
$('#personaSelect').addEventListener('change', (e) => { state.active = state.influencers.find((x) => x.id === e.target.value); render(); });
boot();
