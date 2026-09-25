// Webview script for the Flint Swarm sidebar. Talks to extension.js by postMessage only.
(function () {
  'use strict';
  const vscode = acquireVsCodeApi();
  const R = window.FlintRender;
  const saved = vscode.getState() || {};
  const threads = saved.threads || [];          // [{id, kind, question, where, repo, cards:{}, order:[], done}]
  let status = saved.status || null, info = saved.info || null;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const save = () => vscode.setState({ threads: threads.slice(-20), status, info });

  function statusHtml() {
    if (!status && !info) return '<span class="dim">Loading swarm status…</span>';
    const bits = [];
    if (status) {
      const name = status.repo.split('/').pop();
      if (status.daemon_running) bits.push(`<span class="ok">● running</span> on <b>${R.esc(name)}</b>`);
      else if (status.is_target) bits.push(`<span class="dim">○ stopped</span> · set up for <b>${R.esc(name)}</b>`);
      else bits.push(`<span class="dim">○ not set up for <b>${R.esc(name)}</b></span>`);
      bits.push(`${status.queue.length} queued · ${status.landed} landed` + (status.trunk_ahead ? ` · trunk +${status.trunk_ahead}` : ''));
      const b = status.budget;
      if (b) bits.push(`swarm budget ${b.spent_today}/${b.usable} today, resets ${R.esc(b.resets_local)}`);
    }
    if (info && info.quota) bits.push(`OpenRouter free requests ${info.quota.used}/${info.quota.limit}`);
    if (info && info.corpus) bits.push(`MIT corpus ${info.corpus.chunks.toLocaleString()} chunks`);
    if (status && status.experiment) bits.push(`MIT experiment: ${status.experiment.on} with / ${status.experiment.off} without`);
    return bits.map((b) => `<div>${b}</div>`).join('');
  }

  function editingHtml() {
    // What the swarm has changed, newest first. Each row opens the file in the target repo.
    const files = (status && status.editing) || [];
    if (!files.length) return '';
    const repo = (status && status.repo) || '';
    const rows = files.slice(0, 8).map((f) => {
      const churn = `<span class="dim">+${f.added}\u2212${f.removed}</span>`;
      const where = f.task ? ` <span class="dim">${R.esc(f.task.slice(0, 40))}</span>` : '';
      return `<div><a href="#" class="file" data-path="${R.esc(repo + '/' + f.path)}">${R.esc(f.path)}</a> ${churn}${where}</div>`;
    }).join('');
    return `<div class="section"><b>Being edited</b>${rows}</div>`;
  }

  function renderStatus() {
    $('status').innerHTML = statusHtml();
    const running = status && status.daemon_running;
    $('start').disabled = !!running;
    $('stop').disabled = !running;
    const recent = (status && status.recent) || [];
    $('recent').innerHTML = recent.length ? recent.slice(-6).reverse().map((r) => `<div>${R.esc(r)}</div>`).join('') : '';
    $('editing').innerHTML = editingHtml();
  }

  function cardHtml(c) {
    if (c.kind === 'hit') {
      const where = [c.hit.course, c.hit.lecture || c.hit.document, c.hit.page ? 'p. ' + c.hit.page : null].filter(Boolean).join(' · ');
      return `<div class="meta"><span class="tag">${R.esc(c.hit.kind)}</span> ${R.esc(where)}
        <a href="#" class="file" data-path="${R.esc(c.hit.path)}"${c.hit.page ? ` data-page="${c.hit.page}"` : ''}>open</a></div>
        <div class="md">${R.render(c.hit.text.slice(0, 900))}</div>`;
    }
    const m = R.esc(c.model || '');
    if (c.state === 'pending') return `<div class="meta"><span class="spin"></span> ${m}${c.role === 'synthesis' ? ' is checking and merging the answers…' : ' is reading your code…'}` +
      (c.progress ? ` <span class="dim">${R.esc(c.progress)}</span>` : '') + '</div>';
    if (c.state === 'error') return `<div class="meta err">${m}: ${R.esc(c.error || 'failed')}</div>`;
    const facts = [c.secs != null ? c.secs + 's' : null, c.requests ? c.requests + ' requests' : null,
      c.tool_calls ? c.tool_calls + ' tool calls' : null, c.study_calls ? `<b>${c.study_calls} MIT lookups</b>` : null].filter(Boolean).join(' · ');
    const vote = c.role === 'synthesis' ? '' :
      `<span class="vote">${c.voted ? (c.voted > 0 ? '👍 noted' : '👎 noted') :
        `<button class="icon" data-vote="1" data-model="${m}" title="Useful: this model gets picked more">👍</button><button class="icon" data-vote="0" data-model="${m}" title="Not useful">👎</button>`}</span>`;
    return `<div class="meta">${c.role === 'synthesis' ? '<span class="tag">merged</span> ' : ''}<b>${m}</b> ${facts}${vote}</div>
      <div class="md">${R.render(c.text)}</div>`;
  }

  function renderThread(t) {
    const node = el('article', 'thread');
    node.dataset.id = t.id;
    node.dataset.repo = t.repo || '';
    const head = t.kind === 'study' ? `📚 MIT lookup: ${R.esc(t.question)}` : R.esc(t.question);
    node.appendChild(el('div', 'q', `${head}${t.where ? ` <span class="where">${R.esc(t.where)}</span>` : ''}` +
      `<button class="icon close" title="Remove">✕</button>`));
    const ordered = t.order.map((k) => t.cards[k]);
    ordered.sort((a, b) => (b.role === 'synthesis') - (a.role === 'synthesis'));
    for (const c of ordered) node.appendChild(el('div', 'card' + (c.role === 'synthesis' ? ' synthesis' : '') + (c.state === 'error' ? ' failed' : ''), cardHtml(c)));
    if (t.note) node.appendChild(el('div', 'note', R.esc(t.note)));
    return node;
  }

  function renderThreads() {
    const box = $('threads');
    box.innerHTML = '';
    for (const t of threads.slice().reverse()) box.appendChild(renderThread(t));
    if (!threads.length) box.innerHTML = '<p class="dim">Select code (or just put the cursor in a function), then ask. Every answer comes from a different free model; one more model checks them against your code and merges them.</p>';
  }

  function thread(id) { return threads.find((t) => t.id === id); }

  function onAskEvent(t, ev) {
    const key = (ev.role === 'synthesis' ? 'synthesis:' : '') + (ev.model || ev.event);
    const put = (card) => { if (!t.cards[key]) t.order.push(key); t.cards[key] = Object.assign(t.cards[key] || {}, card); };
    if (ev.event === 'context') { t.where = ev.where || t.where; t.note = ev.study ? 'Models can search the MIT OpenCourseWare corpus.' : ''; }
    else if (ev.event === 'start') put({ model: ev.model, role: ev.role, state: 'pending' });
    else if (ev.event === 'progress') { if (t.cards[key] && t.cards[key].state === 'pending') t.cards[key].progress = ev.text; }
    else if (ev.event === 'answer' || ev.event === 'synthesis') put(Object.assign({}, ev, { state: 'done', role: ev.event === 'synthesis' ? 'synthesis' : ev.role }));
    else if (ev.event === 'error') {
      if (ev.model) put({ model: ev.model, role: ev.role, state: 'error', error: ev.error });
      else t.note = ev.error;
    } else if (ev.event === 'done') {
      t.done = true;
      t.note = `${ev.answered}/${ev.asked} models answered` + (ev.requests ? ` · ${ev.requests} free requests` : '');
    }
  }

  window.addEventListener('message', (e) => {
    const msg = e.data;
    if (msg.type === 'status') { status = msg.data; renderStatus(); }
    else if (msg.type === 'info') { info = msg.data; renderStatus(); }
    else if (msg.type === 'askStart') { threads.push({ id: msg.id, kind: 'ask', question: msg.question, where: msg.where, repo: msg.repo, cards: {}, order: [] }); renderThreads(); }
    else if (msg.type === 'askEvent') { const t = thread(msg.id); if (t) { onAskEvent(t, msg.event); renderThreads(); } }
    else if (msg.type === 'study') {
      const t = { id: msg.id, kind: 'study', question: msg.query, cards: {}, order: [], done: true };
      (msg.hits || []).forEach((h, i) => { t.cards['h' + i] = { kind: 'hit', hit: h }; t.order.push('h' + i); });
      if (!(msg.hits || []).length) t.note = 'No matches in the MIT corpus.';
      threads.push(t); renderThreads();
    } else if (msg.type === 'notice') { $('notice').textContent = msg.text; $('notice').className = 'notice ' + (msg.level || ''); }
    else if (msg.type === 'prefill') { $('question').value = msg.question || ''; $('question').focus(); }
    save();
  });

  document.addEventListener('click', (e) => {
    const a = e.target.closest('a.file');
    if (a) {
      e.preventDefault();
      const th = e.target.closest('.thread');
      vscode.postMessage({ type: 'open', path: a.dataset.path, line: a.dataset.line ? +a.dataset.line : null,
        page: a.dataset.page ? +a.dataset.page : null, repo: th ? th.dataset.repo : '' });
      return;
    }
    const v = e.target.closest('button[data-vote]');
    if (v) {
      const th = e.target.closest('.thread');
      const t = thread(th.dataset.id);
      const card = Object.values(t.cards).find((c) => c.model === v.dataset.model && c.role !== 'synthesis');
      if (card) card.voted = v.dataset.vote === '1' ? 1 : -1;
      vscode.postMessage({ type: 'vote', model: v.dataset.model, useful: v.dataset.vote === '1' });
      renderThreads(); save();
      return;
    }
    if (e.target.closest('button.close')) {
      const th = e.target.closest('.thread');
      const i = threads.findIndex((t) => t.id === th.dataset.id);
      if (i >= 0) threads.splice(i, 1);
      renderThreads(); save();
    }
  });

  $('askForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('question').value.trim();
    if (!q) { $('question').focus(); return; }
    vscode.postMessage({ type: 'ask', question: q, withCode: $('withCode').checked });
  });
  $('question').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $('askForm').requestSubmit(); }
  });
  $('queue').addEventListener('click', () => vscode.postMessage({ type: 'queue', title: $('question').value.trim(), withCode: $('withCode').checked }));
  $('lookup').addEventListener('click', () => vscode.postMessage({ type: 'study', query: $('question').value.trim(), withCode: $('withCode').checked }));
  $('start').addEventListener('click', () => vscode.postMessage({ type: 'start' }));
  $('stop').addEventListener('click', () => vscode.postMessage({ type: 'stop' }));
  $('report').addEventListener('click', () => vscode.postMessage({ type: 'report' }));

  renderStatus();
  renderThreads();
  vscode.postMessage({ type: 'ready' });
})();
