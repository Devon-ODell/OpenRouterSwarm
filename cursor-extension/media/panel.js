// Webview script for the Flint Swarm sidebar. Talks to extension.js by postMessage only.
// Two tabs: the swarm conversation, and the queue — where a task can be edited or dropped.
(function () {
  'use strict';
  const vscode = acquireVsCodeApi();
  const R = window.FlintRender;
  const saved = vscode.getState() || {};
  const threads = saved.threads || [];          // [{id, kind, question, where, repo, cards:{}, order:[], done}]
  let status = saved.status || null, info = saved.info || null;
  let tab = saved.tab || 'ask';
  const open = new Set();                       // queue rows expanded to show their detail
  let editing = null;                           // {id, task} while a task is being rewritten
  let editError = null;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const save = () => vscode.setState({ threads: threads.slice(-20), status, info, tab });
  // Cents are the unit that matters under a dollar: the first few questions cost fractions of one.
  const money = (n) => '$' + Number(n || 0).toFixed(Math.abs(Number(n) || 0) < 1 ? 4 : 2);

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
      const w = status.wallet;
      if (w) {
        bits.push(`paid budget <b>${money(w.spent)}</b> of ${money(w.cap)} used` +
          (w.remaining <= 0 ? ' <span class="err">— spent</span>' : '') +
          ` <button class="icon" id="budget" title="Change the paid budget">✎</button>`);
      } else bits.push('<span class="dim">paid models off</span> <button class="icon" id="budget" title="Set a paid budget">✎</button>');
    }
    if (info && info.quota) bits.push(`OpenRouter free requests ${info.quota.used}/${info.quota.limit}`);
    if (info && info.corpus) bits.push(`MIT corpus ${info.corpus.chunks.toLocaleString()} chunks`);
    if (status && status.experiment) bits.push(`MIT experiment: ${status.experiment.on} with / ${status.experiment.off} without`);
    return bits.map((b) => `<div>${b}</div>`).join('');
  }

  function renderStatus() {
    $('status').innerHTML = statusHtml();
    const running = status && status.daemon_running;
    $('start').disabled = !!running;
    $('stop').disabled = !running;
    const recent = (status && status.recent) || [];
    $('recent').innerHTML = recent.length ? recent.slice(-6).reverse().map((r) => `<div>${R.esc(r)}</div>`).join('') : '';
    const n = status ? status.queue.length : 0;
    const badge = $('qcount');
    badge.textContent = n ? String(n) : '';
    badge.hidden = !n;
  }

  // ---------------------------------------------------------------- tabs

  function renderTab() {
    for (const name of ['ask', 'queue']) {
      $('pane-' + name).hidden = tab !== name;
      const t = $('tab-' + name);
      t.classList.toggle('active', tab === name);
      t.setAttribute('aria-selected', tab === name ? 'true' : 'false');
    }
    if (tab === 'queue') renderQueue();
  }

  function showTab(name) {
    if (tab === name) return;
    tab = name;
    renderTab();
    save();
    if (name === 'queue') vscode.postMessage({ type: 'refresh' });
  }

  // ---------------------------------------------------------------- queue

  function waitFor(t) {
    const secs = Math.round((t.not_before || 0) - Date.now() / 1000);
    if (secs <= 0) return null;
    return secs < 90 ? `retrying in ${secs}s` : `retrying in ${Math.round(secs / 60)}m`;
  }

  function queueMeta(t) {
    const bits = [R.esc(t.kind)];
    if (t.origin) bits.push(R.esc(t.origin));
    if (t.attempts) bits.push(`${t.attempts} attempt${t.attempts > 1 ? 's' : ''}`);
    if (t.depends_on && t.depends_on.length) bits.push(`waits for ${t.depends_on.length}`);
    if (t.blocks && t.blocks.length) bits.push(`blocks ${t.blocks.length}`);
    const wait = waitFor(t);
    if (t.claimed) bits.push('<span class="ok">running now</span>');
    else if (wait) bits.push(`<span class="warn">${wait}</span>`);
    return bits.join(' · ');
  }

  function queueRow(t) {
    const row = el('div', 'qrow' + (t.claimed ? ' claimed' : ''));
    row.dataset.id = t.id;
    const prio = ['later', 'next', 'first'][t.priority] || 'next';
    row.appendChild(el('div', 'qmain',
      `<span class="prio p${t.priority}" title="priority ${t.priority} — ${prio}">P${t.priority}</span>` +
      `<span class="qtitle">${R.esc(t.title)}</span>` +
      `<span class="qmeta">${queueMeta(t)}</span>`));
    row.appendChild(el('div', 'qactions',
      `<button class="icon qedit" title="Edit this task">✎</button>` +
      `<button class="icon qdel" title="Remove this task from the queue">✕</button>`));
    if (open.has(t.id)) {
      const parts = [];
      if (t.detail) parts.push(`<div class="md">${R.render(t.detail)}${t.detail_truncated ? '<p class="dim">…</p>' : ''}</div>`);
      if (t.acceptance && t.acceptance.length) {
        parts.push('<div class="accept"><b>Done when</b><ul>' +
          t.acceptance.map((a) => `<li>${R.esc(a)}</li>`).join('') + '</ul></div>');
      }
      if (t.note) parts.push(`<div class="note">last note: ${R.esc(String(t.note).slice(0, 400))}</div>`);
      if (t.blocks && t.blocks.length) {
        parts.push(`<div class="note">other tasks waiting on this one: ${t.blocks.map((b) => R.esc(b.title)).join(', ')}</div>`);
      }
      row.appendChild(el('div', 'qdetail', parts.join('') || '<span class="dim">No detail.</span>'));
    }
    return row;
  }

  function editForm(t) {
    const kinds = (status && status.kinds) || ['feature', 'bugfix', 'test', 'refactor'];
    const form = el('form', 'qform');
    form.dataset.id = t.id;
    form.innerHTML =
      `<input class="f-title" value="${R.esc(t.title)}" placeholder="Task title" aria-label="Task title">` +
      `<textarea class="f-detail" placeholder="What the swarm should do" aria-label="Detail">${R.esc(t.detail || '')}</textarea>` +
      `<textarea class="f-accept" placeholder="Done when… (one per line)" aria-label="Acceptance criteria">${R.esc((t.acceptance || []).join('\n'))}</textarea>` +
      `<div class="row">` +
      `<select class="f-kind" aria-label="Kind">${kinds.map((k) => `<option value="${R.esc(k)}"${k === t.kind ? ' selected' : ''}>${R.esc(k)}</option>`).join('')}</select>` +
      `<select class="f-prio" aria-label="Priority">${[0, 1, 2].map((v) => `<option value="${v}"${v === t.priority ? ' selected' : ''}>P${v} — ${['later', 'next', 'first'][v]}</option>`).join('')}</select>` +
      `<button type="submit">Save</button><button type="button" class="secondary f-cancel">Cancel</button></div>` +
      (editError ? `<div class="notice error">${R.esc(editError)}</div>` : '');
    return form;
  }

  function renderQueue() {
    const head = $('queueHead'), list = $('queueList');
    const tasks = (status && status.queue) || [];
    const max = (status && status.max_queue) || 20;
    head.innerHTML = status
      ? `<div class="qhead"><span>${tasks.length} of ${max} queued for <b>${R.esc(status.repo.split('/').pop())}</b></span>` +
        `<button id="qclear" class="secondary"${tasks.length ? '' : ' disabled'}>Clear all</button></div>`
      : '<span class="dim">Loading the queue…</span>';
    list.innerHTML = '';
    if (!tasks.length) {
      list.innerHTML = status
        ? '<p class="dim">Nothing queued. Select code and use <b>Queue as task</b>, or let the planner fill the queue while the swarm runs.</p>'
        : '';
      return;
    }
    const order = tasks.slice().sort((a, b) =>
      (b.claimed - a.claimed) || (b.priority - a.priority) || (a.created - b.created));
    for (const t of order) {
      list.appendChild(editing && editing.id === t.id ? editForm(editing.task) : queueRow(t));
    }
  }

  // ---------------------------------------------------------------- threads

  function cardHtml(c) {
    if (c.kind === 'hit') {
      const where = [c.hit.course, c.hit.lecture || c.hit.document, c.hit.page ? 'p. ' + c.hit.page : null].filter(Boolean).join(' · ');
      return `<div class="meta"><span class="tag">${R.esc(c.hit.kind)}</span> ${R.esc(where)}
        <a href="#" class="file" data-path="${R.esc(c.hit.path)}"${c.hit.page ? ` data-page="${c.hit.page}"` : ''}>open</a></div>
        <div class="md">${R.render(c.hit.text.slice(0, 900))}</div>`;
    }
    const m = R.esc(c.model || '');
    const paid = c.paid ? '<span class="tag paid" title="a paid model, charged to the extension budget">paid</span> ' : '';
    if (c.state === 'pending') return `<div class="meta">${paid}<span class="spin"></span> ${m}${c.role === 'synthesis' ? ' is checking and merging the answers…' : ' is reading your code…'}` +
      (c.progress ? ` <span class="dim">${R.esc(c.progress)}</span>` : '') + '</div>';
    if (c.state === 'error') return `<div class="meta err">${paid}${m}: ${R.esc(c.error || 'failed')}</div>`;
    const facts = [c.secs != null ? c.secs + 's' : null, c.requests ? c.requests + ' requests' : null,
      c.tool_calls ? c.tool_calls + ' tool calls' : null, c.study_calls ? `<b>${c.study_calls} MIT lookups</b>` : null,
      c.usd ? money(c.usd) : null].filter(Boolean).join(' · ');
    const vote = c.role === 'synthesis' ? '' :
      `<span class="vote">${c.voted ? (c.voted > 0 ? '👍 noted' : '👎 noted') :
        `<button class="icon" data-vote="1" data-model="${m}" title="Useful: this model gets picked more">👍</button><button class="icon" data-vote="0" data-model="${m}" title="Not useful">👎</button>`}</span>`;
    return `<div class="meta">${c.role === 'synthesis' ? '<span class="tag">merged</span> ' : ''}${paid}<b>${m}</b> ${facts}${vote}</div>
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
    else if (ev.event === 'start') put({ model: ev.model, role: ev.role, state: 'pending', paid: !!ev.paid });
    else if (ev.event === 'progress') { if (t.cards[key] && t.cards[key].state === 'pending') t.cards[key].progress = ev.text; }
    else if (ev.event === 'answer' || ev.event === 'synthesis') put(Object.assign({}, ev, { state: 'done', role: ev.event === 'synthesis' ? 'synthesis' : ev.role }));
    else if (ev.event === 'rescue') {
      t.note = (ev.models || []).length
        ? `No free model answered — paying for ${ev.models.join(', ')} from the extension budget.`
        : `No free model answered, and no paid model could be used: ${ev.why || 'the budget is spent'}.`;
    } else if (ev.event === 'error') {
      if (ev.model) put({ model: ev.model, role: ev.role, state: 'error', error: ev.error, paid: !!ev.paid });
      else t.note = ev.error;
    } else if (ev.event === 'done') {
      t.done = true;
      t.note = `${ev.answered}/${ev.asked} models answered` + (ev.requests ? ` · ${ev.requests} free requests` : '')
        + (ev.usd ? ` · ${money(ev.usd)} of the paid budget` : '');
    }
  }

  // ---------------------------------------------------------------- messages

  window.addEventListener('message', (e) => {
    const msg = e.data;
    if (msg.type === 'status') {
      status = msg.data;
      // A task that was rewritten or removed elsewhere must not stay open in a stale form.
      if (editing && !status.queue.some((t) => t.id === editing.id)) editing = null;
      renderStatus();
      if (tab === 'queue') renderQueue();
    } else if (msg.type === 'info') { info = msg.data; renderStatus(); }
    else if (msg.type === 'tab') { tab = msg.tab === 'queue' ? 'queue' : 'ask'; renderTab(); }
    else if (msg.type === 'queueTask') { editing = { id: msg.task.id, task: msg.task }; editError = null; showTab('queue'); renderQueue(); }
    else if (msg.type === 'queueSaved') { editing = null; editError = null; renderQueue(); }
    else if (msg.type === 'queueError') { editError = msg.error; renderQueue(); }
    else if (msg.type === 'askStart') { threads.push({ id: msg.id, kind: 'ask', question: msg.question, where: msg.where, repo: msg.repo, cards: {}, order: [] }); showTab('ask'); renderThreads(); }
    else if (msg.type === 'askEvent') { const t = thread(msg.id); if (t) { onAskEvent(t, msg.event); renderThreads(); } }
    else if (msg.type === 'study') {
      const t = { id: msg.id, kind: 'study', question: msg.query, cards: {}, order: [], done: true };
      (msg.hits || []).forEach((h, i) => { t.cards['h' + i] = { kind: 'hit', hit: h }; t.order.push('h' + i); });
      if (!(msg.hits || []).length) t.note = 'No matches in the MIT corpus.';
      threads.push(t); showTab('ask'); renderThreads();
    } else if (msg.type === 'notice') { $('notice').textContent = msg.text; $('notice').className = 'notice ' + (msg.level || ''); }
    else if (msg.type === 'prefill') { $('question').value = msg.question || ''; $('question').focus(); }
    save();
  });

  // ---------------------------------------------------------------- clicks

  function queued(id) { return ((status && status.queue) || []).find((t) => t.id === id); }

  document.addEventListener('click', (e) => {
    const tabBtn = e.target.closest('.tab');
    if (tabBtn) { showTab(tabBtn.dataset.tab); return; }
    if (e.target.closest('#budget')) { vscode.postMessage({ type: 'budget' }); return; }
    if (e.target.closest('#qclear')) {
      vscode.postMessage({ type: 'queueClear', repo: status && status.repo });
      return;
    }
    const row = e.target.closest('.qrow');
    if (row) {
      const id = row.dataset.id, t = queued(id);
      if (e.target.closest('.qdel')) {
        vscode.postMessage({ type: 'queueRemove', id, repo: status && status.repo,
          title: t ? t.title : '', claimed: !!(t && t.claimed), blocks: (t && t.blocks) || [] });
        return;
      }
      if (e.target.closest('.qedit')) {
        editError = null;
        vscode.postMessage({ type: 'queueGet', id, repo: status && status.repo });
        return;
      }
      if (open.has(id)) open.delete(id); else open.add(id);
      renderQueue();
      return;
    }
    if (e.target.closest('.f-cancel')) { editing = null; editError = null; renderQueue(); return; }
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

  document.addEventListener('submit', (e) => {
    const form = e.target.closest('.qform');
    if (!form) return;
    e.preventDefault();
    const val = (cls) => form.querySelector(cls).value;
    const title = val('.f-title').trim();
    if (!title) { form.querySelector('.f-title').focus(); return; }
    vscode.postMessage({ type: 'queueEdit', id: form.dataset.id, repo: status && status.repo,
      title, detail: val('.f-detail').trim(), kind: val('.f-kind'), priority: +val('.f-prio'),
      acceptance: val('.f-accept').split('\n').map((l) => l.trim()).filter(Boolean) });
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
  renderTab();
  vscode.postMessage({ type: 'ready' });
})();
